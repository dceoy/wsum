"""Parametrized filesystem edge cases for workspace persistence helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import hashlib
import json
import os
import runpy
from pathlib import Path
from typing import Any

import pytest
import workspace
from workspace import WorkspaceError

_RUN_ID = "20261001T000000Z-deadbeef"
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

    def racing_mkdir(path: Path, *_args: Any, **_kwargs: Any) -> None:
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
            raise error("injected")
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
                raise PermissionError("injected")
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
                raise PermissionError("injected")
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
            raise PermissionError("injected")
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
            Path, "read_bytes", lambda _path: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_lstat = Path.lstat

        def fail(target: Path) -> os.stat_result:
            if target == path:
                raise PermissionError("injected")
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
            Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
                    raise OSError("injected")
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
                raise OSError("injected")
            original_fsync(path)

        monkeypatch.setattr(workspace, "_fsync_directory", fail_pending_fsync)
    with pytest.raises(WorkspaceError, match="cannot (remove|fsync) pending"):
        workspace._remove_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("fault", ["replace", "fsync"], ids=["replace", "fsync"])
def test_pending_file_wraps_atomic_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    destination = tmp_path / "pending" / "state.json"
    destination.parent.mkdir()
    if fault == "replace":
        monkeypatch.setattr(
            Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        with pytest.raises(WorkspaceError, match="non-symlink|private"):
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
            Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
            Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
            Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
            Path, "iterdir", lambda _path: (_ for _ in ()).throw(OSError("injected"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
def test_replacement_matches_commit_handles_missing_malformed_and_unsafe_paths(
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
        WorkspaceError, match="no valid pending|pending decision is invalid"
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

        def __iter__(self) -> Any:
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
                raise PermissionError("injected")
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
            workspace, "_recovery_directory", lambda *_args, **_kwargs: None  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        workspace, "_recovery_directory", lambda *_args, **_kwargs: None  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
            raise OSError("injected")
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
            raise OSError("injected")
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
            raise PermissionError("injected")
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
        workspace, "_read_pending", lambda *_args: {"target_id": "other"}  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        workspace, "_write_report", lambda *_args: root / "reports" / "actual.md"  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
