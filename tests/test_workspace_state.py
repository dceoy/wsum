"""Tests for workspace-local snapshot and report persistence."""

from __future__ import annotations

import hashlib
import stat
from typing import TYPE_CHECKING

import pytest
import workspace
from workspace import WorkspaceError

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


def test_write_report_creates_private_report(tmp_path: Path) -> None:
    report = "# Example\n\nA material update.\n"

    destination = workspace._write_report(tmp_path, "example", report)

    reports = tmp_path / "reports"
    assert destination == reports / "example.md"
    assert destination.read_text() == report
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_report_replaces_existing_regular_report(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    destination = reports / "example.md"
    destination.write_text("old report\n")

    workspace._write_report(tmp_path, "example", "new report\n")

    assert destination.read_text() == "new report\n"


@pytest.mark.parametrize(
    "target_id",
    ["../escape", "/absolute", "nested/path"],
    ids=["traversal", "absolute", "separator"],
)
def test_write_report_rejects_invalid_target_id(
    tmp_path: Path, target_id: str
) -> None:
    with pytest.raises(WorkspaceError, match="invalid_target_id"):
        workspace._write_report(tmp_path, target_id, "update\n")


def test_write_report_rejects_symlinked_reports_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    (tmp_path / "reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        workspace._write_report(tmp_path, "example", "update\n")

    assert not (outside / "example.md").exists()


def test_write_report_rejects_symlinked_destination(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("original\n")
    destination = reports / "example.md"
    destination.symlink_to(outside)

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._write_report(tmp_path, "example", "update\n")

    assert destination.is_symlink()
    assert outside.read_text() == "original\n"


def test_write_report_preserves_destination_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    destination = reports / "example.md"
    destination.write_text("old report\n")

    def fail_replace(_: Path, __: Path) -> Path:
        raise OSError

    monkeypatch.setattr(workspace.Path, "replace", fail_replace)
    with pytest.raises(WorkspaceError, match="cannot write report"):
        workspace._write_report(tmp_path, "example", "new report\n")

    assert destination.read_text() == "old report\n"
    assert not list(reports.glob(".example.md.*.tmp"))


def test_write_report_fsyncs_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    workspace._write_report(tmp_path, "example", "update\n")

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
        workspace._write_report(tmp_path, "example", "update\n")

    assert (reports / "example.md").read_text() == "update\n"
    assert not list(reports.glob(".example.md.*.tmp"))


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
def test_state_directories_reject_symlinks(
    tmp_path: Path, directory_name: str
) -> None:
    state = _state(tmp_path)
    outside = tmp_path / f"outside-{directory_name}"
    outside.mkdir()

    if directory_name == "reports":
        (tmp_path / "reports").symlink_to(outside, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="non-symlink directory"):
            workspace._write_report(tmp_path, "example", "update\n")
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
