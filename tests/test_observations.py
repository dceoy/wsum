"""Tests for durable per-target observation timestamps."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path
from typing import cast

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
    assert cast("list[dict[str, object]]", result["targets"])[0]["action"] == "unchanged"
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
