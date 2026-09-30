# pyright: reportPrivateUsage=false
"""Tests for workspace-local snapshot and report persistence."""

from __future__ import annotations

import hashlib
import stat
from typing import TYPE_CHECKING

import pytest
import workspace
from workspace import WorkspaceError

_RUN_ID = "20261001T000000Z-deadbeef"

if TYPE_CHECKING:
    from pathlib import Path


def _state(tmp_path: Path) -> Path:
    state = tmp_path / ".wsum"
    state.mkdir()
    return state


def _candidate(state: Path, content: str = "next\n") -> tuple[Path, str]:
    directory = state / "candidates"
    directory.mkdir()
    path = directory / "example.txt"
    path.write_text(content)
    return path, hashlib.sha256(content.encode()).hexdigest()


def _promote(
    state: Path,
    candidate: Path,
    digest: str,
    *,
    expected: str | None = None,
) -> dict[str, object]:
    return workspace._promote_snapshot(
        state,
        candidate,
        target_id="example",
        expected_sha256=expected,
        candidate_sha256=digest,
    )


def test_write_report_creates_private_run_report(tmp_path: Path) -> None:
    report = "## Example\n\nA material update.\n"

    destination = workspace._write_report(tmp_path, _RUN_ID, "example", report)

    reports = tmp_path / "reports"
    content = destination.read_text()
    assert destination == reports / f"{_RUN_ID}.md"
    assert f"Run: `{_RUN_ID}`" in content
    assert report.strip() in content
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_report_aggregates_targets_into_one_run_file(tmp_path: Path) -> None:
    workspace._write_report(tmp_path, _RUN_ID, "one", "## One\n\nFirst.\n")
    workspace._write_report(tmp_path, _RUN_ID, "two", "## Two\n\nSecond.\n")

    reports = list((tmp_path / "reports").glob("*.md"))
    assert reports == [tmp_path / "reports" / f"{_RUN_ID}.md"]
    content = reports[0].read_text()
    assert "## One" in content
    assert "## Two" in content


def test_write_report_replaces_existing_target_section(tmp_path: Path) -> None:
    destination = workspace._write_report(
        tmp_path, _RUN_ID, "example", "## Example\n\nOld report.\n"
    )

    workspace._write_report(tmp_path, _RUN_ID, "example", "## Example\n\nNew report.\n")

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
        workspace._write_report(tmp_path, _RUN_ID, target_id, "update\n")


def test_write_report_rejects_invalid_run_id(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="invalid_run_id"):
        workspace._write_report(tmp_path, "../escape", "example", "update\n")

    assert not (tmp_path / "reports").exists()


def test_write_report_rejects_reserved_section_markers(tmp_path: Path) -> None:
    report = "<!-- wsum:target other:start -->\nInjected section\n"

    with pytest.raises(WorkspaceError, match="reserved marker"):
        workspace._write_report(tmp_path, _RUN_ID, "example", report)

    assert not (tmp_path / "reports" / f"{_RUN_ID}.md").exists()


def test_write_report_rejects_symlinked_workspace_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    workspace_root = tmp_path / "workspace-link"
    workspace_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        workspace._write_report(workspace_root, _RUN_ID, "example", "update\n")

    assert not (outside / "reports").exists()


def test_write_report_rejects_symlinked_reports_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    (tmp_path / "reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")

    assert not (outside / f"{_RUN_ID}.md").exists()


def test_write_report_rejects_symlinked_destination(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("original\n")
    destination = reports / f"{_RUN_ID}.md"
    destination.symlink_to(outside)

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")

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
        workspace._write_report(tmp_path, _RUN_ID, "example", "new report\n")

    assert destination.read_text() == "old report\n"
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_write_report_fsyncs_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")

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
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")

    content = (reports / f"{_RUN_ID}.md").read_text()
    assert f"Run: `{_RUN_ID}`" in content
    assert "update" in content
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_promote_snapshot_rejects_symlinked_state_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    state = tmp_path / ".wsum"
    state.symlink_to(outside, target_is_directory=True)
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("next\n")
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, candidate, digest)

    assert not (outside / "candidates").exists()
    assert not (outside / "snapshots").exists()


def test_promote_snapshot_creates_baseline(tmp_path: Path) -> None:
    state = _state(tmp_path)
    candidate, digest = _candidate(state)

    result = _promote(state, candidate, digest)

    snapshot = state / "snapshots" / "example.txt"
    assert result["action"] == "snapshot_promoted"
    assert result["sha256"] == digest
    assert snapshot.read_text() == "next\n"


def test_promote_snapshot_replaces_expected_baseline(tmp_path: Path) -> None:
    state = _state(tmp_path)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    old = b"old\n"
    (snapshots / "example.txt").write_bytes(old)
    candidate, digest = _candidate(state)

    result = _promote(
        state,
        candidate,
        digest,
        expected=hashlib.sha256(old).hexdigest(),
    )

    assert result["applied"] is True
    assert (snapshots / "example.txt").read_text() == "next\n"


def test_promote_snapshot_reports_stale_baseline(tmp_path: Path) -> None:
    state = _state(tmp_path)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    current = b"current\n"
    (snapshots / "example.txt").write_bytes(current)
    candidate, digest = _candidate(state)

    result = _promote(state, candidate, digest, expected="0" * 64)

    assert result == {
        "action": "snapshot_conflict",
        "applied": False,
        "current_sha256": hashlib.sha256(current).hexdigest(),
    }
    assert (snapshots / "example.txt").read_bytes() == current


def test_promote_snapshot_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    state = _state(tmp_path)
    candidate, _ = _candidate(state)

    with pytest.raises(WorkspaceError, match="does not match"):
        _promote(state, candidate, "0" * 64)


def test_promote_snapshot_rejects_candidate_outside_state(tmp_path: Path) -> None:
    state = _state(tmp_path)
    (state / "candidates").mkdir()
    candidate = tmp_path / "outside.txt"
    candidate.write_text("next\n")
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="state/candidates"):
        _promote(state, candidate, digest)


def test_promote_snapshot_rejects_symlink_candidate(tmp_path: Path) -> None:
    state = _state(tmp_path)
    candidates = state / "candidates"
    candidates.mkdir()
    target = candidates / "target.txt"
    target.write_text("next\n")
    candidate = candidates / "link.txt"
    candidate.symlink_to(target)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        _promote(state, candidate, digest)


@pytest.mark.parametrize(
    "directory_name",
    ["candidates", "snapshots", "reports"],
)
def test_state_directories_reject_symlinks(tmp_path: Path, directory_name: str) -> None:
    state = _state(tmp_path)
    outside = tmp_path / f"outside-{directory_name}"
    outside.mkdir()

    if directory_name == "reports":
        (tmp_path / "reports").symlink_to(outside, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="non-symlink directory"):
            workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")
        return

    if directory_name == "candidates":
        candidate = outside / "example.txt"
        candidate.write_text("next\n")
        (state / "candidates").symlink_to(outside, target_is_directory=True)
        digest = hashlib.sha256(b"next\n").hexdigest()
    else:
        candidate, digest = _candidate(state)
        (state / "snapshots").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, candidate, digest)
    assert not (outside / "example.txt").exists() or directory_name == "candidates"


def test_promote_snapshot_fsyncs_snapshot_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    candidate, digest = _candidate(state)
    (state / "snapshots").mkdir()
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    result = _promote(state, candidate, digest)

    assert result["action"] == "snapshot_promoted"
    assert fsynced == [state / "snapshots"]


def test_promote_snapshot_fsyncs_state_when_creating_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    candidate, digest = _candidate(state)
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    result = _promote(state, candidate, digest)

    assert result["action"] == "snapshot_promoted"
    assert fsynced == [state, state / "snapshots"]


def test_promote_snapshot_reports_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    candidate, digest = _candidate(state)
    (state / "snapshots").mkdir()

    def fail(_: Path) -> None:
        raise OSError

    monkeypatch.setattr(workspace, "_fsync_directory", fail)
    with pytest.raises(WorkspaceError, match="cannot fsync snapshot directory"):
        _promote(state, candidate, digest)
    assert (state / "snapshots" / "example.txt").read_text() == "next\n"


def test_promote_snapshot_retries_idempotently(tmp_path: Path) -> None:
    state = _state(tmp_path)
    candidate, digest = _candidate(state)

    _promote(state, candidate, digest)
    result = _promote(state, candidate, digest)

    assert result == {
        "action": "snapshot_promoted",
        "applied": True,
        "already": True,
        "path": str(state / "snapshots" / "example.txt"),
        "sha256": digest,
    }
