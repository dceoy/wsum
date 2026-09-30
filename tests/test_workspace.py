"""Tests for the agent-facing CSV workflow."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, cast

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
    payload = {
        "target_id": "example",
        "run_id": _RUN_ID,
        "revision": "b" * 32,
        "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
        "candidate_sha256": hashlib.sha256(b"third\n").hexdigest(),
        "diff_truncated": False,
    }

    undo_retirement_error = "injected undo retirement failure"

    def fail_undo_retirement(_state: Path, _target_id: str) -> None:
        raise WorkspaceError(undo_retirement_error)

    monkeypatch.setattr(workspace, "_retire_recovery_record", fail_undo_retirement)
    workspace._write_pending_transaction(  # pyright: ignore[reportPrivateUsage]
        state, payload, b"third\n"
    )
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
