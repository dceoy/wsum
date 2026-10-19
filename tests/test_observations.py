"""Tests for durable per-target observation timestamps."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import workspace

if TYPE_CHECKING:
    from argparse import Namespace


_RUN_ID = "20261001T000000Z-deadbeef"


def _targets(root: Path) -> tuple[Path, str]:
    path = root / "targets.csv"
    path.write_text(
        "name,url,enabled\nExample,https://example.com/,true\n",
        encoding="utf-8",
    )
    return path, str(workspace.load_targets(path)[0]["target_id"])


def test_status_has_no_invented_timestamps(tmp_path: Path) -> None:
    targets, target_id = _targets(tmp_path)
    del targets
    state = tmp_path / "internal" / "state"
    (state / "snapshots").mkdir(parents=True)
    (state / "snapshots" / f"{target_id}.txt").write_text("legacy baseline\n")
    assert workspace.observation_status(tmp_path) == {"targets": []}
    assert workspace.observation_status(tmp_path, target_id=target_id) == {
        "target_id": target_id,
        "last_checked_at": None,
        "baseline_updated_at": None,
    }


def test_check_records_success_not_failure_or_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets, target_id = _targets(tmp_path)

    def fake_monitor(
        _state: Path,
        target: dict[str, object],
        _run_id: str,
        *,
        link_depth: int,
        max_links: int,
    ) -> dict[str, object]:
        del link_depth, max_links
        return {
            "action": "unchanged",
            "target_id": target["target_id"],
            "name": target["name"],
        }

    monkeypatch.setattr(workspace, "_monitor_target", fake_monitor)
    result = workspace.check(tmp_path, targets)
    outcomes = cast("list[dict[str, object]]", result["targets"])
    assert outcomes[0]["action"] == "unchanged"
    recorded = workspace.observation_status(tmp_path, target_id=target_id)
    assert recorded["last_checked_at"] is not None
    assert recorded["baseline_updated_at"] is None

    targets.write_text(
        "name,url,enabled\nExample,https://example.com/,false\n", encoding="utf-8"
    )
    previous = recorded["last_checked_at"]
    assert workspace.check(tmp_path, targets)["targets"] == [
        {"action": "skipped", "target_id": target_id, "name": "Example"}
    ]
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)["last_checked_at"]
        == previous
    )


def test_baseline_promotion_and_changed_review_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets, target_id = _targets(tmp_path)
    fetched = tmp_path / "fetch.txt"
    fetched.write_text("old\n")

    def fake_run(args: Namespace) -> dict[str, object]:
        path = Path(args.input)
        output = Path(args.output)
        content = path.read_bytes()
        output.write_bytes(content)
        previous_path = args.previous
        previous = Path(previous_path).read_bytes() if previous_path else None
        return {
            "status": (
                "baseline"
                if previous is None
                else "unchanged"
                if previous == content
                else "changed"
            ),
            "sha256": hashlib.sha256(content).hexdigest(),
            "previous_sha256": (
                hashlib.sha256(previous).hexdigest() if previous else None
            ),
            "diff": "changed",
            "diff_truncated": False,
            "links": {},
        }

    monkeypatch.setattr(workspace.monitor, "run", fake_run)
    first = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=target_id,
        run_id=_RUN_ID,
        input_path=fetched,
        link_depth=0,
    )
    assert first["action"] == "baseline_created"
    recorded = workspace.observation_status(tmp_path, target_id=target_id)
    assert recorded["last_checked_at"] is not None
    assert recorded["baseline_updated_at"] is not None

    fetched.write_text("new\n")
    review = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=target_id,
        run_id=_RUN_ID,
        input_path=fetched,
        link_depth=0,
    )
    assert review["action"] == "review"
    observation = workspace.observation_status(tmp_path, target_id=target_id)
    assert observation["baseline_updated_at"] == recorded["baseline_updated_at"]

    final = workspace.finalize(
        tmp_path,
        {
            "target_id": target_id,
            "revision": review["revision"],
            "material": False,
        },
    )
    assert final["action"] == "finalized"
    observation = workspace.observation_status(tmp_path, target_id=target_id)
    assert observation["baseline_updated_at"] is not None
    assert (
        tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    ).read_text() == "new\n"


def test_status_command_and_corrupt_record_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, target_id = _targets(tmp_path)
    argv = ["--workspace", str(tmp_path), "status", "--target-id", target_id]
    assert workspace.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {
        "target_id": target_id,
        "last_checked_at": None,
        "baseline_updated_at": None,
    }

    state = workspace._state_dir(tmp_path)
    workspace._record_observation(state, target_id, "last_checked_at")
    path = state / "observations" / f"{target_id}.json"
    path.write_text('{"last_checked_at":"yesterday","baseline_updated_at":null}')
    with pytest.raises(workspace.WorkspaceError, match="timestamp is invalid"):
        workspace.observation_status(tmp_path, target_id=target_id)


def test_observation_rejects_symlink(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text("{}")
    directory = state / "observations"
    directory.mkdir()
    path = directory / f"{target_id}.json"
    try:
        path.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported")
    with pytest.raises(workspace.WorkspaceError, match="unsafe"):
        workspace.observation_status(tmp_path, target_id=target_id)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not valid json", "cannot read"),
        ("[]", "record is invalid"),
        ('{"last_checked_at":null}', "record is invalid"),
        ('{"last_checked_at":123,"baseline_updated_at":null}', "timestamp is invalid"),
        (
            '{"last_checked_at":"2026-1-1T0:0:0Z","baseline_updated_at":null}',
            "timestamp is invalid",
        ),
        ("", "record is unsafe"),
        ("x" * 1025, "record is unsafe"),
    ],
)
def test_observation_rejects_invalid_records(
    tmp_path: Path, content: str, message: str
) -> None:
    _, target_id = _targets(tmp_path)
    directory = tmp_path / "internal" / "state" / "observations"
    directory.mkdir(parents=True)
    record = directory / f"{target_id}.json"
    record.write_text(content)
    record.chmod(0o600)
    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.observation_status(tmp_path, target_id=target_id)


def test_observation_accepts_gws_restored_permissions(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    directory = tmp_path / "internal" / "state" / "observations"
    directory.mkdir(parents=True)
    record = directory / f"{target_id}.json"
    record.write_text('{"last_checked_at":null,"baseline_updated_at":null}')
    record.chmod(0o644)
    result = workspace.observation_status(tmp_path, target_id=target_id)
    assert result["last_checked_at"] is None
    assert result["baseline_updated_at"] is None


def test_observation_rejects_nonregular_record(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    directory = tmp_path / "internal" / "state" / "observations"
    directory.mkdir(parents=True)
    (directory / f"{target_id}.json").mkdir()
    with pytest.raises(workspace.WorkspaceError, match="unsafe"):
        workspace.observation_status(tmp_path, target_id=target_id)


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_observation_rejects_unsafe_directory(tmp_path: Path, kind: str) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    observation = state / "observations"
    if kind == "directory":
        observation.write_text("not a directory")
    else:
        try:
            observation.symlink_to(tmp_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not supported")
    with pytest.raises(workspace.WorkspaceError, match="non-symlink directory"):
        workspace.observation_status(tmp_path, target_id=target_id)


def test_observation_listing_and_missing_record(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    workspace._record_observation(state, target_id, "last_checked_at")
    result = workspace.observation_status(tmp_path)
    records = cast("list[dict[str, object]]", result["targets"])
    assert len(records) == 1
    assert records[0]["target_id"] == target_id
    assert records[0]["last_checked_at"] is not None
    assert records[0]["baseline_updated_at"] is None
    missing = workspace.observation_status(tmp_path, target_id="not-monitored")
    assert missing["last_checked_at"] is None

    ignored = state / "observations" / ".leftover.tmp"
    ignored.write_text("discarded")
    result = workspace.observation_status(tmp_path)
    assert len(cast("list[object]", result["targets"])) == 1
    (state / "observations" / "unexpected.txt").write_text("bad")
    with pytest.raises(workspace.WorkspaceError, match="unexpected"):
        workspace.observation_status(tmp_path)


def test_observation_listing_rejects_unsafe_temporary(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    workspace._record_observation(state, target_id, "last_checked_at")
    (state / "observations" / ".unexpected.tmp").mkdir()
    with pytest.raises(workspace.WorkspaceError, match="unexpected"):
        workspace.observation_status(tmp_path)


def test_observation_listing_handles_disappearing_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    workspace._record_observation(state, target_id, "last_checked_at")
    temporary = state / "observations" / ".race.tmp"
    temporary.write_text("temp")
    original = workspace._optional_lstat

    def disappearing(path: Path, description: str) -> object:
        if path == temporary:
            return None
        return original(path, description)

    monkeypatch.setattr(workspace, "_optional_lstat", disappearing)
    with pytest.raises(workspace.WorkspaceError, match="unexpected"):
        workspace.observation_status(tmp_path)


def test_record_observation_rejects_unknown_field(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    with pytest.raises(workspace.WorkspaceError, match="unsupported"):
        workspace._record_observation(
            tmp_path / "internal" / "state", target_id, "unknown"
        )


def test_record_observation_never_moves_time_backward(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
    directory = state / "observations"
    directory.mkdir(parents=True)
    record = directory / f"{target_id}.json"
    record.write_text(
        '{"last_checked_at":"2099-01-01T00:00:00Z","baseline_updated_at":null}'
    )
    record.chmod(0o600)
    workspace._record_observation(state, target_id, "last_checked_at")
    current = workspace.observation_status(tmp_path, target_id=target_id)
    assert current["last_checked_at"] == "2099-01-01T00:00:00Z"


def test_record_observation_rejects_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)

    def failed_replace(_self: Path, _destination: Path) -> None:
        raise OSError

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(workspace.WorkspaceError, match="cannot persist"):
        workspace._record_observation(state, target_id, "last_checked_at")


def test_record_observation_rejects_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    original = workspace._fsync_directory

    def failed_fsync(path: Path) -> None:
        if path.name == "observations":
            raise OSError
        original(path)

    monkeypatch.setattr(workspace, "_fsync_directory", failed_fsync)
    with pytest.raises(workspace.WorkspaceError, match="cannot fsync observation"):
        workspace._record_observation(state, target_id, "last_checked_at")


def test_check_conflict_does_not_record_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets, target_id = _targets(tmp_path)

    def fake_monitor(
        _state: Path,
        _target: dict[str, object],
        _run_id: str,
        *,
        link_depth: int,
        max_links: int,
    ) -> dict[str, object]:
        del link_depth, max_links
        return {"action": "snapshot_conflict", "target_id": target_id}

    monkeypatch.setattr(workspace, "_monitor_target", fake_monitor)
    workspace.check(tmp_path, targets)
    assert workspace.observation_status(tmp_path)["targets"] == []


def test_ingest_conflict_does_not_record_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets, target_id = _targets(tmp_path)
    fetched = tmp_path / "fetch.txt"
    fetched.write_text("content")

    def fake_conflict(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"action": "snapshot_conflict", "target_id": target_id}

    monkeypatch.setattr(workspace, "_monitor_target_from_source", fake_conflict)
    result = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=target_id,
        run_id=_RUN_ID,
        input_path=fetched,
    )
    assert result["action"] == "snapshot_conflict"
    assert workspace.observation_status(tmp_path)["targets"] == []


def _ingest(root: Path, targets: Path, target_id: str, text: str) -> dict[str, object]:
    fetched = root / "fetch.txt"
    fetched.write_text(text)
    return workspace.ingest_agent_fetch(
        root,
        targets,
        target_id=target_id,
        run_id=_RUN_ID,
        input_path=fetched,
        link_depth=0,
    )


def _break_observations(
    root: Path, target_id: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = root / "internal" / "state" / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / f"{target_id}.json"
    if kind == "corrupt":
        record.write_text("broken json")
    elif kind == "symlink":
        record.unlink(missing_ok=True)
        external = root / "external.json"
        external.write_text("untouched")
        record.symlink_to(external)
    else:
        original = workspace._write_temporary_file

        def failed_write(destination: Path, data: bytes, description: str) -> Path:
            if description == "observation record":
                raise OSError
            return original(destination, data, description)

        monkeypatch.setattr(workspace, "_write_temporary_file", failed_write)


@pytest.mark.parametrize("kind", ["corrupt", "write", "symlink"])
def test_ingest_metadata_failure_preserves_core_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    targets, target_id = _targets(tmp_path)
    _break_observations(tmp_path, target_id, kind, monkeypatch)
    assert (
        _ingest(tmp_path, targets, target_id, "old\n")["action"] == "baseline_created"
    )
    assert _ingest(tmp_path, targets, target_id, "old\n")["action"] == "unchanged"
    assert _ingest(tmp_path, targets, target_id, "new\n")["action"] == "review"
    assert "observation warning" in capsys.readouterr().err
    snapshot = tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    assert snapshot.read_text() == "old\n"
    if kind == "symlink":
        assert (tmp_path / "external.json").read_text() == "untouched"


@pytest.mark.parametrize("kind", ["corrupt", "write", "symlink"])
@pytest.mark.parametrize("compact", [False, True])
def test_check_metadata_failure_preserves_core_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    compact: bool,
) -> None:
    targets, target_id = _targets(tmp_path)
    fetched = tmp_path / "fetch.txt"
    fetched.write_text("old\n")

    def local_monitor(
        state: Path,
        target: dict[str, object],
        run_id: str,
        *,
        link_depth: int,
        max_links: int,
    ) -> dict[str, object]:
        return workspace._monitor_target_from_source(
            state,
            target,
            run_id,
            ["--input", str(fetched)],
            link_depth=link_depth,
            max_links=max_links,
        )

    monkeypatch.setattr(workspace, "_monitor_target", local_monitor)
    _break_observations(tmp_path, target_id, kind, monkeypatch)
    for text, action in [
        ("old\n", "baseline_created"),
        ("old\n", "unchanged"),
        ("new\n", "review"),
    ]:
        fetched.write_text(text)
        result = workspace.check(tmp_path, targets, compact=compact, link_depth=0)
        assert cast("list[dict[str, object]]", result["targets"])[0]["action"] == action
    assert "observation warning" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["corrupt", "write", "symlink"])
@pytest.mark.parametrize("archive", [False, True])
def test_finalize_metadata_failure_preserves_core_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    archive: bool,
) -> None:
    targets, target_id = _targets(tmp_path)
    _ingest(tmp_path, targets, target_id, "old\n")
    review = _ingest(tmp_path, targets, target_id, "new\n")
    _break_observations(tmp_path, target_id, kind, monkeypatch)
    result = workspace.finalize(
        tmp_path,
        {
            "target_id": target_id,
            "revision": review["revision"],
            "material": True,
            "report": "Changed content.\n",
        },
        targets=targets,
        archive_evidence=archive,
    )
    assert result["action"] == "finalized"
    snapshot = tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    assert snapshot.read_text() == "new\n"
    assert "observation warning" in capsys.readouterr().err
    if kind == "write":
        assert (
            workspace.observation_status(tmp_path, target_id=target_id)[
                "baseline_updated_at"
            ]
            is None
        )


@pytest.mark.parametrize("archive", [False, True])
def test_finalize_retry_preserves_prepared_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive: bool,
) -> None:
    targets, target_id = _targets(tmp_path)
    _ingest(tmp_path, targets, target_id, "old\n")
    review = _ingest(tmp_path, targets, target_id, "new\n")
    decision = {
        "target_id": target_id,
        "revision": review["revision"],
        "material": True,
        "report": "Changed content.\n",
    }
    original_report = workspace._write_report

    def fail_report(*_args: object, **_kwargs: object) -> Path:
        message = "report failed after snapshot promotion"
        raise workspace.WorkspaceError(message)

    monkeypatch.setattr(workspace, "_write_report", fail_report)
    with pytest.raises(workspace.WorkspaceError, match="report failed"):
        workspace.finalize(
            tmp_path, decision, targets=targets, archive_evidence=archive
        )
    state = tmp_path / "internal" / "state"
    path = state / "observations" / f"{target_id}.json"
    record = json.loads(path.read_text())
    # A distinct persisted timestamp makes accidental use of retry time observable.
    record["pending_baseline_updated_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(workspace, "_write_report", original_report)
    assert (
        workspace.finalize(
            tmp_path, decision, targets=targets, archive_evidence=archive
        )["action"]
        == "finalized"
    )
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        == "2020-01-01T00:00:00Z"
    )
    # A later source observation consolidates the matching prepared pair.
    assert _ingest(tmp_path, targets, target_id, "new\n")["action"] == "unchanged"
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        == "2020-01-01T00:00:00Z"
    )


def test_failed_snapshot_replace_preserves_previous_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets, target_id = _targets(tmp_path)
    _ingest(tmp_path, targets, target_id, "old\n")
    previous = workspace.observation_status(tmp_path, target_id=target_id)
    review = _ingest(tmp_path, targets, target_id, "new\n")
    original = Path.replace

    def fail_snapshot(path: Path, destination: Path) -> Path:
        if destination.parent.name == "snapshots":
            raise OSError
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", fail_snapshot)
    with pytest.raises(workspace.WorkspaceError, match="cannot promote snapshot"):
        workspace.finalize(
            tmp_path,
            {"target_id": target_id, "revision": review["revision"], "material": False},
        )
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        == previous["baseline_updated_at"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_sha256", "invalid"),
        ("pending_baseline_sha256", "invalid"),
        ("pending_baseline_updated_at", None),
        ("pending_baseline_updated_at", "yesterday"),
        ("baseline_mtime_ns", 1),
        ("baseline_mtime_ns", "-1"),
        ("pending_baseline_mtime_ns", "9" * 19),
        ("pending_baseline_mtime_ns", "9" * 20),
    ],
)
def test_invalid_observation_binding(
    tmp_path: Path,
    field: str,
    value: str | int | None,
) -> None:
    targets, target_id = _targets(tmp_path)
    _ingest(tmp_path, targets, target_id, "old\n")
    path = tmp_path / "internal" / "state" / "observations" / f"{target_id}.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(workspace.WorkspaceError):
        workspace.observation_status(tmp_path, target_id=target_id)


def test_observation_legacy_baseline_is_unassociated(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    directory = state / "observations"
    directory.mkdir()
    (directory / f"{target_id}.json").write_text(
        json.dumps({
            "last_checked_at": "2020-01-01T00:00:00Z",
            "baseline_updated_at": "2020-01-01T00:00:00Z",
        })
    )
    (state / "snapshots").mkdir()
    status = workspace.observation_status(tmp_path, target_id=target_id)
    assert status["last_checked_at"] == "2020-01-01T00:00:00Z"
    assert status["baseline_updated_at"] is None


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_observation_rejects_unsafe_snapshot_directory(
    tmp_path: Path, kind: str
) -> None:
    _, target_id = _targets(tmp_path)
    state = workspace._state_dir(tmp_path)
    path = state / "snapshots"
    if kind == "file":
        path.write_text("unsafe")
    else:
        path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(workspace.WorkspaceError, match="snapshot directory"):
        workspace.observation_status(tmp_path, target_id=target_id)


@pytest.mark.parametrize("clock", [0, 10**18], ids=["rollback", "coarse"])
def test_returning_content_during_metadata_outage_has_unknown_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: int,
) -> None:
    targets, target_id = _targets(tmp_path)
    monkeypatch.setattr(workspace, "time_ns", lambda: clock)
    _ingest(tmp_path, targets, target_id, "A\n")
    snapshot = tmp_path / "internal/state/snapshots" / f"{target_id}.txt"
    first_stamp = snapshot.stat().st_mtime_ns
    review = _ingest(tmp_path, targets, target_id, "B\n")
    decision = {
        "target_id": target_id,
        "revision": review["revision"],
        "material": False,
    }
    assert workspace.finalize(tmp_path, decision)["action"] == "finalized"
    second_stamp = snapshot.stat().st_mtime_ns
    assert second_stamp > first_stamp
    with monkeypatch.context() as outage:
        _break_observations(tmp_path, target_id, "write", outage)
        review = _ingest(tmp_path, targets, target_id, "A\n")
        decision["revision"] = review["revision"]
        assert workspace.finalize(tmp_path, decision)["action"] == "finalized"
    assert snapshot.stat().st_mtime_ns > second_stamp
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        is None
    )
    assert _ingest(tmp_path, targets, target_id, "A\n")["action"] == "unchanged"
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        is None
    )


def test_snapshot_identity_rejects_inexact_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets, target_id = _targets(tmp_path)
    original = os.utime

    def inexact(path: Path, *, ns: tuple[int, int]) -> None:
        original(path, ns=(ns[0] - 1, ns[1] - 1))

    monkeypatch.setattr(workspace.os, "utime", inexact)
    with pytest.raises(workspace.WorkspaceError, match="identity precision"):
        _ingest(tmp_path, targets, target_id, "content\n")
    assert not (tmp_path / "internal/state/snapshots" / f"{target_id}.txt").exists()
    assert (
        workspace.observation_status(tmp_path, target_id=target_id)[
            "baseline_updated_at"
        ]
        is None
    )
