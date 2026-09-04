"""Tests for the local workflow helpers."""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING

import pytest
import workflow
from workflow import WorkflowError, promote_snapshot, validate_targets

if TYPE_CHECKING:
    from pathlib import Path


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


def test_validate_targets_applies_defaults() -> None:
    assert validate_targets({"targets": [_target()]}) == {
        "targets": [
            {
                "target_id": "example",
                "name": "Example",
                "url": "https://example.com/",
                "enabled": True,
                "action": "monitor",
                "watch_focus": "",
                "fetch_mode": "static",
            }
        ]
    }


def test_validate_targets_marks_disabled_target() -> None:
    result = validate_targets({"targets": [_target(enabled=False)]})

    assert result["targets"][0]["action"] == "skip_disabled"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"targets": [], "extra": True},
    ],
)
def test_validate_targets_rejects_invalid_request_shape(
    payload: dict[str, object],
) -> None:
    with pytest.raises(WorkflowError, match="only targets"):
        validate_targets(payload)


def test_validate_targets_rejects_unknown_target_fields() -> None:
    with pytest.raises(WorkflowError, match="unsupported fields"):
        validate_targets({"targets": [_target(extra=True)]})


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

    assert result["action"] == "snapshot_promoted"
    assert result["sha256"] == digest
    assert (tmp_path / "snapshots" / "example.txt").read_text() == "next\n"


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
    (tmp_path / "candidates").mkdir()
    candidate = tmp_path.parent / "outside.txt"
    candidate.write_text("next\n")

    with pytest.raises(WorkflowError, match="runtime_dir/candidates"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": hashlib.sha256(b"next\n").hexdigest(),
            },
        )


def test_promote_snapshot_rejects_symlink_candidate(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    target = candidates / "target.txt"
    target.write_text("next\n")
    candidate = candidates / "link.txt"
    candidate.symlink_to(target)

    with pytest.raises(WorkflowError, match="regular non-symlink"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": hashlib.sha256(b"next\n").hexdigest(),
            },
        )


@pytest.mark.parametrize("directory_name", ["candidates", "snapshots"])
def test_promote_snapshot_rejects_symlinked_runtime_directory(
    tmp_path: Path,
    directory_name: str,
) -> None:
    outside = tmp_path / f"outside-{directory_name}"
    outside.mkdir()
    (tmp_path / directory_name).symlink_to(outside, target_is_directory=True)

    if directory_name == "candidates":
        candidate = outside / "example.txt"
        candidate.write_text("next\n")
    else:
        candidate, _ = _candidate(tmp_path)

    with pytest.raises(WorkflowError, match=f"runtime_dir/{directory_name}"):
        promote_snapshot(
            tmp_path,
            candidate,
            {
                "target_id": "example",
                "expected_sha256": None,
                "candidate_sha256": hashlib.sha256(b"next\n").hexdigest(),
            },
        )


def test_promote_snapshot_is_idempotent(tmp_path: Path) -> None:
    candidate, digest = _candidate(tmp_path)
    request = {
        "target_id": "example",
        "expected_sha256": None,
        "candidate_sha256": digest,
    }

    promote_snapshot(tmp_path, candidate, request)
    result = promote_snapshot(tmp_path, candidate, request)

    assert result["already"] is True
    assert result["sha256"] == digest


def test_main_returns_error_for_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow.sys, "stdin", io.StringIO("{"))
    monkeypatch.setattr(workflow.sys, "argv", ["workflow.py", "validate-targets"])

    assert workflow.main() == 2
