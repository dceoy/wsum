"""Tests for durable per-target observation timestamps."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from argparse import Namespace

import pytest
import workspace


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
    kwargs = {
        "target_id": target_id,
        "run_id": _RUN_ID,
        "input_path": fetched,
        "link_depth": 0,
    }
    first = workspace.ingest_agent_fetch(tmp_path, targets, **kwargs)
    assert first["action"] == "baseline_created"
    recorded = workspace.observation_status(tmp_path, target_id=target_id)
    assert recorded["last_checked_at"] is not None
    assert recorded["baseline_updated_at"] is not None

    fetched.write_text("new\n")
    review = workspace.ingest_agent_fetch(tmp_path, targets, **kwargs)
    assert review["action"] == "review"
    assert workspace.observation_status(tmp_path, target_id=target_id)[
        "baseline_updated_at"
    ] == recorded["baseline_updated_at"]

    final = workspace.finalize(
        tmp_path,
        {
            "target_id": target_id,
            "revision": review["revision"],
            "material": False,
        },
    )
    assert final["action"] == "finalized"
    assert workspace.observation_status(tmp_path, target_id=target_id)[
        "baseline_updated_at"
    ] is not None
    assert (
        tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    ).read_text() == "new\n"


def test_status_command_and_corrupt_record_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, target_id = _targets(tmp_path)
    assert workspace.main([
        "--workspace", str(tmp_path), "status", "--target-id", target_id,
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "target_id": target_id,
        "last_checked_at": None,
        "baseline_updated_at": None,
    }

    state = tmp_path / "internal" / "state"
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


def test_observation_rejects_insecure_permissions(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    directory = tmp_path / "internal" / "state" / "observations"
    directory.mkdir(parents=True)
    record = directory / f"{target_id}.json"
    record.write_text('{"last_checked_at":null,"baseline_updated_at":null}')
    record.chmod(0o644)
    with pytest.raises(workspace.WorkspaceError, match="unsafe"):
        workspace.observation_status(tmp_path, target_id=target_id)


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
    state = tmp_path / "internal" / "state"
    workspace._record_observation(state, target_id, "last_checked_at")
    records = cast("list[dict[str, object]]", workspace.observation_status(tmp_path)["targets"])
    assert len(records) == 1
    assert records[0]["target_id"] == target_id
    assert records[0]["last_checked_at"] is not None
    assert records[0]["baseline_updated_at"] is None
    assert workspace.observation_status(tmp_path, target_id="not-monitored")[
        "last_checked_at"
    ] is None

    ignored = state / "observations" / ".leftover.tmp"
    ignored.write_text("discarded")
    assert len(cast("list[object]", workspace.observation_status(tmp_path)["targets"])) == 1
    (state / "observations" / "unexpected.txt").write_text("bad")
    with pytest.raises(workspace.WorkspaceError, match="unexpected"):
        workspace.observation_status(tmp_path)


def test_observation_listing_rejects_unsafe_temporary(tmp_path: Path) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
    workspace._record_observation(state, target_id, "last_checked_at")
    (state / "observations" / ".unexpected.tmp").mkdir()
    with pytest.raises(workspace.WorkspaceError, match="unexpected"):
        workspace.observation_status(tmp_path)


def test_observation_listing_handles_disappearing_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
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
    assert workspace.observation_status(tmp_path, target_id=target_id)[
        "last_checked_at"
    ] == "2099-01-01T00:00:00Z"


def test_record_observation_rejects_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"

    def failed_replace(_self: Path, _destination: Path) -> None:
        raise OSError("injected replace error")

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(workspace.WorkspaceError, match="cannot persist"):
        workspace._record_observation(state, target_id, "last_checked_at")


def test_record_observation_rejects_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target_id = _targets(tmp_path)
    state = tmp_path / "internal" / "state"
    original = workspace._fsync_directory

    def failed_fsync(path: Path) -> None:
        if path.name == "observations":
            raise OSError("injected fsync error")
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
    monkeypatch.setattr(
        workspace,
        "_monitor_target_from_source",
        lambda *_args, **_kwargs: {
            "action": "snapshot_conflict",
            "target_id": target_id,
        },
    )
    result = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=target_id,
        run_id=_RUN_ID,
        input_path=fetched,
    )
    assert result["action"] == "snapshot_conflict"
    assert workspace.observation_status(tmp_path)["targets"] == []
