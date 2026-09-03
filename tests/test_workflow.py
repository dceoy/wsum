"""Tests for deterministic local workflow helpers."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
import workflow
from workflow import WorkflowError, change_action, promote_snapshot, validate_targets


def _target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
    }
    target.update(overrides)
    return target


def _candidate(runtime_dir: Path, content: str = "next\n") -> tuple[Path, str]:
    directory = runtime_dir / "candidates"
    directory.mkdir()
    path = directory / "example.txt"
    path.write_text(content)
    return path, hashlib.sha256(content.encode()).hexdigest()


def test_validate_targets_defaults_local_target_fields() -> None:
    result = validate_targets({"targets": [_target()]})

    assert result == {
        "targets": [
            {
                "target_id": "example",
                "name": "Example",
                "url": "https://example.com/",
                "enabled": True,
                "action": "monitor",
                "watch_focus": "",
                "notification_group": "",
                "fetch_mode": "static",
            }
        ]
    }


def test_validate_targets_marks_disabled_target_before_fetch() -> None:
    result = validate_targets({"targets": [_target(enabled=False)]})

    assert result == {
        "targets": [
            {
                "target_id": "example",
                "name": "Example",
                "url": "https://example.com/",
                "enabled": False,
                "action": "skip_disabled",
                "watch_focus": "",
                "notification_group": "",
                "fetch_mode": "static",
            }
        ]
    }


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "https://user:pass@example.com/",
        "https://example.com/?token=secret",
        "https://example.com/#fragment",
    ],
)
def test_validate_targets_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(WorkflowError):
        validate_targets({"targets": [_target(url=url)]})


def test_validate_targets_rejects_duplicate_ids() -> None:
    with pytest.raises(WorkflowError, match="duplicate_target_id"):
        validate_targets({"targets": [_target(), _target(name="Second")]})


@pytest.mark.parametrize(
    ("payload", "action"),
    [
        ({"status": "baseline"}, "promote_snapshot"),
        ({"status": "unchanged"}, "discard_candidate"),
        (
            {"status": "changed", "diff_truncated": False, "materiality": None},
            "assess_materiality",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": False},
            "promote_snapshot",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": True},
            "notify",
        ),
        (
            {"status": "changed", "diff_truncated": True, "materiality": False},
            "manual_review",
        ),
    ],
)
def test_change_action_routes_result(payload: dict[str, object], action: str) -> None:
    assert change_action(payload) == {"action": action}


def test_change_action_rejects_invalid_changed_payload() -> None:
    with pytest.raises(WorkflowError, match="diff_truncated"):
        change_action({"status": "changed"})


def test_promote_snapshot_creates_baseline(tmp_path: Path) -> None:
    candidate, digest = _candidate(tmp_path)

    result = promote_snapshot(
        tmp_path,
        candidate,
        {
            "target_id": "example",
            "expected_sha256": None,
            "candidate_sha256": digest,
        },
    )

    snapshot = tmp_path / "snapshots" / "example.txt"
    assert result["action"] == "snapshot_promoted"
    assert result["sha256"] == digest
    assert snapshot.read_text() == "next\n"


def test_promote_snapshot_replaces_expected_baseline(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    old = b"old\n"
    (snapshots / "example.txt").write_bytes(old)
    candidate, digest = _candidate(tmp_path)

    result = promote_snapshot(
        tmp_path,
        candidate,
        {
            "target_id": "example",
            "expected_sha256": hashlib.sha256(old).hexdigest(),
            "candidate_sha256": digest,
        },
    )

    assert result["applied"] is True
    assert (snapshots / "example.txt").read_text() == "next\n"


def test_promote_snapshot_reports_stale_baseline(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    current = b"current\n"
    (snapshots / "example.txt").write_bytes(current)
    candidate, digest = _candidate(tmp_path)

    result = promote_snapshot(
        tmp_path,
        candidate,
        {
            "target_id": "example",
            "expected_sha256": "0" * 64,
            "candidate_sha256": digest,
        },
    )

    assert result == {
        "action": "snapshot_conflict",
        "applied": False,
        "current_sha256": hashlib.sha256(current).hexdigest(),
    }
    assert (snapshots / "example.txt").read_bytes() == current


def test_promote_snapshot_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    candidate, _ = _candidate(tmp_path)

    with pytest.raises(WorkflowError, match="does not match"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": "0" * 64,
            },
        )


def test_promote_snapshot_rejects_candidate_outside_runtime(tmp_path: Path) -> None:
    candidate = tmp_path.parent / "outside.txt"
    candidate.write_text("next\n")
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkflowError, match="runtime_dir/candidates"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": digest,
            },
        )


def test_promote_snapshot_rejects_symlink_candidate(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    target = candidates / "target.txt"
    target.write_text("next\n")
    candidate = candidates / "link.txt"
    candidate.symlink_to(target)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkflowError, match="regular non-symlink"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": digest,
            },
        )


def test_promote_snapshot_requires_existing_runtime_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkflowError, match="existing directory"):
        promote_snapshot(
            missing,
            missing / "candidates" / "example.txt",
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": "0" * 64,
            },
        )


def test_main_returns_error_for_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow.sys, "stdin", io.StringIO("{"))
    monkeypatch.setattr(workflow.sys, "argv", ["workflow.py", "validate-targets"])

    assert workflow.main() == 2
