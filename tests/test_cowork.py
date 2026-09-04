"""Tests for the Cowork-facing CSV workflow."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, cast

import cowork
import pytest
from cowork import CoworkError, check, finalize, load_targets

if TYPE_CHECKING:
    from pathlib import Path


def _write_targets(path: Path, rows: str) -> None:
    path.write_text(f"name,url,watch_focus,enabled\n{rows}", encoding="utf-8")


def _changed_result(
    current: str = "new\n", previous: str = "old\n"
) -> dict[str, object]:
    return {
        "status": "changed",
        "sha256": hashlib.sha256(current.encode()).hexdigest(),
        "previous_sha256": hashlib.sha256(previous.encode()).hexdigest(),
        "diff": "--- previous\n+++ current\n-old\n+new",
        "diff_truncated": False,
    }


def test_load_targets_normalizes_csv_and_generates_stable_ids(tmp_path: Path) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Example,https://example.com/,pricing,true\n"
        "Disabled,https://example.org/,,false\n",
    )

    targets = load_targets(tmp_path)

    assert [target["action"] for target in targets] == ["monitor", "skip_disabled"]
    assert targets[0]["watch_focus"] == "pricing"
    assert str(targets[0]["target_id"]).startswith("example-com-")
    first_id = targets[0]["target_id"]

    _write_targets(
        tmp_path / "targets.csv",
        "Renamed,https://example.com/,pricing,true\n",
    )
    assert load_targets(tmp_path)[0]["target_id"] == first_id


@pytest.mark.parametrize(
    ("header", "row", "message"),
    [
        ("name,watch_focus\n", "Example,pricing\n", "requires name and url"),
        ("name,url,extra\n", "Example,https://example.com/,x\n", "unsupported"),
        (
            "name,url,watch_focus,enabled\n",
            "Example,https://example.com/,,yes\n",
            "enabled must be true or false",
        ),
    ],
)
def test_load_targets_rejects_invalid_csv(
    tmp_path: Path, header: str, row: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(header + row, encoding="utf-8")

    with pytest.raises(CoworkError, match=message):
        load_targets(tmp_path)


def test_load_targets_rejects_duplicate_urls(tmp_path: Path) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "One,https://example.com/,,true\nTwo,https://example.com/,,true\n",
    )

    with pytest.raises(cowork.workflow.WorkflowError, match="duplicate_target_id"):
        load_targets(tmp_path)


def test_handle_monitor_result_promotes_baseline(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "candidates"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "example.txt"
    content = "baseline\n"
    candidate.write_text(content)
    target = {"target_id": "example", "name": "Example"}

    result = cowork._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state,
        target,
        candidate,
        {
            "status": "baseline",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    )

    assert result["action"] == "baseline_created"
    assert (state / "snapshots" / "example.txt").read_text() == content
    assert not candidate.exists()


def test_handle_monitor_result_records_changed_candidate(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "candidates"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "example.txt"
    candidate.write_text("new\n")
    (snapshot_dir / "example.txt").write_text("old\n")
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    result = cowork._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, candidate, _changed_result()
    )

    assert result["action"] == "review"
    assert len(str(result["revision"])) == 32
    assert result["watch_focus"] == "pricing"
    pending = json.loads((state / "pending" / "example.json").read_text())
    assert pending["target_id"] == "example"
    assert pending["revision"] == result["revision"]
    assert candidate.exists()


def test_finalize_material_change_writes_report_and_promotes(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "candidates"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "example.txt"
    candidate.write_text("new\n")
    (snapshot_dir / "example.txt").write_text("old\n")
    cowork._write_pending(  # pyright: ignore[reportPrivateUsage]
        state,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "candidate_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff_truncated": False,
        },
    )

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "# Example\n\nPricing changed.\n",
        },
    )

    assert result["action"] == "finalized"
    assert (tmp_path / "reports" / "example.md").exists()
    assert (state / "snapshots" / "example.txt").read_text() == "new\n"
    assert not candidate.exists()
    assert not (state / "pending" / "example.json").exists()


def test_finalize_non_material_truncated_diff_stops(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    (state / "candidates").mkdir(parents=True)
    (state / "candidates" / "example.txt").write_text("new\n")
    cowork._write_pending(  # pyright: ignore[reportPrivateUsage]
        state,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "candidate_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff_truncated": True,
        },
    )

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result == {"action": "manual_review_required", "target_id": "example"}
    assert (state / "pending" / "example.json").exists()


def test_finalize_rejects_stale_review_revision(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "candidates"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "example.txt"
    (snapshot_dir / "example.txt").write_text("old\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "example.md"
    report.write_text("current report\n")
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    candidate.write_text("first\n")
    first = cowork._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, candidate, _changed_result(current="first\n")
    )
    first_revision = str(first["revision"])
    candidate.write_text("second\n")
    second = cowork._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, candidate, _changed_result(current="second\n")
    )
    pending = (state / "pending" / "example.json").read_bytes()

    with pytest.raises(CoworkError, match="revision"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": first_revision,
                "material": True,
                "report": "# Stale\n",
            },
        )

    assert (snapshot_dir / "example.txt").read_text() == "old\n"
    assert report.read_text() == "current report\n"
    assert candidate.read_text() == "second\n"
    assert (state / "pending" / "example.json").read_bytes() == pending
    assert str(second["revision"]) != first_revision


def test_finalize_material_snapshot_conflict_does_not_write_report(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "candidates"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "example.txt"
    candidate.write_text("new\n")
    snapshot = snapshot_dir / "example.txt"
    snapshot.write_text("old\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "example.md"
    report.write_text("previous report\n")
    cowork._write_pending(  # pyright: ignore[reportPrivateUsage]
        state,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "candidate_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff_truncated": False,
        },
    )
    snapshot.write_text("external\n")

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "# Candidate\n",
        },
    )

    assert result == {"action": "snapshot_conflict", "target_id": "example"}
    assert snapshot.read_text() == "external\n"
    assert report.read_text() == "previous report\n"
    assert candidate.read_text() == "new\n"
    assert (state / "pending" / "example.json").exists()


def test_check_batches_targets_and_contains_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Good,https://example.com/,,true\n"
        "Bad,https://example.org/,,true\n"
        "Off,https://example.net/,,false\n",
    )

    def fake_monitor(_state: Path, target: dict[str, object]) -> dict[str, object]:
        if target["name"] == "Bad":
            raise cowork.monitor.MonitorError
        return {
            "action": "unchanged",
            "target_id": target["target_id"],
            "name": target["name"],
        }

    monkeypatch.setattr(cowork, "_monitor_target", fake_monitor)

    result = check(tmp_path)
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert [item["action"] for item in outcomes] == [
        "unchanged",
        "error",
        "skipped",
    ]


def test_main_reports_invalid_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    assert cowork.main(["--workspace", "/missing", "check"]) == 2
    assert "workspace must be an existing directory" in capsys.readouterr().err
