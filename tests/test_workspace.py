"""Tests for the agent-facing CSV workflow."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import hashlib
import json
import os
import runpy
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import workspace
from workspace import WorkspaceError, check, finalize, load_targets

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


_RUN_ID = "20261001T000000Z-deadbeef"


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


def _transaction_paths(state: Path, target_id: str, layout: str) -> tuple[Path, Path]:
    if layout == "grouped":
        directory = state / "pending" / target_id
        return directory / "state.json", directory / "candidate.txt"
    if layout == "legacy":
        return (
            state / "pending" / f"{target_id}.json",
            state / "candidates" / f"{target_id}.txt",
        )
    raise AssertionError


def _write_review_transaction(
    state: Path,
    layout: str,
    *,
    target_id: str = "example",
    revision: str = "a" * 32,
    run_id: str = _RUN_ID,
    include_run_id: bool = True,
    diff_truncated: bool = False,
    baseline: str = "old\n",
    candidate: str = "new\n",
) -> tuple[Path, Path, Path]:
    metadata, candidate_path = _transaction_paths(state, target_id, layout)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(candidate, encoding="utf-8")

    snapshots = state / "snapshots"
    snapshots.mkdir(exist_ok=True)
    snapshot = snapshots / f"{target_id}.txt"
    snapshot.write_text(baseline, encoding="utf-8")

    payload: dict[str, object] = {
        "target_id": target_id,
        "revision": revision,
        "expected_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "diff_truncated": diff_truncated,
    }
    if include_run_id:
        payload["run_id"] = run_id
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    return metadata, candidate_path, snapshot


def test_load_targets_normalizes_csv_and_generates_stable_ids(tmp_path: Path) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Example,https://example.com/,pricing,true\n"
        "Disabled,https://example.org/,,false\n",
    )

    targets = load_targets(tmp_path)

    assert [target["action"] for target in targets] == ["monitor", "skip_disabled"]
    assert targets[0]["watch_focus"] == "pricing"
    assert "fetch_mode" not in targets[0]
    assert str(targets[0]["target_id"]).startswith("example-com-")
    first_id = targets[0]["target_id"]

    _write_targets(
        tmp_path / "targets.csv",
        "Renamed,https://example.com/,pricing,true\n",
    )
    assert load_targets(tmp_path)[0]["target_id"] == first_id


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        ("name,watch_focus\n", "Example,pricing\n", "requires name and url"),
        ("name,url,extra\n", "Example,https://example.com/,x\n", "unsupported"),
        (
            "name,url,watch_focus,enabled\n",
            "Example,https://example.com/,,yes\n",
            "enabled must be true or false",
        ),
        (
            "name,url,watch_focus,enabled\n",
            ",https://example.com/,,true\n",
            "name must be non-empty",
        ),
        (
            "name,url,watch_focus,enabled\n",
            "One,https://example.com/,,true\nTwo,https://example.com/,,true\n",
            "duplicate_target_id",
        ),
    ],
    ids=[
        "missing-required-column",
        "unsupported-column",
        "invalid-enabled",
        "empty-name",
        "duplicate-url",
    ],
)
def test_load_targets_rejects_invalid_csv(
    tmp_path: Path, header: str, rows: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(header + rows, encoding="utf-8")

    with pytest.raises(WorkspaceError, match=message):
        load_targets(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "https://user:pass@example.com/",
        "https://example.com/?token=secret",
        "https://example.com/#fragment",
    ],
    ids=["non-http-scheme", "credentials", "query-secret", "fragment"],
)
def test_load_targets_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    _write_targets(tmp_path / "targets.csv", f"Example,{url},,true\n")

    with pytest.raises(WorkspaceError):
        load_targets(tmp_path)


def test_handle_monitor_result_promotes_baseline(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "pending" / "example"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "candidate.txt"
    content = "baseline\n"
    candidate.write_text(content)
    target = {"target_id": "example", "name": "Example"}

    result = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state,
        target,
        {
            "status": "baseline",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        _RUN_ID,
    )

    assert result["action"] == "baseline_created"
    assert (state / "snapshots" / "example.txt").read_text() == content
    assert not candidate.exists()


def test_handle_monitor_result_records_changed_candidate(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "pending" / "example"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "candidate.txt"
    candidate.write_text("new\n")
    (snapshot_dir / "example.txt").write_text("old\n")
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    result = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, _changed_result(), _RUN_ID
    )

    assert result["action"] == "review"
    assert len(str(result["revision"])) == 32
    assert result["watch_focus"] == "pricing"
    pending = json.loads((state / "pending" / "example" / "state.json").read_text())
    assert pending["target_id"] == "example"
    assert pending["run_id"] == _RUN_ID
    assert pending["revision"] == result["revision"]
    assert candidate.exists()


@pytest.mark.parametrize(
    ("layout", "include_run_id"),
    [
        ("grouped", True),
        ("grouped", False),
        ("legacy", True),
        ("legacy", False),
    ],
    ids=[
        "grouped-six-field",
        "grouped-five-field",
        "legacy-six-field",
        "legacy-five-field",
    ],
)
def test_finalize_material_review_promotes_and_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    include_run_id: bool,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(
        state, layout, include_run_id=include_run_id
    )
    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "## Example\n\nPricing changed.\n",
        },
    )

    report = tmp_path / "reports" / f"{_RUN_ID}.md"
    assert result["action"] == "finalized"
    assert result["report_path"] == str(report)
    assert report.exists()
    assert "Pricing changed." in report.read_text()
    assert snapshot.read_text() == "new\n"
    assert not candidate.exists()
    assert not metadata.exists()
    assert not (state / "pending" / "example").exists()


@pytest.mark.parametrize("layout", ["grouped", "legacy"], ids=["grouped", "legacy"])
def test_finalize_non_material_truncated_diff_stops(
    tmp_path: Path, layout: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(
        state, layout, diff_truncated=True
    )

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result == {"action": "manual_review_required", "target_id": "example"}
    assert metadata.exists()
    assert candidate.exists()
    assert snapshot.read_text() == "old\n"


@pytest.mark.parametrize("layout", ["grouped", "legacy"], ids=["grouped", "legacy"])
def test_finalize_non_material_review_promotes_without_report(
    tmp_path: Path, layout: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, layout)

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result == {"action": "finalized", "target_id": "example", "material": False}
    assert snapshot.read_text() == "new\n"
    assert not (tmp_path / "reports").exists()
    assert not metadata.exists()
    assert not candidate.exists()


@pytest.mark.parametrize(
    ("layout", "failure"),
    [
        ("grouped", "partial-delete"),
        ("legacy", "partial-delete"),
        ("grouped", "parent-fsync"),
        ("legacy", "parent-fsync"),
    ],
    ids=[
        "grouped-partial-delete",
        "legacy-partial-delete",
        "grouped-parent-fsync",
        "legacy-parent-fsync",
    ],
)
def test_finalize_cleanup_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    failure: str,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, layout)
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nRetry cleanup.\n",
    }
    failed = False

    if failure == "partial-delete" and layout == "grouped":
        original_rmtree = workspace.shutil.rmtree

        def partially_remove_group(path: Path) -> None:
            nonlocal failed
            if path == candidate.parent and not failed:
                failed = True
                candidate.unlink()
                raise PermissionError
            original_rmtree(path)

        monkeypatch.setattr(workspace.shutil, "rmtree", partially_remove_group)
    elif failure == "partial-delete":
        original_unlink = Path.unlink

        def partially_remove_legacy(path: Path, *, missing_ok: bool = False) -> None:
            nonlocal failed
            original_unlink(path, missing_ok=missing_ok)
            if path == candidate and not failed:
                failed = True
                raise PermissionError

        monkeypatch.setattr(Path, "unlink", partially_remove_legacy)
    else:
        original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
        pending_dir = state / "pending"

        def fail_pending_fsync(path: Path) -> None:
            nonlocal failed
            if path == pending_dir and not failed:
                failed = True
                raise OSError
            original_fsync(path)

        monkeypatch.setattr(workspace, "_fsync_directory", fail_pending_fsync)

    with pytest.raises(WorkspaceError, match=r"pending transaction|fsync"):
        finalize(tmp_path, decision)

    assert failed
    assert (state / ".pending-recovery" / "example.json").exists()
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert "Retry cleanup." in (tmp_path / "reports" / f"{_RUN_ID}.md").read_text(
        encoding="utf-8"
    )

    result = finalize(tmp_path, decision)

    assert result == {
        "action": "finalized",
        "target_id": "example",
        "material": True,
        "report_path": str(tmp_path / "reports" / f"{_RUN_ID}.md"),
    }
    assert not metadata.exists()
    assert not candidate.exists()
    assert not (state / "pending" / "example").exists()
    assert not (state / ".pending-recovery" / "example.json").exists()


def test_finalize_rejects_stale_review_revision(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "pending" / "example"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "candidate.txt"
    (snapshot_dir / "example.txt").write_text("old\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / f"{_RUN_ID}.md"
    original_report = (
        f"# Web Update Monitor Report\n\nRun: `{_RUN_ID}`\n\ncurrent report\n"
    )
    report.write_text(original_report)
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    candidate.write_text("first\n")
    first = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, _changed_result(current="first\n"), _RUN_ID
    )
    first_revision = str(first["revision"])
    candidate.write_text("second\n")
    second = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, _changed_result(current="second\n"), _RUN_ID
    )
    pending = (state / "pending" / "example" / "state.json").read_bytes()

    with pytest.raises(WorkspaceError, match="revision"):
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
    assert report.read_text() == original_report
    assert candidate.read_text() == "second\n"
    assert (state / "pending" / "example" / "state.json").read_bytes() == pending
    assert str(second["revision"]) != first_revision


@pytest.mark.parametrize("layout", ["grouped", "legacy"], ids=["grouped", "legacy"])
def test_finalize_material_snapshot_conflict_does_not_write_report(
    tmp_path: Path, layout: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, layout)
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / f"{_RUN_ID}.md"
    original_report = (
        f"# Web Update Monitor Report\n\nRun: `{_RUN_ID}`\n\nprevious report\n"
    )
    report.write_text(original_report)
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
    assert report.read_text() == original_report
    assert candidate.read_text() == "new\n"
    assert metadata.exists()


@pytest.mark.parametrize(
    "include_run_id", [True, False], ids=["six-field", "five-field"]
)
def test_finalize_rejects_legacy_revision_without_clearing_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_run_id: bool
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(
        state, "legacy", include_run_id=include_run_id
    )
    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)

    with pytest.raises(WorkspaceError, match="revision"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "b" * 32,
                "material": True,
                "report": "## Stale\n",
            },
        )

    persisted = json.loads(metadata.read_text(encoding="utf-8"))
    assert persisted["run_id"] == _RUN_ID
    assert snapshot.read_text() == "old\n"
    assert candidate.read_text() == "new\n"


@pytest.mark.parametrize(
    ("layout", "include_run_id"),
    [
        ("grouped", True),
        ("grouped", False),
        ("legacy", True),
        ("legacy", False),
    ],
    ids=[
        "grouped-six-field",
        "grouped-five-field",
        "legacy-six-field",
        "legacy-five-field",
    ],
)
def test_finalize_report_failure_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    include_run_id: bool,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(
        state, layout, include_run_id=include_run_id
    )
    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)
    write_report = workspace._write_report  # pyright: ignore[reportPrivateUsage]
    write_attempts = 0

    def fail_once(root: Path, run_id: str, target_id: str, report: str) -> Path:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            message = "cannot write report"
            raise WorkspaceError(message)
        return write_report(root, run_id, target_id, report)

    monkeypatch.setattr(workspace, "_write_report", fail_once)  # pyright: ignore[reportPrivateUsage]
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nPricing changed.\n",
    }

    with pytest.raises(WorkspaceError, match="cannot write report"):
        finalize(tmp_path, decision)

    assert snapshot.read_text() == "new\n"
    assert metadata.exists()
    assert candidate.exists()
    assert json.loads(metadata.read_text(encoding="utf-8"))["run_id"] == _RUN_ID
    assert not (tmp_path / "reports").exists()

    result = finalize(tmp_path, decision)

    report_path = tmp_path / "reports" / f"{_RUN_ID}.md"
    report_content = report_path.read_text(encoding="utf-8")
    assert result["report_path"] == str(report_path)
    assert write_attempts == 2
    assert report_content.count("<!-- wsum:target example:start -->") == 1
    assert not metadata.exists()
    assert not candidate.exists()


def test_finalize_prefers_grouped_transaction_and_cleans_matching_legacy_files(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    grouped_metadata, grouped_candidate, snapshot = _write_review_transaction(
        state, "grouped", candidate="grouped\n"
    )
    legacy_metadata, legacy_candidate, _ = _write_review_transaction(
        state, "legacy", revision="b" * 32, candidate="legacy\n"
    )
    unrelated_metadata, unrelated_candidate, unrelated_snapshot = (
        _write_review_transaction(
            state,
            "legacy",
            target_id="other",
            revision="c" * 32,
            baseline="previous other\n",
            candidate="next other\n",
        )
    )

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "## Example\n\nGrouped state won.\n",
        },
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text() == "grouped\n"
    assert unrelated_snapshot.read_text() == "previous other\n"
    assert not grouped_metadata.exists()
    assert not grouped_candidate.exists()
    assert not legacy_metadata.exists()
    assert not legacy_candidate.exists()
    assert unrelated_metadata.exists()
    assert unrelated_candidate.exists()


def test_cleanup_failure_keeps_grouped_transaction_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    grouped_metadata, grouped_candidate, _ = _write_review_transaction(
        state, "grouped", candidate="grouped\n"
    )
    legacy_metadata, legacy_candidate, _ = _write_review_transaction(
        state, "legacy", revision="b" * 32, candidate="legacy\n"
    )
    original_unlink = Path.unlink

    def fail_legacy_candidate_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == legacy_candidate:
            raise PermissionError
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_legacy_candidate_unlink)

    with pytest.raises(WorkspaceError, match="cannot remove pending transaction"):
        workspace._remove_pending(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )

    assert grouped_metadata.exists()
    assert grouped_candidate.exists()
    assert legacy_metadata.exists()
    assert legacy_candidate.exists()
    pending = workspace._read_pending(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    assert pending["revision"] == "a" * 32

    monkeypatch.setattr(Path, "unlink", original_unlink)
    workspace._remove_pending(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    assert not grouped_metadata.exists()
    assert not grouped_candidate.exists()
    assert not legacy_metadata.exists()
    assert not legacy_candidate.exists()


def _grouped_candidate_without_metadata(
    state: Path, legacy_metadata: Path, legacy_candidate: Path, tmp_path: Path
) -> Path:
    del legacy_metadata, tmp_path
    grouped = state / "pending" / "example"
    grouped.mkdir()
    candidate = grouped / "candidate.txt"
    candidate.write_bytes(legacy_candidate.read_bytes())
    return legacy_candidate


def _grouped_metadata_without_candidate(
    state: Path, legacy_metadata: Path, legacy_candidate: Path, tmp_path: Path
) -> Path:
    del tmp_path
    grouped = state / "pending" / "example"
    grouped.mkdir()
    (grouped / "state.json").write_bytes(legacy_metadata.read_bytes())
    return legacy_candidate


def _symlink_grouped_directory(
    state: Path, legacy_metadata: Path, legacy_candidate: Path, tmp_path: Path
) -> Path:
    del legacy_metadata, legacy_candidate
    outside = tmp_path / "outside-grouped-transaction"
    outside.mkdir()
    marker = outside / "untouched.txt"
    marker.write_text("outside\n", encoding="utf-8")
    (state / "pending" / "example").symlink_to(outside, target_is_directory=True)
    return marker


@pytest.mark.parametrize(
    ("grouped_setup", "message"),
    [
        (
            _grouped_candidate_without_metadata,
            "no valid pending decision",
        ),
        (_grouped_metadata_without_candidate, "cannot stat candidate"),
        (_symlink_grouped_directory, "non-symlink directory"),
    ],
    ids=["missing-metadata", "missing-candidate", "symlinked-directory"],
)
def test_finalize_does_not_mix_grouped_and_legacy_paths(
    tmp_path: Path,
    grouped_setup: Callable[[Path, Path, Path, Path], Path],
    message: str,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    legacy_metadata, legacy_candidate, snapshot = _write_review_transaction(
        state, "legacy"
    )
    marker = grouped_setup(state, legacy_metadata, legacy_candidate, tmp_path)
    marker_data = marker.read_bytes()

    with pytest.raises(WorkspaceError, match=message):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": True,
                "report": "## Example\n",
            },
        )

    assert marker.read_bytes() == marker_data
    assert snapshot.read_text() == "old\n"
    assert legacy_metadata.exists()
    assert legacy_candidate.exists()


def _replace_with_legacy_symlink(path: Path) -> Path:
    outside = path.parents[2] / f"outside-{path.name}"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    return outside


def _replace_with_legacy_directory(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _replace_with_legacy_fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path)


@pytest.mark.parametrize(
    "make_unsafe",
    [
        _replace_with_legacy_symlink,
        _replace_with_legacy_directory,
        _replace_with_legacy_fifo,
    ],
    ids=["symlink", "directory", "fifo"],
)
def test_finalize_rejects_non_regular_legacy_metadata(
    tmp_path: Path, make_unsafe: Callable[[Path], Path | None]
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "legacy")
    outside = make_unsafe(metadata)
    outside_data = None if outside is None else outside.read_bytes()

    with pytest.raises(WorkspaceError, match="regular non-symlink file"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": True,
                "report": "## Example\n",
            },
        )

    if outside is not None:
        assert outside.read_bytes() == outside_data
    assert candidate.read_text(encoding="utf-8") == "new\n"
    assert snapshot.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    "make_unsafe",
    [
        _replace_with_legacy_symlink,
        _replace_with_legacy_directory,
        _replace_with_legacy_fifo,
    ],
    ids=["symlink", "directory", "fifo"],
)
def test_finalize_rejects_non_regular_legacy_candidate(
    tmp_path: Path, make_unsafe: Callable[[Path], Path | None]
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "legacy")
    outside = make_unsafe(candidate)

    with pytest.raises(WorkspaceError, match="regular non-symlink file"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": True,
                "report": "## Example\n",
            },
        )

    if outside is not None:
        assert outside.read_text(encoding="utf-8") == "new\n"
    assert metadata.exists()
    assert snapshot.read_text(encoding="utf-8") == "old\n"


def test_finalize_rejects_legacy_candidate_hash_mismatch(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "legacy")
    candidate.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="candidate_sha256 does not match"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": True,
                "report": "## Example\n",
            },
        )

    assert metadata.exists()
    assert candidate.read_text(encoding="utf-8") == "tampered\n"
    assert snapshot.read_text(encoding="utf-8") == "old\n"


def test_finalize_rejects_symlinked_legacy_candidate_directory(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "legacy")
    candidates = state / "candidates"
    candidate.unlink()
    candidates.rmdir()
    outside = tmp_path / "outside-candidates"
    outside.mkdir()
    outside_candidate = outside / "example.txt"
    outside_candidate.write_text("outside\n", encoding="utf-8")
    candidates.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": True,
                "report": "## Example\n",
            },
        )

    assert outside_candidate.read_text(encoding="utf-8") == "outside\n"
    assert metadata.exists()
    assert candidates.is_symlink()
    assert snapshot.read_text(encoding="utf-8") == "old\n"


def test_check_batches_targets_and_contains_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Good,https://example.com/,,true\n"
        "Bad,https://example.org/,,true\n"
        "Off,https://example.net/,,false\n",
    )

    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)

    def fake_monitor(
        _state: Path, target: dict[str, object], run_id: str
    ) -> dict[str, object]:
        assert run_id == _RUN_ID
        if target["name"] == "Bad":
            raise workspace.monitor.MonitorError
        return {
            "action": "unchanged",
            "target_id": target["target_id"],
            "name": target["name"],
        }

    monkeypatch.setattr(workspace, "_monitor_target", fake_monitor)

    result = check(tmp_path)
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert result["run_id"] == _RUN_ID
    assert [item["action"] for item in outcomes] == [
        "unchanged",
        "error",
        "skipped",
    ]


def test_check_failure_preserves_legacy_pending_for_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,pricing,true\n"
    )
    target_id = str(load_targets(tmp_path)[0]["target_id"])
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(
        state, "legacy", target_id=target_id
    )

    def fail_monitor(_args: argparse.Namespace) -> dict[str, object]:
        raise workspace.monitor.MonitorError

    monkeypatch.setattr(workspace.monitor, "run", fail_monitor)

    result = check(tmp_path)
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert outcomes[0]["action"] == "error"
    assert "error" in outcomes[0]
    assert metadata.exists()
    assert candidate.read_text(encoding="utf-8") == "new\n"
    assert not (state / "pending" / target_id).exists()

    pending = json.loads(metadata.read_text(encoding="utf-8"))
    finalized = finalize(
        tmp_path,
        {
            "target_id": target_id,
            "revision": pending["revision"],
            "material": True,
            "report": "## Example\n\nReviewed.\n",
        },
    )

    assert finalized["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert not metadata.exists()
    assert not candidate.exists()


@pytest.mark.parametrize(
    "failure_point",
    ["staged-output-fsync", "pending-candidate-fsync"],
    ids=["staged-output-fsync", "pending-candidate-fsync"],
)
def test_monitor_update_failure_keeps_previous_review_finalizable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "grouped")
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    failure_path: Path | None = None
    failed = False
    failure_message = "injected directory fsync failure"

    def fail_once(path: Path) -> None:
        nonlocal failed
        if path == failure_path and not failed:
            failed = True
            raise OSError(failure_message)
        original_fsync(path)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        nonlocal failure_path
        staged = Path(args.output)
        replacement = staged.with_suffix(".replacement")
        replacement.write_text("third\n", encoding="utf-8")
        replacement.replace(staged)
        failure_path = (
            staged.parent
            if failure_point == "staged-output-fsync"
            else candidate.parent
        )
        workspace._fsync_directory(  # pyright: ignore[reportPrivateUsage]
            staged.parent
        )
        return _changed_result(current="third\n")

    monkeypatch.setattr(workspace, "_fsync_directory", fail_once)
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    with pytest.raises((OSError, WorkspaceError), match="fsync"):
        workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]

    assert failed
    assert metadata.exists()
    assert candidate.read_text(encoding="utf-8") == "new\n"

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert not metadata.exists()
    assert not candidate.exists()


def test_staging_cleanup_failure_does_not_commit_monitor_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "grouped")
    original_metadata = metadata.read_bytes()
    cleanup_error = "injected staging cleanup failure"
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    class FailingTemporaryDirectory:
        def __init__(self, *, prefix: str, **kwargs: str | Path) -> None:
            self.path = Path(kwargs["dir"]) / f"{prefix}cleanup-failure"
            self.path.mkdir(mode=0o700)

        def __enter__(self) -> str:
            return str(self.path)

        def __exit__(
            self, _exc_type: object, _exc_value: object, _traceback: object
        ) -> None:
            workspace.shutil.rmtree(self.path)
            raise OSError(cleanup_error)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("third\n", encoding="utf-8")
        return _changed_result(current="third\n")

    monkeypatch.setattr(
        workspace.tempfile, "TemporaryDirectory", FailingTemporaryDirectory
    )
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    with pytest.raises(OSError, match=cleanup_error):
        workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]

    assert metadata.read_bytes() == original_metadata
    assert candidate.read_text(encoding="utf-8") == "new\n"
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"


def test_failed_commit_marker_keeps_undo_available_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, _ = _write_review_transaction(state, "grouped")
    recovery_dir = state / ".pending-recovery"
    commit_marker = recovery_dir / "example.json.commit"
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    original_write = workspace._write_temporary_file  # pyright: ignore[reportPrivateUsage]
    commit_sync_failed = False
    rollback_write_failed = False
    commit_sync_error = "injected commit marker fsync failure"
    rollback_write_error = "injected rollback file creation failure"

    def fail_commit_sync(path: Path) -> None:
        nonlocal commit_sync_failed
        if path == recovery_dir and commit_marker.exists() and not commit_sync_failed:
            commit_sync_failed = True
            raise OSError(commit_sync_error)
        original_fsync(path)

    def fail_rollback_write(destination: Path, data: bytes, description: str) -> Path:
        nonlocal rollback_write_failed
        if (
            commit_sync_failed
            and destination == candidate
            and data == b"new\n"
            and not rollback_write_failed
        ):
            rollback_write_failed = True
            raise OSError(rollback_write_error)
        return original_write(destination, data, description)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_commit_sync)
    monkeypatch.setattr(workspace, "_write_temporary_file", fail_rollback_write)

    payload = {
        "target_id": "example",
        "run_id": _RUN_ID,
        "revision": "b" * 32,
        "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
        "candidate_sha256": hashlib.sha256(b"third\n").hexdigest(),
        "diff_truncated": False,
    }
    with pytest.raises(WorkspaceError, match="rollback could not be completed"):
        workspace._write_pending_transaction(  # pyright: ignore[reportPrivateUsage]
            state, payload, b"third\n"
        )

    assert commit_sync_failed
    assert rollback_write_failed
    assert metadata.exists()
    assert commit_marker.exists() is False
    assert (recovery_dir / "example.json").exists()

    monkeypatch.undo()
    workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    assert json.loads(metadata.read_text(encoding="utf-8"))["revision"] == "a" * 32
    assert candidate.read_text(encoding="utf-8") == "new\n"
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"


def _leave_ambiguous_replacement(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "target_id": "example",
        "run_id": _RUN_ID,
        "revision": "b" * 32,
        "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
        "candidate_sha256": hashlib.sha256(b"third\n").hexdigest(),
        "diff_truncated": False,
    }
    retirement_error = "injected undo retirement failure"

    def fail_undo_retirement(_state: Path, _target_id: str) -> None:
        raise WorkspaceError(retirement_error)

    monkeypatch.setattr(workspace, "_retire_recovery_record", fail_undo_retirement)
    workspace._write_pending_transaction(  # pyright: ignore[reportPrivateUsage]
        state, payload, b"third\n"
    )


@pytest.mark.parametrize(
    ("revision", "expected_snapshot"),
    [("a" * 32, "new\n"), ("b" * 32, "third\n")],
    ids=["retain-previous-review", "accept-committed-review"],
)
def test_finalize_resolves_ambiguous_commit_by_requested_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
    expected_snapshot: str,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    _, _, snapshot = _write_review_transaction(state, "grouped")
    _leave_ambiguous_replacement(state, monkeypatch)
    assert (state / ".pending-recovery" / "example.json").exists()
    assert (state / ".pending-recovery" / "example.json.commit").exists()
    with pytest.raises(WorkspaceError, match="requires a decision revision"):
        workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )

    monkeypatch.undo()
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": revision, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == expected_snapshot


def test_finalize_unrelated_revision_preserves_ambiguous_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "grouped")
    _leave_ambiguous_replacement(state, monkeypatch)
    monkeypatch.undo()
    original_metadata = metadata.read_bytes()
    original_candidate = candidate.read_bytes()
    recovery_dir = state / ".pending-recovery"

    with pytest.raises(WorkspaceError, match="does not match pending replacement"):
        finalize(
            tmp_path,
            {"target_id": "example", "revision": "c" * 32, "material": False},
        )

    assert snapshot.read_text(encoding="utf-8") == "old\n"
    assert metadata.read_bytes() == original_metadata
    assert candidate.read_bytes() == original_candidate
    assert (recovery_dir / "example.json").exists()
    assert (recovery_dir / "example.json.commit").exists()

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "b" * 32, "material": False},
    )
    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "third\n"


def test_finalize_rejects_symlinked_pending_ancestor_with_ambiguous_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    _write_review_transaction(state, "grouped")
    _leave_ambiguous_replacement(state, monkeypatch)
    monkeypatch.undo()

    target_dir = state / "pending" / "example"
    moved_target = state / "pending" / "example-moved"
    target_dir.replace(moved_target)
    target_dir.symlink_to(moved_target, target_is_directory=True)
    recovery_dir = state / ".pending-recovery"

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        finalize(
            tmp_path,
            {"target_id": "example", "revision": "b" * 32, "material": False},
        )

    assert (recovery_dir / "example.json").exists()
    assert (recovery_dir / "example.json.commit").exists()
    target_dir.unlink()
    moved_target.replace(target_dir)
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"


def test_finalize_recovers_interrupted_pending_replacement(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, snapshot = _write_review_transaction(state, "grouped")
    undo = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, undo
    )
    candidate.write_text("uncommitted replacement\n", encoding="utf-8")

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert not metadata.exists()
    assert not candidate.exists()
    assert not (state / ".pending-recovery" / "example.json").exists()


def test_recovery_removes_temporaries_from_interrupted_initial_write(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    undo = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, undo
    )
    target_dir = state / "pending" / "example"
    target_dir.mkdir()
    (target_dir / ".candidate.txt.crash.tmp").write_text("partial", encoding="utf-8")
    (target_dir / ".state.json.crash.tmp").write_text("partial", encoding="utf-8")

    workspace._recover_pending(state, "example")  # pyright: ignore[reportPrivateUsage]

    assert not target_dir.exists()
    assert not (state / ".pending-recovery" / "example.json").exists()


@pytest.mark.parametrize(
    ("unsafe_record", "message"),
    [
        ("symlink", "non-symlink"),
        ("malformed", "invalid"),
        ("permissive-mode", "private"),
    ],
    ids=["symlink", "malformed", "permissive-mode"],
)
def test_pending_recovery_rejects_unsafe_record(
    tmp_path: Path, unsafe_record: str, message: str
) -> None:
    state = tmp_path / ".wsum"
    recovery_dir = state / ".pending-recovery"
    recovery_dir.mkdir(parents=True, mode=0o700)
    record = recovery_dir / "example.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    if unsafe_record == "symlink":
        record.symlink_to(outside)
    elif unsafe_record == "malformed":
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o600)
    else:
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o644)

    with pytest.raises(WorkspaceError, match=message):
        workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )


def test_finalize_rejects_decision_mismatch_for_pending_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    metadata, candidate, _ = _write_review_transaction(state, "grouped")
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nOriginal decision.\n",
    }

    error_message = "injected cleanup failure"

    def fail_cleanup(_state: Path, _target_id: str) -> None:
        raise WorkspaceError(error_message)

    monkeypatch.setattr(workspace, "_remove_pending", fail_cleanup)
    with pytest.raises(WorkspaceError, match=error_message):
        finalize(tmp_path, decision)

    with pytest.raises(WorkspaceError, match="does not match pending cleanup"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": False,
            },
        )

    assert metadata.exists()
    assert candidate.exists()
    assert (state / ".pending-recovery" / "example.json").exists()

    monkeypatch.undo()
    result = finalize(tmp_path, decision)

    assert result["action"] == "finalized"
    assert not metadata.exists()
    assert not candidate.exists()


def test_check_syncs_new_state_and_grouped_pending_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,pricing,true\n"
    )
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    fsynced: list[Path] = []

    def record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync(path)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("new\n", encoding="utf-8")
        return _changed_result(current="new\n")

    monkeypatch.setattr(workspace, "_fsync_directory", record_fsync)
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    result = check(tmp_path)

    target_id = str(load_targets(tmp_path)[0]["target_id"])
    state = tmp_path / ".wsum"
    pending = state / "pending"
    assert result["targets"][0]["action"] == "review"  # type: ignore[index]
    assert tmp_path in fsynced
    assert state in fsynced
    assert pending in fsynced
    assert pending / target_id in fsynced
    assert state / ".pending-recovery" in fsynced
    assert (pending / target_id / "state.json").exists()


def test_recovery_directory_parent_fsync_retries_after_creation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    _write_review_transaction(state, "grouped")
    record = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    state_fsyncs = 0
    parent_fsync_error = "injected recovery parent fsync failure"

    def fail_once(path: Path) -> None:
        nonlocal state_fsyncs
        if path == state:
            state_fsyncs += 1
            if state_fsyncs == 1:
                raise OSError(parent_fsync_error)
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_once)
    with pytest.raises(WorkspaceError, match="fsync parent directory"):
        workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
            state, record
        )

    recovery_dir = state / ".pending-recovery"
    assert recovery_dir.is_dir()
    assert not (recovery_dir / "example.json").exists()
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, record
    )

    assert state_fsyncs == 2
    assert (recovery_dir / "example.json").is_file()


def test_check_migrates_legacy_pending_on_successful_changed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_id = "example"
    state = tmp_path / ".wsum"
    state.mkdir()
    legacy_metadata, legacy_candidate, _ = _write_review_transaction(
        state, "legacy", target_id=target_id
    )

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("third\n", encoding="utf-8")
        return _changed_result(current="third\n")

    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)
    target = {
        "target_id": target_id,
        "name": "Example",
        "url": "https://example.com/",
        "watch_focus": "pricing",
    }

    result = workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]

    grouped = state / "pending" / target_id
    pending = json.loads((grouped / "state.json").read_text(encoding="utf-8"))
    assert result["action"] == "review"
    assert pending["revision"] == result["revision"]
    assert (grouped / "candidate.txt").read_text(encoding="utf-8") == "third\n"
    assert not legacy_metadata.exists()
    assert not legacy_candidate.exists()


def test_main_reports_invalid_workspace(capsys: pytest.CaptureFixture[str]) -> None:
    assert workspace.main(["--workspace", "/missing", "check"]) == 2
    assert "workspace must be an existing directory" in capsys.readouterr().err


def _state(tmp_path: Path) -> Path:
    state = tmp_path / ".wsum"
    state.mkdir()
    return state


def _candidate(state: Path, content: str = "next\n") -> tuple[Path, str]:
    directory = state / "pending" / "example"
    directory.mkdir(parents=True)
    path = directory / "candidate.txt"
    path.write_text(content)
    return path, hashlib.sha256(content.encode()).hexdigest()


def _promote(
    state: Path,
    digest: str,
    *,
    expected: str | None = None,
) -> dict[str, object]:
    return workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256=expected,
        candidate_sha256=digest,
    )


def test_write_report_creates_private_run_report(tmp_path: Path) -> None:
    report = "## Example\n\nA material update.\n"

    destination = workspace._write_report(tmp_path, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]

    reports = tmp_path / "reports"
    content = destination.read_text()
    assert destination == reports / f"{_RUN_ID}.md"
    assert f"Run: `{_RUN_ID}`" in content
    assert report.strip() in content
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_report_aggregates_targets_into_one_run_file(tmp_path: Path) -> None:
    workspace._write_report(tmp_path, _RUN_ID, "one", "## One\n\nFirst.\n")  # pyright: ignore[reportPrivateUsage]
    workspace._write_report(tmp_path, _RUN_ID, "two", "## Two\n\nSecond.\n")  # pyright: ignore[reportPrivateUsage]

    reports = list((tmp_path / "reports").glob("*.md"))
    assert reports == [tmp_path / "reports" / f"{_RUN_ID}.md"]
    content = reports[0].read_text()
    assert "## One" in content
    assert "## Two" in content


def test_write_report_replaces_existing_target_section(tmp_path: Path) -> None:
    destination = workspace._write_report(  # pyright: ignore[reportPrivateUsage]
        tmp_path, _RUN_ID, "example", "## Example\n\nOld report.\n"
    )

    workspace._write_report(tmp_path, _RUN_ID, "example", "## Example\n\nNew report.\n")  # pyright: ignore[reportPrivateUsage]

    content = destination.read_text()
    assert "Old report." not in content
    assert content.count("New report.") == 1
    assert content.count("<!-- wsum:target example:start -->") == 1


@pytest.mark.parametrize(
    "target_id",
    ["../escape", "/absolute", "nested/path"],
    ids=["traversal", "absolute", "separator"],
)
def test_write_report_rejects_invalid_target_id(tmp_path: Path, target_id: str) -> None:
    with pytest.raises(WorkspaceError, match="invalid_target_id"):
        workspace._write_report(tmp_path, _RUN_ID, target_id, "update\n")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "20260101T000000Z-DEADBEEF", "not-a-run"],
    ids=["traversal", "uppercase-suffix", "malformed"],
)
def test_write_report_rejects_invalid_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(WorkspaceError, match="invalid_run_id"):
        workspace._write_report(tmp_path, run_id, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "reports").exists()


def test_write_report_rejects_reserved_section_markers(tmp_path: Path) -> None:
    report = "<!-- wsum:target other:start -->\nInjected section\n"

    with pytest.raises(WorkspaceError, match="reserved marker"):
        workspace._write_report(tmp_path, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "reports" / f"{_RUN_ID}.md").exists()


def _symlink_workspace_root_for_report(
    tmp_path: Path, outside: Path
) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace-link"
    workspace_root.symlink_to(outside, target_is_directory=True)
    return workspace_root, outside / "reports" / f"{_RUN_ID}.md"


def _symlink_reports_directory_for_report(
    tmp_path: Path, outside: Path
) -> tuple[Path, Path]:
    (tmp_path / "reports").symlink_to(outside, target_is_directory=True)
    return tmp_path, outside / f"{_RUN_ID}.md"


@pytest.mark.parametrize(
    "setup",
    [_symlink_workspace_root_for_report, _symlink_reports_directory_for_report],
    ids=["workspace-root", "reports-directory"],
)
def test_write_report_rejects_symlinked_workspace_paths(
    tmp_path: Path,
    setup: Callable[[Path, Path], tuple[Path, Path]],
) -> None:
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    workspace_root, outside_report = setup(tmp_path, outside)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        workspace._write_report(workspace_root, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert not outside_report.exists()


def test_write_report_rejects_symlinked_destination(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("original\n")
    destination = reports / f"{_RUN_ID}.md"
    destination.symlink_to(outside)

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert destination.is_symlink()
    assert outside.read_text() == "original\n"


def test_write_report_preserves_destination_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    destination = reports / f"{_RUN_ID}.md"
    destination.write_text("old report\n")

    def fail_replace(_: Path, __: Path) -> Path:
        raise OSError

    monkeypatch.setattr(workspace.Path, "replace", fail_replace)
    with pytest.raises(WorkspaceError, match="cannot write report"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "new report\n")  # pyright: ignore[reportPrivateUsage]

    assert destination.read_text() == "old report\n"
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_write_report_fsyncs_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert fsynced == [tmp_path, tmp_path / "reports"]


def test_write_report_reports_fsync_failure_after_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    def fail(path: Path) -> None:
        if path == reports:
            raise OSError

    monkeypatch.setattr(workspace, "_fsync_directory", fail)
    with pytest.raises(WorkspaceError, match="cannot fsync report directory"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    content = (reports / f"{_RUN_ID}.md").read_text()
    assert f"Run: `{_RUN_ID}`" in content
    assert "update" in content
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_promote_snapshot_rejects_symlinked_state_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    state = tmp_path / ".wsum"
    state.symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, digest)

    assert not (outside / "pending").exists()
    assert not (outside / "snapshots").exists()


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        (None, None),
        ("old\n", hashlib.sha256(b"old\n").hexdigest()),
    ],
    ids=["create-baseline", "replace-matching-baseline"],
)
def test_promote_snapshot_creates_or_replaces_baseline(
    tmp_path: Path, baseline: str | None, expected: str | None
) -> None:
    state = _state(tmp_path)
    if baseline is not None:
        snapshots = state / "snapshots"
        snapshots.mkdir()
        (snapshots / "example.txt").write_text(baseline, encoding="utf-8")
    _, digest = _candidate(state)

    result = _promote(state, digest, expected=expected)

    snapshot = state / "snapshots" / "example.txt"
    assert result["action"] == "snapshot_promoted"
    assert result["applied"] is True
    assert result["sha256"] == digest
    assert snapshot.read_text() == "next\n"


def test_promote_snapshot_reports_stale_baseline(tmp_path: Path) -> None:
    state = _state(tmp_path)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    current = b"current\n"
    (snapshots / "example.txt").write_bytes(current)
    _, digest = _candidate(state)

    result = _promote(state, digest, expected="0" * 64)

    assert result == {
        "action": "snapshot_conflict",
        "applied": False,
        "current_sha256": hashlib.sha256(current).hexdigest(),
    }
    assert (snapshots / "example.txt").read_bytes() == current


def test_promote_snapshot_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _candidate(state)

    with pytest.raises(WorkspaceError, match="does not match"):
        _promote(state, "0" * 64)


def test_promote_snapshot_requires_pending_candidate(tmp_path: Path) -> None:
    state = _state(tmp_path)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="pending decision"):
        _promote(state, digest)


def test_promote_snapshot_rejects_symlink_candidate(tmp_path: Path) -> None:
    state = _state(tmp_path)
    pending = state / "pending" / "example"
    pending.mkdir(parents=True)
    target = tmp_path / "outside-candidate.txt"
    target.write_text("next\n")
    (pending / "candidate.txt").symlink_to(target)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        _promote(state, digest)


def _symlink_pending_directory(state: Path, outside: Path) -> None:
    (state / "pending").symlink_to(outside, target_is_directory=True)


def _symlink_snapshot_directory(state: Path, outside: Path) -> None:
    _candidate(state)
    (state / "snapshots").symlink_to(outside, target_is_directory=True)


@pytest.mark.parametrize(
    ("setup", "outside_name"),
    [
        (_symlink_pending_directory, "outside-pending"),
        (_symlink_snapshot_directory, "outside-snapshots"),
    ],
    ids=["pending", "snapshots"],
)
def test_promote_snapshot_rejects_symlinked_state_directories(
    tmp_path: Path, setup: Callable[[Path, Path], None], outside_name: str
) -> None:
    state = _state(tmp_path)
    outside = tmp_path / outside_name
    outside.mkdir()
    setup(state, outside)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, digest)
    assert not (outside / "example.txt").exists()


@pytest.mark.parametrize(
    "snapshot_directory_exists", [True, False], ids=["existing", "created"]
)
def test_promote_snapshot_fsyncs_snapshot_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_directory_exists: bool,
) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)
    if snapshot_directory_exists:
        (state / "snapshots").mkdir()
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    result = _promote(state, digest)

    assert result["action"] == "snapshot_promoted"
    expected = [state, state, state / "snapshots"]
    assert fsynced == expected


def test_promote_snapshot_reports_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]

    def fail(path: Path) -> None:
        if path == snapshots:
            raise OSError
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail)
    with pytest.raises(WorkspaceError, match="cannot fsync snapshot directory"):
        _promote(state, digest)
    assert (state / "snapshots" / "example.txt").read_text() == "next\n"


def test_promote_snapshot_retries_idempotently(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)

    _promote(state, digest)
    result = _promote(state, digest)

    assert result == {
        "action": "snapshot_promoted",
        "applied": True,
        "already": True,
        "path": str(state / "snapshots" / "example.txt"),
        "sha256": digest,
    }


_REVISION = "a" * 32


def _replace_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "group_dir_existed": False,
        "kind": "replace",
        "layout": "none",
        "old_candidate": None,
        "old_state": None,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


def _commit_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "commit",
        "revision": _REVISION,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


def _cleanup_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "cleanup",
        "material": False,
        "purpose": "finalize",
        "report_sha256": None,
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "existing directory"),
        ("file", "non-symlink directory"),
        ("symlink", "non-symlink directory"),
    ],
    ids=["missing", "file", "symlink"],
)
def test_workspace_rejects_missing_and_non_directory_roots(
    tmp_path: Path, case: str, message: str
) -> None:
    path = tmp_path / "workspace"
    if case == "file":
        path.write_text("file", encoding="utf-8")
    elif case == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError, match=message):
        workspace._workspace(path)  # pyright: ignore[reportPrivateUsage]


def test_directory_fsync_is_skipped_when_platform_has_no_directory_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(workspace.os, "O_DIRECTORY", raising=False)
    workspace._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure",
    ["mkdir", "restat", "parent-fsync", "lstat"],
    ids=["mkdir", "restat", "parent-fsync", "lstat"],
)
def test_ensure_directory_wraps_filesystem_failures(  # ruff: ignore[complex-structure]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "state"
    if failure == "mkdir":
        original_lstat = Path.lstat

        def missing(target: Path) -> os.stat_result:
            if target == path:
                raise FileNotFoundError
            return original_lstat(target)

        def fail_mkdir(_target: Path, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
            raise OSError("mkdir failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        monkeypatch.setattr(Path, "lstat", missing)
        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    elif failure == "restat":
        original_lstat = Path.lstat
        calls = 0

        def fail_restat(target: Path) -> os.stat_result:
            nonlocal calls
            if target == path:
                calls += 1
                if calls == 1:
                    raise FileNotFoundError
                raise OSError("restat failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_restat)
        original_mkdir = Path.mkdir

        def create(target: Path, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
            if target == path:
                original_mkdir(target, *args, **kwargs)
            else:
                original_mkdir(target, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", create)
    elif failure == "parent-fsync":
        path.mkdir()
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _target: (_ for _ in ()).throw(OSError("sync failed")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_lstat = Path.lstat

        def fail_lstat(target: Path) -> os.stat_result:
            if target == path:
                raise PermissionError("cannot stat")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(WorkspaceError, match="unavailable|fsync parent"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
        workspace._ensure_directory(  # pyright: ignore[reportPrivateUsage]
            path, "test directory", sync_parent=failure == "parent-fsync"
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "missing"),
        (b"", "size"),
        (b"\xff", "UTF-8"),
        (b"x" * (1024 * 1024 + 1), "size"),
    ],
    ids=["missing", "empty", "invalid-utf8", "oversized"],
)
def test_read_csv_rejects_missing_empty_invalid_and_oversized_files(
    tmp_path: Path, contents: bytes | None, message: str
) -> None:
    path = tmp_path / "targets.csv"
    if contents is not None:
        path.write_bytes(contents)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_csv(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("name,name,url\nA,A,https://example.com/\n", "unique header"),
        ("name,url\nA,https://example.com/,extra\n", "too many columns"),
        ("name,url\n,\n", "contains no targets"),
        ("name,url\n\nA,https://example.com/\n", ""),
    ],
    ids=["duplicate-header", "too-many-columns", "no-targets", "blank-row"],
)
def test_load_targets_handles_header_rows_and_empty_records(
    tmp_path: Path, csv_text: str, message: str
) -> None:
    if not message:
        csv_text = "name,url,enabled\n\nA,https://example.com/,\n"
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    if message:
        with pytest.raises(WorkspaceError, match=message):
            workspace.load_targets(tmp_path)
    else:
        result = workspace.load_targets(tmp_path)
        assert len(result) == 1
        assert result[0]["enabled"] is True


def test_target_id_falls_back_for_malformed_urlsplit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace,
        "urlsplit",
        lambda _url: (_ for _ in ()).throw(ValueError("bad URL")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    target_id = workspace._target_id("not-a-url")  # pyright: ignore[reportPrivateUsage]
    assert target_id.startswith("target-")


@pytest.mark.parametrize(
    ("value", "validator", "message"),
    [
        (None, "target", "invalid_target_id"),
        ("../x", "target", "invalid_target_id"),
        ("bad", "run", "invalid_run_id"),
        ("A" * 64, "sha", "lowercase hexadecimal"),
    ],
    ids=["non-string-target", "unsafe-target", "run-id", "uppercase-sha"],
)
def test_identifier_validators_reject_malformed_values(
    value: object, validator: str, message: str
) -> None:
    function = {  # pyright: ignore[reportUnknownVariableType]
        "target": workspace._validate_target_id,
        "run": workspace._validate_run_id,
        "sha": lambda item: workspace._validate_sha256(item, "digest"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    }[validator]
    with pytest.raises(WorkspaceError, match=message):
        function(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        (
            "name,url,enabled\n",
            "A,https://example.com/\nB,https://example.com/\n",
            "duplicate_target_id",
        ),
        ("name,url\n", "A,http://[::1\n", "url is invalid"),
        ("name,url\n", "A,https://example.com/#frag\n", "fragment"),
    ],
    ids=["duplicate-url", "malformed-url", "url-fragment"],
)
def test_load_targets_rejects_duplicate_and_malformed_rows(
    tmp_path: Path, header: str, rows: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(header + rows, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        workspace.load_targets(tmp_path)


@pytest.mark.parametrize(
    "layout", ["grouped", "legacy", "missing"], ids=["grouped", "legacy", "missing"]
)
def test_pending_path_resolver_selects_layout_or_creates_grouped(
    tmp_path: Path, layout: str
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    if layout == "grouped":
        (pending / "example").mkdir()
    elif layout == "legacy":
        (state / "candidates").mkdir()
        (pending / "example.json").write_text("{}", encoding="utf-8")
    if layout == "missing":
        with pytest.raises(WorkspaceError, match="no valid pending"):
            workspace._pending_paths(state, "example")  # pyright: ignore[reportPrivateUsage]
        metadata, candidate = workspace._pending_paths(  # pyright: ignore[reportPrivateUsage]
            state, "example", create=True
        )
        assert metadata == pending / "example" / "state.json"
        assert candidate == pending / "example" / "candidate.txt"
    else:
        metadata, candidate, found = workspace._existing_pending_paths(  # pyright: ignore[reportGeneralTypeIssues, reportPrivateUsage, reportUnknownVariableType]
            state, "example"
        )
        assert found == layout
        assert metadata.name == (  # pyright: ignore[reportUnknownMemberType]
            "state.json" if layout == "grouped" else "example.json"
        )
        assert (
            candidate.name == "candidate.txt"  # pyright: ignore[reportUnknownMemberType]
            if layout == "grouped"
            else candidate.name == "example.txt"  # pyright: ignore[reportUnknownMemberType]
        )


@pytest.mark.parametrize(
    "mutate",
    [
        "odd-header",
        "start-only",
        "end-before-start",
        "duplicate-start",
        "duplicate-end",
    ],
    ids=[
        "reserved-marker",
        "unbalanced",
        "reversed",
        "duplicate-start",
        "duplicate-end",
    ],
)
def test_report_renderer_rejects_invalid_managed_sections(mutate: str) -> None:
    marker = "<!-- wsum:target example"
    existing = {
        "odd-header": "old report",
        "start-only": f"{marker}:start -->\nsection\n",
        "end-before-start": f"{marker}:end -->\n{marker}:start -->\n",
        "duplicate-start": (
            f"{marker}:start -->\na\n{marker}:end -->\n{marker}:start -->\n"
        ),
        "duplicate-end": f"{marker}:start -->\na\n{marker}:end -->\n{marker}:end -->\n",
    }[mutate]
    report = f"Injected {marker}" if mutate == "odd-header" else "new report"
    message = "reserved marker" if mutate == "odd-header" else "managed section"
    with pytest.raises(WorkspaceError, match=message):
        workspace._render_run_report(  # pyright: ignore[reportPrivateUsage]
            _RUN_ID, "example", report, existing.encode()
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [(b"\xff", "UTF-8"), (b"", "size"), (b"x" * (40 * 1024 * 1024 + 1), "size")],
    ids=["invalid-utf8", "empty", "oversized"],
)
def test_read_text_bytes_rejects_invalid_content(
    tmp_path: Path, contents: bytes, message: str
) -> None:
    path = tmp_path / "content.txt"
    path.write_bytes(contents)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_text_bytes(path, "test content")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        ("not-object", None),
        ("bad-version", _replace_record(version=True)),
        ("wrong-target", _replace_record(target_id="other")),
        ("extra-key", _replace_record(extra="x")),
        ("bad-layout", _replace_record(layout="other")),
        ("bad-group-flag", _replace_record(layout="grouped", group_dir_existed=False)),
        ("bad-backup-type", _replace_record(old_state=1)),
        ("bad-base64", _replace_record(old_state="%%%")),
        ("commit-extra-key", _commit_record(extra="x")),
        ("bad-revision", _commit_record(revision="bad")),
        ("cleanup-extra-key", _cleanup_record(extra="x")),
        ("bad-cleanup-purpose", _cleanup_record(purpose="unknown")),
        (
            "discard-has-decision",
            _cleanup_record(purpose="discard", revision=_REVISION),
        ),
        ("finalize-bad-revision", _cleanup_record(revision="bad")),
        ("finalize-bad-material", _cleanup_record(material="yes")),
        ("finalize-bad-run-id", _cleanup_record(run_id="bad")),
        ("material-bad-hash", _cleanup_record(material=True, report_sha256="bad")),
        ("nonmaterial-hash", _cleanup_record(report_sha256="a" * 64)),
        ("unknown-kind", {"kind": "other", "target_id": "example", "version": 1}),
    ],
    ids=[
        "not-object",
        "bad-version",
        "wrong-target",
        "extra-key",
        "bad-layout",
        "bad-group-flag",
        "bad-backup-type",
        "bad-base64",
        "commit-extra-key",
        "bad-revision",
        "cleanup-extra-key",
        "bad-cleanup-purpose",
        "discard-decision",
        "finalize-revision",
        "finalize-material",
        "finalize-run-id",
        "material-hash",
        "nonmaterial-hash",
        "unknown-kind",
    ],
)
def test_recovery_record_validation_rejects_malformed_shapes(
    kind: str, record: object
) -> None:
    value = record
    if kind == "not-object":
        value = None
    with pytest.raises(WorkspaceError, match="invalid"):
        workspace._validate_recovery_record(value, "example")  # pyright: ignore[reportPrivateUsage]


def test_recovery_backup_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1)
    encoded = base64.b64encode(b"too long").decode()
    with pytest.raises(WorkspaceError, match="too large"):
        workspace._decode_recovery_backup(encoded)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "record",
    [
        _cleanup_record(
            purpose="discard",
            material=None,
            report_sha256=None,
            revision=None,
            run_id=None,
        ),
        _cleanup_record(material=True, report_sha256="b" * 64),
        _replace_record(layout="legacy", old_state=base64.b64encode(b"old").decode()),
        _commit_record(),
    ],
    ids=["valid-discard", "valid-finalize", "valid-legacy-replace", "valid-commit"],
)
def test_recovery_record_validation_accepts_supported_records(
    record: dict[str, object],
) -> None:
    assert workspace._validate_recovery_record(record, "example") == record  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("oversized", "size is invalid"),
        ("invalid-json", "is invalid"),
        ("wrong-mode", "private"),
    ],
    ids=["oversized", "invalid-json", "permissive-mode"],
)
def test_read_recovery_file_checks_size_json_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    path = tmp_path / "recovery.json"
    if kind == "oversized":
        path.write_text("x", encoding="utf-8")
        monkeypatch.setattr(workspace, "_MAX_RECOVERY_RECORD_BYTES", 0)
    else:
        path.write_text("{}", encoding="utf-8")
    path.chmod(0o644 if kind == "wrong-mode" else 0o600)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_recovery_file(  # pyright: ignore[reportPrivateUsage]
            path, "example", "recovery record"
        )


@pytest.mark.parametrize("data", [b"bad", b"\xff"], ids=["malformed", "invalid-utf8"])
def test_read_recovery_file_rejects_invalid_json(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "recovery.json"
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(WorkspaceError, match="is invalid"):
        workspace._read_recovery_file(path, "example", "recovery record")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("purpose", ["replace", "commit"], ids=["undo", "commit"])
def test_recovery_record_write_and_retire_roundtrip(
    tmp_path: Path, purpose: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    record = _replace_record() if purpose == "replace" else _commit_record()
    writer = (
        workspace._write_recovery_record
        if purpose == "replace"
        else workspace._write_commit_record
    )
    writer(state, record)  # pyright: ignore[reportPrivateUsage]
    read = (
        workspace._read_recovery_record
        if purpose == "replace"
        else workspace._read_commit_record
    )
    assert read(state, "example") == record  # pyright: ignore[reportPrivateUsage]
    retire = (
        workspace._retire_recovery_record
        if purpose == "replace"
        else workspace._retire_commit_record
    )
    retire(state, "example")  # pyright: ignore[reportPrivateUsage]
    assert read(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    retire(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_retire_recovery_record_ignores_unlink_errors_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    recovery = state / ".pending-recovery"
    recovery.mkdir(mode=0o700)
    path = recovery / "example.json"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    workspace._retire_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, "example", ignore_errors=True
    )
    with pytest.raises(WorkspaceError, match="cannot retire"):
        workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_restore_transaction_file_removes_or_replaces_contents(tmp_path: Path) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    path.write_text("old", encoding="utf-8")
    workspace._restore_transaction_file(path, b"new", "pending decision")  # pyright: ignore[reportPrivateUsage]
    assert path.read_bytes() == b"new"
    workspace._restore_transaction_file(path, None, "pending decision")  # pyright: ignore[reportPrivateUsage]
    assert not path.exists()
    workspace._restore_transaction_file(path, None, "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_optional_lstat_wraps_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blocked"
    original_lstat = Path.lstat

    def deny(path: Path) -> os.stat_result:
        if path == target:
            raise PermissionError("denied")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny)
    with pytest.raises(WorkspaceError, match="cannot stat blocked"):
        workspace._optional_lstat(target, "blocked")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_replace_record(), None),
        (
            _replace_record(
                old_state=base64.b64encode(
                    b'{"revision":"' + b"a" * 32 + b'"}'
                ).decode()
            ),
            _REVISION,
        ),
    ],
    ids=["no-old-state", "old-revision"],
)
def test_replacement_previous_revision(
    record: dict[str, object], expected: str | None
) -> None:
    assert workspace._replacement_previous_revision(record) == expected  # pyright: ignore[reportPrivateUsage]


def test_main_success_emits_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url,enabled\nExample,https://example.com/,false\n", encoding="utf-8"
    )
    assert workspace.main(["--workspace", str(tmp_path), "check"]) == 0
    captured = capsys.readouterr()
    assert '"action": "skipped"' in captured.out
    assert not captured.err


@pytest.mark.parametrize(
    "payload", ["not json", "[]"], ids=["invalid-json", "not-object"]
)
def test_read_decision_rejects_invalid_stdin(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setattr(
        workspace.sys,
        "stdin",
        type("Input", (), {"read": lambda _self: payload})(),  # pyright: ignore[reportUnknownLambdaType]
    )
    with pytest.raises(WorkspaceError, match="stdin|object"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
        workspace._read_decision()  # pyright: ignore[reportPrivateUsage]


def _pending_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_sha256": "b" * 64,
        "diff_truncated": False,
        "expected_sha256": None,
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "target_id": "example",
    }
    payload.update(changes)
    return payload


def _grouped_pending(state: Path, payload: dict[str, object], data: bytes) -> Path:
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    (target / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    (target / "candidate.txt").write_bytes(data)
    return target


def _write_recovery(state: Path, record: dict[str, object]) -> None:
    workspace._write_recovery_record(state, record)  # pyright: ignore[reportPrivateUsage]


def _write_commit(state: Path, revision: str = _REVISION) -> None:
    workspace._write_commit_record(  # pyright: ignore[reportPrivateUsage]
        state,
        {
            "kind": "commit",
            "revision": revision,
            "target_id": "example",
            "version": 1,
        },
    )


def test_fsync_directory_handles_optional_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(workspace.os, "O_CLOEXEC", raising=False)
    monkeypatch.delattr(workspace.os, "O_NOFOLLOW", raising=False)
    workspace._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]


def test_ensure_directory_tolerates_creation_race_and_optional_parent_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "state"
    original_lstat = Path.lstat
    original_mkdir = Path.mkdir
    calls = 0

    def racing_lstat(path: Path) -> os.stat_result:
        nonlocal calls
        if path == directory:
            calls += 1
            if calls == 1:
                raise FileNotFoundError
        return original_lstat(path)

    def racing_mkdir(path: Path, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
        original_mkdir(path)
        raise FileExistsError

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    assert (
        workspace._ensure_directory(  # pyright: ignore[reportPrivateUsage]
            directory, "state", sync_parent=False
        )
        == directory
    )


@pytest.mark.parametrize("kind", ["symlink", "fifo"], ids=["symlink", "non-file"])
def test_read_csv_requires_regular_non_symlink_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "targets.csv"
    if kind == "symlink":
        target = tmp_path / "target.csv"
        target.write_text("name,url\nA,https://example.com/\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_csv(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("name,url,unexpected\nA,https://example.com/,x\n", "unsupported"),
        ("name\nA\n", "requires name and url"),
        ("name,url\n,https://example.com/\n", "name must be non-empty"),
        ("name,url,enabled\nA,https://example.com/,no\n", "enabled must be"),
        (
            "name,url\nA,https://user:pass@example.com/\n",
            "credentials",
        ),
    ],
    ids=[
        "unsupported-column",
        "missing-required",
        "empty-name",
        "bad-enabled",
        "credentials",
    ],
)
def test_load_targets_rejects_invalid_schema_and_row_values(
    tmp_path: Path, csv_text: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        workspace.load_targets(tmp_path)


@pytest.mark.parametrize(
    ("location", "error", "message"),
    [
        ("target", PermissionError, "pending target directory"),
        ("legacy", PermissionError, "cannot stat pending decision"),
        ("candidates", PermissionError, "legacy candidate directory"),
    ],
    ids=["target-stat", "legacy-state-stat", "candidate-dir-stat"],
)
def test_existing_pending_paths_wraps_stat_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    error: type[OSError],
    message: str,
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    (pending / "example.json").write_text("{}", encoding="utf-8")
    candidates = state / "candidates"
    candidates.mkdir()
    target = {
        "target": pending / "example",
        "legacy": pending / "example.json",
        "candidates": candidates,
    }[location]
    original_lstat = Path.lstat

    def fail(path: Path) -> os.stat_result:
        if path == target:
            raise error("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._existing_pending_paths(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("target", "pending target"),
        ("legacy-state", "pending decision"),
        ("candidates", "state/candidates"),
    ],
    ids=["target-symlink", "legacy-state-symlink", "candidate-dir-symlink"],
)
def test_existing_pending_paths_rejects_unsafe_layouts(
    tmp_path: Path, location: str, message: str
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    candidates = state / "candidates"
    candidates.mkdir()
    target_dir = pending / "example"
    target_file = pending / "example.json"
    candidate = candidates / "example.txt"
    for path in (target_dir, target_file, candidate):
        if path.exists():
            path.unlink()
    if location == "target":
        target_dir.symlink_to(tmp_path, target_is_directory=True)
    elif location == "legacy-state":
        target_file.symlink_to(tmp_path / "missing")
    elif location == "candidates":
        candidates.rmdir()
        candidates.symlink_to(tmp_path, target_is_directory=True)
        target_file.write_text("{}", encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        workspace._existing_pending_paths(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [("stat", "cannot stat candidate"), ("symlink", "regular non-symlink")],
    ids=["stat-error", "symlink"],
)
def test_candidate_path_rejects_unavailable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    state = tmp_path / ".wsum"
    candidate_dir = state / "pending" / "example"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "state.json").write_text("{}", encoding="utf-8")
    candidate = candidate_dir / "candidate.txt"
    if kind == "symlink":
        candidate.symlink_to(tmp_path / "missing")
    else:
        original_lstat = Path.lstat

        def fail(path: Path) -> os.stat_result:
            if path == candidate:
                raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._candidate_path(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [("stat", "cannot stat report"), ("directory", "regular non-symlink")],
    ids=["stat-error", "directory"],
)
def test_report_path_rejects_invalid_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    destination = tmp_path / f"{_RUN_ID}.md"
    if kind == "directory":
        destination.mkdir()
    else:
        original_lstat = Path.lstat

        def fail(path: Path) -> os.stat_result:
            if path == destination:
                raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._report_path(tmp_path, _RUN_ID)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("function", ["report", "snapshot"], ids=["report", "snapshot"])
def test_optional_reads_wrap_non_missing_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, function: str
) -> None:
    path = tmp_path / "value.txt"
    original_lstat = Path.lstat

    def fail(target: Path) -> os.stat_result:
        if target == path:
            raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(target)

    monkeypatch.setattr(Path, "lstat", fail)
    reader = (
        workspace._read_optional_report
        if function == "report"
        else workspace._read_snapshot
    )
    with pytest.raises(WorkspaceError, match="cannot stat"):
        reader(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("stat", "cannot stat test file"),
        ("directory", "regular non-symlink"),
        ("read", "cannot read test file"),
    ],
    ids=["stat-error", "directory", "read-error"],
)
def test_read_text_bytes_wraps_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    path = tmp_path / "value.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "read":
        path.write_text("valid", encoding="utf-8")
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_lstat = Path.lstat

        def fail(target: Path) -> os.stat_result:
            if target == path:
                raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_text_bytes(path, "test file")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure", ["mkstemp", "write"], ids=["create-temp", "write-temp"]
)
def test_temporary_file_creation_and_write_failures_are_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    destination = tmp_path / "target.txt"
    if failure == "mkstemp":
        monkeypatch.setattr(
            workspace.tempfile,
            "mkstemp",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot create temporary"
    else:
        monkeypatch.setattr(
            workspace,
            "_write_file_data",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot write temporary"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_temporary_file(destination, b"data", "test")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["replace", "fsync", "readback"], ids=["replace", "fsync", "readback"]
)
def test_snapshot_promotion_wraps_failures_and_checks_durable_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    data = b"candidate"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(workspace, "_fsync_snapshot_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot promote snapshot"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_snapshot_directory",
            lambda _path: (_ for _ in ()).throw(WorkspaceError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "injected"
    else:
        original_read = workspace._read_snapshot
        calls = 0

        def changed(path: Path) -> bytes | None:
            nonlocal calls
            calls += 1
            return original_read(path) if calls == 1 else b"different"

        monkeypatch.setattr(workspace, "_read_snapshot", changed)
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
            state,
            target_id="example",
            expected_sha256=None,
            candidate_sha256=digest,
            candidate_data=data,
        )


def test_snapshot_promotion_accepts_candidate_source_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    candidate = tmp_path / "candidate.txt"
    candidate.write_bytes(b"candidate")
    digest = __import__("hashlib").sha256(b"candidate").hexdigest()
    monkeypatch.setattr(workspace, "_fsync_snapshot_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    promoted = workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256=None,
        candidate_sha256=digest,
        candidate_source=candidate,
    )
    assert promoted["action"] == "snapshot_promoted"
    conflict = workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256="c" * 64,
        candidate_sha256=hashlib.sha256(b"another").hexdigest(),
        candidate_data=b"another",
    )
    assert conflict["action"] == "snapshot_conflict"


@pytest.mark.parametrize(
    "fault",
    ["render-size", "replace", "fsync", "readback"],
    ids=["rendered-size", "replace", "fsync", "readback"],
)
def test_report_write_validates_and_persists_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 20)
    if fault == "render-size":
        report = "x" * 19
        expected = "size is invalid"
    else:
        monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1000)
        report = "ok"
        if fault == "replace":
            monkeypatch.setattr(
                Path,
                "replace",
                lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            )
            expected = "cannot write report"
        elif fault == "fsync":
            original_fsync = workspace._fsync_directory

            def fail_report_fsync(path: Path) -> None:
                if path.name == "reports":
                    raise OSError("injected")  # ruff: ignore[raw-string-in-exception]
                original_fsync(path)

            monkeypatch.setattr(workspace, "_fsync_directory", fail_report_fsync)
            expected = "cannot fsync report directory"
        else:
            calls = 0

            def mismatch(_path: Path, _description: str) -> bytes:
                nonlocal calls
                calls += 1
                return b"bad"

            monkeypatch.setattr(workspace, "_read_text_bytes", mismatch)
            expected = "report read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_report(workspace_root, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]


def test_monitor_target_skips_candidate_read_for_unchanged_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "snapshots").mkdir()
    monkeypatch.setattr(workspace, "_recover_pending", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace.monitor, "run", lambda _args: {"status": "unchanged"})  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace, "_discard_pending", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    target = {"target_id": "example", "url": "https://example.com/", "name": "Example"}
    result = workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]
    assert result["action"] == "unchanged"


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("target", "pending target"),
        ("legacy-state", "pending decision"),
        ("candidates", "state/candidates"),
        ("candidate", "candidate must"),
    ],
    ids=["grouped-target", "legacy-state", "candidate-dir", "candidate-file"],
)
def test_pending_cleanup_paths_rejects_unsafe_entries(
    tmp_path: Path, kind: str, message: str
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    candidates = state / "candidates"
    candidates.mkdir()
    if kind == "target":
        (pending / "example").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "legacy-state":
        (pending / "example.json").symlink_to(tmp_path / "missing")
    elif kind == "candidates":
        candidates.rmdir()
        candidates.symlink_to(tmp_path, target_is_directory=True)
    else:
        (candidates / "example.txt").symlink_to(tmp_path / "missing")
    with pytest.raises(WorkspaceError, match=message):
        workspace._pending_cleanup_paths(state, pending, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["legacy-unlink", "grouped-remove", "fsync"],
    ids=["legacy-unlink", "grouped-remove", "directory-fsync"],
)
def test_remove_pending_wraps_removal_and_sync_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    target = pending / "example"
    target.mkdir(parents=True)
    if fault == "legacy-unlink":
        candidates = state / "candidates"
        candidates.mkdir()
        (candidates / "example.txt").write_text("candidate", encoding="utf-8")
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    elif fault == "grouped-remove":
        monkeypatch.setattr(
            workspace.shutil,
            "rmtree",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_fsync = workspace._fsync_directory

        def fail_pending_fsync(path: Path) -> None:
            if path == pending:
                raise OSError("injected")  # ruff: ignore[raw-string-in-exception]
            original_fsync(path)

        monkeypatch.setattr(workspace, "_fsync_directory", fail_pending_fsync)
    with pytest.raises(WorkspaceError, match="cannot (remove|fsync) pending"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
        workspace._remove_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("fault", ["replace", "fsync"], ids=["replace", "fsync"])
def test_pending_file_wraps_atomic_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    destination = tmp_path / "pending" / "state.json"
    destination.parent.mkdir()
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist pending decision"
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync pending directory"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_pending_file(destination, {"k": "v"})  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "permissive"], ids=["missing-dir", "symlink", "mode"]
)
def test_recovery_directory_validates_existing_directory(
    tmp_path: Path, kind: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    recovery = state / ".pending-recovery"
    if kind == "symlink":
        recovery.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "permissive":
        recovery.mkdir(mode=0o755)
        recovery.chmod(0o755)
    if kind == "missing":
        assert workspace._recovery_directory(state, create=False) is None  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(WorkspaceError, match="non-symlink|private"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
            workspace._recovery_directory(state, create=False)  # pyright: ignore[reportPrivateUsage]


def test_decode_recovery_backup_rejects_non_ascii_and_wrong_type() -> None:
    for value in (5, "é", "!@@"):
        with pytest.raises(WorkspaceError, match="invalid"):
            workspace._decode_recovery_backup(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_replace_record(layout="grouped", group_dir_existed=True), True),
        (
            {
                "kind": "cleanup",
                "material": False,
                "purpose": "finalize",
                "report_sha256": None,
                "revision": _REVISION,
                "run_id": _RUN_ID,
                "target_id": "example",
                "version": 1,
            },
            True,
        ),
    ],
    ids=["grouped-replacement", "nonmaterial-finalize"],
)
def test_recovery_record_validator_accepts_supported_optional_shapes(
    record: dict[str, object], expected: bool
) -> None:
    assert bool(workspace._validate_recovery_record(record, "example")) is expected  # pyright: ignore[reportPrivateUsage]


def test_read_commit_record_rejects_non_commit_record(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    recovery = state / ".pending-recovery"
    recovery.mkdir(parents=True, mode=0o700)
    path = recovery / "example.json.commit"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(WorkspaceError, match="pending commit record is invalid"):
        workspace._read_commit_record(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["commit-kind", "unsafe-existing", "replace", "fsync"],
    ids=["wrong-record-kind", "unsafe-existing", "replace", "fsync"],
)
def test_recovery_writer_validates_and_wraps_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    record = _replace_record()
    if fault == "commit-kind":
        with pytest.raises(WorkspaceError, match="pending commit record is invalid"):
            workspace._write_commit_record(state, record)  # pyright: ignore[reportPrivateUsage]
        return
    recovery = state / ".pending-recovery"
    recovery.mkdir(mode=0o700)
    destination = recovery / "example.json"
    if fault == "unsafe-existing":
        destination.write_text("old", encoding="utf-8")
        destination.chmod(0o644)
        expected = "unsafe"
    elif fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist"
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_recovery_record(state, record)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["unsafe", "unlink", "fsync"], ids=["unsafe", "unlink", "fsync"]
)
def test_recovery_retirement_checks_safety_and_optional_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    recovery = state / ".pending-recovery"
    recovery.mkdir(parents=True, mode=0o700)
    path = recovery / "example.json"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o644 if fault == "unsafe" else 0o600)
    if fault == "unsafe":
        expected = "unsafe"
        with pytest.raises(WorkspaceError, match=expected):
            workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "unlink":
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    with pytest.raises(WorkspaceError, match="cannot retire"):
        workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]
    workspace._retire_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, "example", ignore_errors=True
    )


@pytest.mark.parametrize("kind", ["symlink", "directory"], ids=["symlink", "directory"])
def test_transaction_backup_requires_regular_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "backup"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_transaction_backup(path, "backup")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["replace", "fsync", "readback"], ids=["replace", "fsync", "readback"]
)
def test_pending_candidate_persistence_checks_durable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    target = tmp_path / "pending" / "example"
    target.mkdir(parents=True)
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist pending candidate"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync pending candidate"
    else:
        monkeypatch.setattr(workspace, "_read_text_bytes", lambda *_args: b"different")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_pending_candidate(target / "candidate.txt", b"candidate")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["missing-unlink", "replace", "fsync", "readback"],
    ids=["missing-unlink", "replace", "fsync", "readback"],
)
def test_transaction_restore_wraps_delete_and_atomic_restore_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    if fault == "missing-unlink":
        path.write_text("old", encoding="utf-8")
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = None
        expected = "cannot restore"
    elif fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = b"new"
        expected = "cannot restore"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = b"new"
        expected = "cannot fsync"
    else:
        monkeypatch.setattr(workspace, "_read_text_bytes", lambda *_args: b"different")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        data = b"new"
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._restore_transaction_file(path, data, "pending decision")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "kind", ["iterdir", "symlink", "unlink"], ids=["iterdir", "symlink", "unlink"]
)
def test_pending_temporary_cleanup_rejects_invalid_or_unremovable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = tmp_path / "pending"
    directory.mkdir()
    if kind == "iterdir":
        monkeypatch.setattr(
            Path,
            "iterdir",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot inspect"
    else:
        temp = directory / ".candidate.txt.x.tmp"
        if kind == "symlink":
            temp.symlink_to(tmp_path / "missing")
            expected = "must be regular"
        else:
            temp.write_text("x", encoding="utf-8")
            monkeypatch.setattr(
                Path,
                "unlink",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            )
            expected = "cannot remove"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._remove_pending_write_temporaries(directory)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("layout", "old_state", "old_candidate"),
    [("legacy", b"old state", b"old candidate"), ("none", None, None)],
    ids=["legacy-restore", "none-layout"],
)
def test_restore_pending_replacement_restores_legacy_or_absent_layout(
    tmp_path: Path, layout: str, old_state: bytes | None, old_candidate: bytes | None
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    pending = state / "pending"
    pending.mkdir()
    candidates = state / "candidates"
    candidates.mkdir()
    record = _replace_record(
        layout=layout,
        old_state=None if old_state is None else base64.b64encode(old_state).decode(),
        old_candidate=None
        if old_candidate is None
        else base64.b64encode(old_candidate).decode(),
    )
    grouped = pending / "example"
    grouped.mkdir()
    (grouped / "state.json").write_text("new", encoding="utf-8")
    (grouped / "candidate.txt").write_text("new", encoding="utf-8")
    workspace._restore_pending_replacement(state, record)  # pyright: ignore[reportPrivateUsage]
    assert not grouped.exists()
    if layout == "legacy":
        assert (pending / "example.json").read_bytes() == old_state
        assert (candidates / "example.txt").read_bytes() == old_candidate


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ({"target_id": "../x", "revision": _REVISION, "material": False}, "target_id"),
        ({"target_id": "example", "revision": "bad", "material": False}, "revision"),
        ({"target_id": "example", "revision": _REVISION, "material": 1}, "boolean"),
        (
            {"target_id": "example", "revision": _REVISION, "material": True},
            "non-empty report",
        ),
        (
            {
                "target_id": "example",
                "revision": _REVISION,
                "material": False,
                "extra": 1,
            },
            "unsupported fields",
        ),
    ],
    ids=["bad-target", "bad-revision", "bad-material", "missing-report", "extra-field"],
)
def test_decision_validation_rejects_invalid_shapes(
    decision: dict[str, object], message: str
) -> None:
    with pytest.raises(WorkspaceError, match=message):
        workspace._validate_decision(decision)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("payload", "result"),
    [
        (None, False),
        ([], False),
        ({"extra": 1}, False),
        ({"target_id": "elsewhere"}, False),
        ({"revision": "c" * 32}, False),
        ({"run_id": "bad"}, False),
        ({"diff_truncated": 1}, False),
        ({"candidate_sha256": "bad"}, False),
        ({"expected_sha256": "bad"}, False),
    ],
    ids=[
        "not-object",
        "list",
        "wrong-fields",
        "wrong-target",
        "wrong-revision",
        "bad-run-id",
        "bad-truncation-flag",
        "bad-candidate-digest",
        "bad-expected-digest",
    ],
)
def test_replacement_matches_commit_returns_false_for_invalid_state(
    tmp_path: Path, payload: object, result: bool
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    data = b"candidate"
    item = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    item.update(payload if isinstance(payload, dict) else {})  # pyright: ignore[reportUnknownArgumentType]
    target = _grouped_pending(state, item, data)
    if payload is None:
        (target / "state.json").write_text("null", encoding="utf-8")
    elif isinstance(payload, list):
        (target / "state.json").write_text("[]", encoding="utf-8")
    assert (
        workspace._replacement_matches_commit(  # pyright: ignore[reportPrivateUsage]
            state, "example", {"revision": _REVISION}
        )
        is result
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing-state",
        "state-file",
        "missing-pending",
        "missing-group",
        "missing-files",
        "invalid-json",
        "invalid-utf8",
        "symlink-state",
        "directory-state",
        "symlink-candidate",
        "directory-candidate",
    ],
    ids=[
        "missing-state",
        "state-file",
        "missing-pending",
        "missing-group",
        "missing-files",
        "invalid-json",
        "invalid-utf8",
        "symlink-state",
        "directory-state",
        "symlink-candidate",
        "directory-candidate",
    ],
)
def test_replacement_matches_commit_handles_missing_malformed_and_unsafe_paths(  # ruff: ignore[too-many-return-statements]
    tmp_path: Path, fault: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    pending = state / "pending"
    target = pending / "example"
    if fault == "state-file":
        state.rmdir()
        state.write_text("not a directory", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="state must be"):
            workspace._replacement_matches_commit(
                state, "example", {"revision": _REVISION}
            )  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-state":
        state.rmdir()
        with pytest.raises(WorkspaceError, match="state must be"):
            workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-pending":
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-group":
        pending.mkdir()
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    pending.mkdir()
    target.mkdir()
    state_path = target / "state.json"
    candidate_path = target / "candidate.txt"
    if fault == "missing-files":
        state_path.write_text("{}", encoding="utf-8")
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault in {"invalid-json", "invalid-utf8"}:
        state_path.write_bytes(b"\xff" if fault == "invalid-utf8" else b"{")
        candidate_path.write_bytes(b"candidate")
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault in {"symlink-state", "directory-state"}:
        if fault == "symlink-state":
            state_path.symlink_to(tmp_path / "missing")
        else:
            state_path.mkdir()
        candidate_path.write_bytes(b"candidate")
        with pytest.raises(WorkspaceError, match="pending decision must be"):
            workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    state_path.write_text("{}", encoding="utf-8")
    if fault == "symlink-candidate":
        candidate_path.symlink_to(tmp_path / "missing")
    else:
        candidate_path.mkdir()
    with pytest.raises(WorkspaceError, match="candidate must be"):
        workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]


def test_replacement_matches_commit_accepts_valid_durable_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    data = b"candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    _grouped_pending(state, payload, data)
    assert workspace._replacement_matches_commit(  # pyright: ignore[reportPrivateUsage]
        state, "example", {"revision": _REVISION}
    )


@pytest.mark.parametrize(
    "record",
    [
        _replace_record(old_state=base64.b64encode(b"{").decode()),
        _replace_record(old_state=base64.b64encode(b"\xff").decode()),
        _replace_record(old_state=base64.b64encode(b"[]").decode()),
        _replace_record(old_state=base64.b64encode(b'{"revision":"bad"}').decode()),
    ],
    ids=["invalid-json", "invalid-utf8", "not-object", "invalid-revision"],
)
def test_replacement_previous_revision_rejects_corrupt_backup(
    record: dict[str, object],
) -> None:
    with pytest.raises(WorkspaceError, match="pending recovery record is invalid"):
        workspace._replacement_previous_revision(record)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "mode",
    ["no-record", "commit-only", "legacy-commit", "cleanup", "replace"],
    ids=["no-record", "commit-only", "legacy-commit", "cleanup", "replace"],
)
def test_recover_pending_completes_noncommit_recovery_records(
    tmp_path: Path, mode: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "pending").mkdir()
    if mode == "no-record":
        assert workspace._recover_pending(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    elif mode == "commit-only":
        _write_commit(state)
        assert workspace._recover_pending(state, "example") is None  # pyright: ignore[reportPrivateUsage]
        assert workspace._read_commit_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    elif mode == "legacy-commit":
        _write_recovery(
            state,
            {
                "kind": "commit",
                "revision": _REVISION,
                "target_id": "example",
                "version": 1,
            },
        )
        assert workspace._recover_pending(state, "example") is None  # pyright: ignore[reportPrivateUsage]
        assert workspace._read_recovery_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    elif mode == "cleanup":
        record = {
            "kind": "cleanup",
            "material": None,
            "purpose": "discard",
            "report_sha256": None,
            "revision": None,
            "run_id": None,
            "target_id": "example",
            "version": 1,
        }
        _write_recovery(state, record)  # pyright: ignore[reportArgumentType]
        assert workspace._recover_pending(state, "example") == record  # pyright: ignore[reportPrivateUsage]
        assert workspace._read_recovery_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    else:
        _write_recovery(state, _replace_record())
        assert workspace._recover_pending(state, "example") is None  # pyright: ignore[reportPrivateUsage]
        assert workspace._read_recovery_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("case", "revision", "expected"),
    [
        ("no-revision", None, "decision revision"),
        ("wrong-revision", "c" * 32, "does not match pending replacement"),
        ("committed-incomplete", _REVISION, "committed replacement is incomplete"),
    ],
    ids=["requires-revision", "wrong-revision", "incomplete-commit"],
)
def test_recover_pending_rejects_unresolvable_commit_states(
    tmp_path: Path, case: str, revision: str | None, expected: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "pending").mkdir()
    _write_recovery(state, _replace_record())
    _write_commit(state)
    if case == "committed-incomplete":
        revision = _REVISION
    with pytest.raises(WorkspaceError, match=expected):
        workspace._recover_pending(state, "example", revision=revision)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "resolution", ["accept", "rollback"], ids=["accept-commit", "rollback-old"]
)
def test_recover_pending_resolves_committed_replacement_by_revision(
    tmp_path: Path, resolution: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    old_revision = "c" * 32
    old_state = json.dumps({"revision": old_revision}).encode()
    backup = base64.b64encode(old_state).decode()
    undo = _replace_record(
        layout="grouped",
        group_dir_existed=True,
        old_state=backup,
        old_candidate=base64.b64encode(b"old candidate").decode(),
    )
    _write_recovery(state, undo)
    _write_commit(state)
    data = b"new candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    group = _grouped_pending(state, payload, data)
    if resolution == "accept":
        revision = _REVISION
        assert workspace._recover_pending(state, "example", revision=revision) is None  # pyright: ignore[reportPrivateUsage]
        assert group.joinpath("candidate.txt").read_bytes() == data
    else:
        revision = old_revision
        # The old pending group must exist before grouped rollback restores it.
        assert workspace._recover_pending(state, "example", revision=revision) is None  # pyright: ignore[reportPrivateUsage]
        assert group.joinpath("candidate.txt").read_bytes() == b"old candidate"
    assert workspace._read_recovery_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    assert workspace._read_commit_record(state, "example") is None  # pyright: ignore[reportPrivateUsage]


def test_recover_pending_rejects_conflicting_recovery_and_commit_records(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    _write_recovery(
        state,
        {
            "kind": "cleanup",
            "material": None,
            "purpose": "discard",
            "report_sha256": None,
            "revision": None,
            "run_id": None,
            "target_id": "example",
            "version": 1,
        },
    )
    _write_commit(state)
    with pytest.raises(WorkspaceError, match="records conflict"):
        workspace._recover_pending(state, "example", revision=_REVISION)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["malformed-json", "wrong-type"], ids=["malformed", "wrong-type"]
)
def test_read_pending_rejects_invalid_json_and_wrong_shapes(
    tmp_path: Path, fault: str
) -> None:
    state = tmp_path / ".wsum"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    path.write_text("{" if fault == "malformed-json" else "[]", encoding="utf-8")
    with pytest.raises(
        WorkspaceError,
        match="no valid pending|pending decision is invalid",  # ruff: ignore[pytest-raises-ambiguous-pattern]
    ):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("field_change", "message"),
    [
        ({"extra": 1}, "pending decision is invalid"),
        ({"run_id": "bad"}, "pending decision is invalid"),
    ],
    ids=["unexpected-field", "invalid-run-id"],
)
def test_read_pending_rejects_extra_fields_and_bad_run_ids(
    tmp_path: Path, field_change: dict[str, object], message: str
) -> None:
    state = tmp_path / ".wsum"
    payload = _pending_payload()
    payload.update(field_change)
    _grouped_pending(state, payload, b"candidate")
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_read_pending_backfills_legacy_missing_run_id(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    payload = _pending_payload()
    payload.pop("run_id")
    path = target / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    pending = workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]
    assert isinstance(pending.get("run_id"), str)
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == pending["run_id"]


def test_load_targets_rejects_non_string_csv_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "targets.csv").write_text("name,url\n", encoding="utf-8")

    class StubReader:
        fieldnames = ("name", "url")

        def __iter__(self) -> Any:  # ruff: ignore[any-type]
            return iter([{"name": object(), "url": "https://example.com/"}])

    monkeypatch.setattr(workspace.csv, "DictReader", lambda _stream: StubReader())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(WorkspaceError, match="invalid CSV value"):
        workspace.load_targets(tmp_path)


def test_existing_pending_paths_requires_legacy_candidate_directory(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    (pending / "example.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        WorkspaceError, match="legacy candidate directory is unavailable"
    ):
        workspace._existing_pending_paths(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("report", ["", "x" * 2], ids=["empty", "oversized"])
def test_report_write_rejects_empty_or_oversized_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(WorkspaceError, match="report size is invalid"):
        workspace._write_report(root, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]


def test_remove_pending_detects_candidate_directory_that_changes_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    pending = state / "pending"
    pending.mkdir(parents=True)
    candidates = state / "candidates"
    candidates.mkdir()
    original_optional = workspace._optional_lstat
    calls = 0

    def changed(path: Path, description: str) -> os.stat_result | None:
        nonlocal calls
        if path == candidates:
            calls += 1
            if calls == 2:
                candidates.rmdir()
                candidates.symlink_to(tmp_path, target_is_directory=True)
                return candidates.lstat()
        return original_optional(path, description)

    monkeypatch.setattr(workspace, "_optional_lstat", changed)
    with pytest.raises(WorkspaceError, match="state/candidates"):
        workspace._remove_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_recovery_directory_wraps_second_lstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    directory = state / ".pending-recovery"
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat
    calls = 0

    def fail_second(path: Path) -> os.stat_result:
        nonlocal calls
        if path == directory:
            calls += 1
            if calls == 2:
                raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_second)
    with pytest.raises(WorkspaceError, match="cannot stat pending recovery directory"):
        workspace._recovery_directory(state, create=False)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["size", "no-directory"], ids=["size", "no-directory"]
)
def test_recovery_writer_rejects_size_and_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    if fault == "size":
        monkeypatch.setattr(workspace, "_MAX_RECOVERY_RECORD_BYTES", 0)
        expected = "size is invalid"
    else:
        monkeypatch.setattr(
            workspace,
            "_recovery_directory",
            lambda *_args, **_kwargs: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "directory is unavailable"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_recovery_record(state, _replace_record())  # pyright: ignore[reportPrivateUsage]


def test_recovery_retirement_returns_when_directory_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    monkeypatch.setattr(
        workspace,
        "_recovery_directory",
        lambda *_args, **_kwargs: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_capture_pending_replacement_rejects_target_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    (target / "state.json").write_text("{}", encoding="utf-8")
    (target / "candidate.txt").write_text("old", encoding="utf-8")
    original_read = workspace._read_transaction_backup

    def replace_group(path: Path, description: str) -> bytes | None:
        data = original_read(path, description)
        if description == "candidate":
            (target / "state.json").unlink()
            (target / "candidate.txt").unlink()
            target.rmdir()
            target.write_text("raced", encoding="utf-8")
        return data

    monkeypatch.setattr(workspace, "_read_transaction_backup", replace_group)
    with pytest.raises(WorkspaceError, match="pending target must be"):
        workspace._capture_pending_replacement(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("data", [None, b"new"], ids=["delete", "replace"])
def test_restore_transaction_file_rejects_symlink_targets(
    tmp_path: Path, data: bytes | None
) -> None:
    path = tmp_path / "target"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._restore_transaction_file(path, data, "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_restore_transaction_file_wraps_restore_directory_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    original_fsync = workspace._fsync_directory

    def fail_pending(path_arg: Path) -> None:
        if path_arg == path.parent:
            raise OSError("injected")  # ruff: ignore[raw-string-in-exception]
        original_fsync(path_arg)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_pending)
    with pytest.raises(WorkspaceError, match="cannot fsync pending decision directory"):
        workspace._restore_transaction_file(path, b"new", "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_pending_temporary_cleanup_skips_entries_removed_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "pending"
    directory.mkdir()
    temp = directory / ".state.json.race.tmp"
    temp.write_text("x", encoding="utf-8")
    original_optional = workspace._optional_lstat

    def removed(path: Path, description: str) -> os.stat_result | None:
        if path == temp:
            temp.unlink()
            return None
        return original_optional(path, description)

    monkeypatch.setattr(workspace, "_optional_lstat", removed)
    workspace._remove_pending_write_temporaries(directory)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "layout",
    ["grouped-invalid", "unrecognized-file", "candidates-symlink", "fsync"],
    ids=["grouped-symlink", "nonempty-group", "candidates-symlink", "fsync"],
)
def test_restore_pending_replacement_rejects_unsafe_layout_or_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    pending = state / "pending"
    pending.mkdir()
    grouped = pending / "example"
    if layout == "grouped-invalid":
        grouped.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="pending target must be"):
            workspace._restore_pending_replacement(state, _replace_record())  # pyright: ignore[reportPrivateUsage]
        return
    if layout == "unrecognized-file":
        grouped.mkdir()
        (grouped / "keep.txt").write_text("x", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="cannot restore pending target"):
            workspace._restore_pending_replacement(state, _replace_record())  # pyright: ignore[reportPrivateUsage]
        return
    if layout == "candidates-symlink":
        (state / "candidates").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="state/candidates"):
            workspace._restore_pending_replacement(state, _replace_record())  # pyright: ignore[reportPrivateUsage]
        return
    original_fsync = workspace._fsync_directory

    def fail_pending(path: Path) -> None:
        if path == pending:
            raise OSError("injected")  # ruff: ignore[raw-string-in-exception]
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_pending)
    with pytest.raises(
        WorkspaceError, match="cannot fsync restored pending transaction"
    ):
        workspace._restore_pending_replacement(state, _replace_record())  # pyright: ignore[reportPrivateUsage]


def _legacy_undo() -> dict[str, object]:
    return _replace_record(layout="legacy", group_dir_existed=False)


def _valid_payload(data: bytes = b"new candidate") -> dict[str, object]:
    return _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize(
    "fault",
    ["no-legacy-files", "unsafe-legacy-file"],
    ids=["absent-legacy", "symlink-legacy"],
)
def test_install_pending_replacement_handles_legacy_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "pending").mkdir()
    candidates = state / "candidates"
    candidates.mkdir()
    if fault == "unsafe-legacy-file":
        (candidates / "example.txt").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(workspace, "_fsync_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    if fault == "unsafe-legacy-file":
        with pytest.raises(WorkspaceError, match="candidate must be a regular"):
            workspace._install_pending_replacement(  # pyright: ignore[reportPrivateUsage]
                state, _valid_payload(), b"new candidate", _legacy_undo()
            )
        return
    workspace._install_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, _valid_payload(), b"new candidate", _legacy_undo()
    )
    assert workspace._read_commit_record(state, "example") is not None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "mismatch", ["candidate", "state"], ids=["candidate", "pending-state"]
)
def test_install_pending_replacement_checks_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "pending").mkdir()
    (state / "candidates").mkdir()
    monkeypatch.setattr(workspace, "_fsync_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    if mismatch == "candidate":
        original_read = workspace._read_text_bytes
        candidate_path = state / "pending" / "example" / "candidate.txt"
        calls = 0

        def mismatch_read(path: Path, description: str) -> bytes:
            nonlocal calls
            if path == candidate_path:
                calls += 1
                if calls == 2:
                    return b"different"
            return original_read(path, description)

        monkeypatch.setattr(workspace, "_read_text_bytes", mismatch_read)
        expected = "pending candidate read-back mismatch"
    else:
        monkeypatch.setattr(workspace, "_read_pending", lambda *_args: {})  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        expected = "pending decision read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._install_pending_replacement(  # pyright: ignore[reportPrivateUsage]
            state, _valid_payload(), b"new candidate", _replace_record()
        )


def test_handle_monitor_result_returns_baseline_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    monkeypatch.setattr(
        workspace,
        "_promote_snapshot",
        lambda *_args, **_kwargs: {"action": "snapshot_conflict"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    target = {"target_id": "example", "name": "Example"}
    result = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, {"status": "baseline", "sha256": "a" * 64}, _RUN_ID
    )
    assert result["action"] == "snapshot_conflict"


def test_handle_monitor_result_rejects_mismatched_candidate_digest(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    target = {"target_id": "example", "name": "Example"}
    result = {
        "status": "changed",
        "sha256": "a" * 64,
        "previous_sha256": None,
    }
    with pytest.raises(WorkspaceError, match="does not match candidate"):
        workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
            state, target, result, _RUN_ID, candidate_data=b"candidate"
        )


def test_handle_monitor_result_rejects_unknown_status(tmp_path: Path) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    target = {"target_id": "example", "name": "Example"}
    with pytest.raises(WorkspaceError, match="unsupported status"):
        workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
            state, target, {"status": "unknown"}, _RUN_ID
        )


def test_read_pending_wraps_state_file_lstat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".wsum"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    path.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def fail(value: Path) -> os.stat_result:
        if value == path:
            raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(value)

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match="no valid pending decision"):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("kind", ["symlink", "directory"], ids=["symlink", "directory"])
def test_read_pending_rejects_nonregular_state_file(tmp_path: Path, kind: str) -> None:
    state = tmp_path / ".wsum"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_finalize_recovers_discard_cleanup_before_reading_pending_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".wsum"
    state.mkdir()
    (state / "pending").mkdir()
    discard = {
        "kind": "cleanup",
        "material": None,
        "purpose": "discard",
        "report_sha256": None,
        "revision": None,
        "run_id": None,
        "target_id": "example",
        "version": 1,
    }
    _write_recovery(state, discard)  # pyright: ignore[reportArgumentType]
    decision = {"target_id": "example", "revision": _REVISION, "material": False}
    with pytest.raises(WorkspaceError, match="no valid pending decision"):
        workspace.finalize(tmp_path, decision)


def test_finalize_rejects_pending_target_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    state = root / ".wsum"
    state.mkdir()
    monkeypatch.setattr(
        workspace,
        "_read_pending",
        lambda *_args: {"target_id": "other"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    decision = {"target_id": "example", "revision": _REVISION, "material": False}
    with pytest.raises(WorkspaceError, match="pending decision target does not match"):
        workspace.finalize(root, decision)


def test_finalize_checks_report_path_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    state = root / ".wsum"
    state.mkdir()
    pending = {
        "target_id": "example",
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "expected_sha256": None,
        "candidate_sha256": "b" * 64,
        "diff_truncated": False,
    }
    monkeypatch.setattr(workspace, "_read_pending", lambda *_args: pending)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        workspace,
        "_promote_snapshot",
        lambda *_args, **_kwargs: {"action": "snapshot_promoted"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        workspace,
        "_write_report",
        lambda *_args: root / "reports" / "actual.md",  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(workspace, "_write_recovery_record", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace, "_complete_cleanup_record", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        workspace,
        "_finalized_result",
        lambda *_args: {"action": "finalized", "report_path": "wrong"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    decision = {
        "target_id": "example",
        "revision": _REVISION,
        "material": True,
        "report": "material update",
    }
    with pytest.raises(WorkspaceError, match="finalized report path mismatch"):
        workspace.finalize(root, decision)


def test_read_decision_returns_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace.sys, "stdin", __import__("io").StringIO('{"material": false}')
    )
    assert workspace._read_decision() == {"material": False}  # pyright: ignore[reportPrivateUsage]


def test_module_entry_point_runs_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url,enabled\nExample,https://example.com/,false\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        workspace.sys,
        "argv",
        ["workspace.py", "--workspace", str(tmp_path), "check"],
    )
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(workspace.__file__)), run_name="__main__")
    assert exit_info.value.code == 0
    assert '"action": "skipped"' in capsys.readouterr().out
