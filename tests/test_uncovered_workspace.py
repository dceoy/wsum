"""Boundary and fault-injection tests for workspace persistence helpers."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import workspace
from workspace import WorkspaceError

if TYPE_CHECKING:
    import os

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
def test_ensure_directory_wraps_filesystem_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "state"
    if failure == "mkdir":
        original_lstat = Path.lstat

        def missing(target: Path) -> os.stat_result:
            if target == path:
                raise FileNotFoundError
            return original_lstat(target)

        def fail_mkdir(_target: Path, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("mkdir failed")

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
                raise OSError("restat failed")
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_restat)
        original_mkdir = Path.mkdir

        def create(target: Path, *args: Any, **kwargs: Any) -> None:
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
            lambda _target: (_ for _ in ()).throw(OSError("sync failed")),
        )
    else:
        original_lstat = Path.lstat

        def fail_lstat(target: Path) -> os.stat_result:
            if target == path:
                raise PermissionError("cannot stat")
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(WorkspaceError, match="unavailable|fsync parent"):
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
        lambda _url: (_ for _ in ()).throw(ValueError("bad URL")),
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
    function = {
        "target": workspace._validate_target_id,
        "run": workspace._validate_run_id,
        "sha": lambda item: workspace._validate_sha256(item, "digest"),
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
        metadata, candidate, found = workspace._existing_pending_paths(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )
        assert found == layout
        assert metadata.name == (
            "state.json" if layout == "grouped" else "example.json"
        )
        assert (
            candidate.name == "candidate.txt"
            if layout == "grouped"
            else candidate.name == "example.txt"
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
        Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy"))
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
            raise PermissionError("denied")
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
        workspace.sys, "stdin", type("Input", (), {"read": lambda _self: payload})()
    )
    with pytest.raises(WorkspaceError, match="stdin|object"):
        workspace._read_decision()  # pyright: ignore[reportPrivateUsage]
