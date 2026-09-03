"""Tests for deterministic workflow decisions."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - trusted CLI
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import workflow
from workflow import (
    LocalNotificationStore,
    LocalStoreError,
    WorkflowError,
    change_action,
    notification_step,
    validate_targets,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def _target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "enabled": True,
    }
    target.update(overrides)
    return target


def _event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_id": "example",
        "sha256": "a" * 64,
        "destination": "alerts",
        "message": "changed",
        "signal": "start",
        "notification": None,
    }
    payload.update(overrides)
    return payload


def _dict_field(result: Mapping[str, object], field: str) -> dict[str, object]:
    value = result.get(field)
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _record(result: Mapping[str, object]) -> dict[str, object]:
    return _dict_field(result, "notification")


def _local_cas_payload(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "protocol_version": result["protocol_version"],
        "action": result["action"],
        "expected_notification": result["expected_notification"],
        "notification": result["notification"],
        "next_signal": result["next_signal"],
    }


def _run_local_cas(
    directive: Mapping[str, object], runtime_dir: Path
) -> dict[str, object]:
    return workflow.run(
        "local-notification-cas",
        _local_cas_payload(directive),
        runtime_dir=runtime_dir,
    )


def test_validate_targets_normalizes_defaults() -> None:
    result = validate_targets({"persistence_mode": "local", "targets": [_target()]})

    assert result["persistence_mode"] == "local"
    assert result["targets"] == [
        {
            "version": 1,
            "target_id": "example",
            "name": "Example",
            "url": "https://example.com/",
            "enabled": True,
            "action": "monitor",
            "watch_focus": "",
            "notification_group": "",
            "fetch_mode": "static",
        }
    ]


@pytest.mark.parametrize(
    ("enabled", "action"),
    [(True, "monitor"), (False, "skip_disabled")],
    ids=["enabled", "disabled"],
)
def test_validate_targets_assigns_target_action(enabled: bool, action: str) -> None:
    result = validate_targets({
        "persistence_mode": "local",
        "targets": [_target(enabled=enabled)],
    })

    targets = result["targets"]
    assert isinstance(targets, list)
    assert isinstance(targets[0], dict)
    assert targets[0]["action"] == action


def test_validate_targets_accepts_benign_query() -> None:
    result = validate_targets({
        "persistence_mode": "local",
        "targets": [_target(url="https://example.com/?page=2")],
    })

    targets = result["targets"]
    assert isinstance(targets, list)
    assert isinstance(targets[0], dict)
    assert targets[0]["url"] == "https://example.com/?page=2"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target_id": "bad/id"}, "invalid_target_id"),
        ({"url": "file:///tmp/a"}, "absolute HTTP"),
        ({"url": "https://user:pass@example.com/"}, "credentials"),
        ({"url": "https://example.com/?token=secret"}, "credentials"),
        (
            {
                "url": "https://example.com/?next=https%3A%2F%2Fexample.com%2F%3Ftoken%3Dsecret"
            },
            "credentials",
        ),
        (
            {
                "url": "/".join([
                    "https://hooks.slack.com",
                    "services",
                    "T00000000",
                    "B00000000",
                    "X" * 24,
                ])
            },
            "credentials",
        ),
        ({"url": "https://example.com/#x"}, "fragment"),
        ({"enabled": "yes"}, "enabled"),
        ({"fetch_mode": "auto"}, "fetch_mode"),
        ({"include_selector": "main"}, "selector_migration_required"),
        ({"exclude_selectors": ["nav"]}, "selector_migration_required"),
    ],
)
def test_validate_targets_rejects_invalid_target(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(WorkflowError, match=message):
        validate_targets({
            "persistence_mode": "google-drive",
            "targets": [_target(**override)],
        })


def test_validate_targets_rejects_duplicates_and_mode() -> None:
    with pytest.raises(WorkflowError, match="duplicate_target_id"):
        validate_targets({
            "persistence_mode": "local",
            "targets": [_target(), _target()],
        })
    with pytest.raises(WorkflowError, match="persistence_mode"):
        validate_targets({"persistence_mode": "mixed", "targets": []})


@pytest.mark.parametrize(
    ("payload", "action"),
    [
        ({"status": "baseline"}, "promote_snapshot"),
        ({"status": "unchanged"}, "discard_candidate"),
        (
            {"status": "changed", "diff_truncated": False, "materiality": None},
            "assess_materiality",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": False},
            "promote_snapshot",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": True},
            "notify",
        ),
        (
            {"status": "changed", "diff_truncated": True, "materiality": False},
            "manual_review",
        ),
    ],
)
def test_change_action(payload: dict[str, object], action: str) -> None:
    assert change_action(payload) == {"action": action}


def test_notification_protocol_success_path() -> None:
    first = notification_step(_event_payload())
    assert first["protocol_version"] == 2
    assert first["action"] == "compare_and_swap"
    assert first["expected_notification"] is None
    assert first["next_signal"] == "pending_persisted"
    pending = _record(first)
    assert pending["status"] == "pending"
    assert pending["attempt"] == 0
    assert re.fullmatch(r"[0-9a-f]{64}", str(pending["event_id"]))

    sending_step = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    assert sending_step["protocol_version"] == 2
    assert sending_step["action"] == "compare_and_swap"
    assert sending_step["expected_notification"] == pending
    sending = _record(sending_step)
    assert sending_step["next_signal"] == "sending_claimed"
    assert sending["status"] == "sending"
    assert sending["attempt"] == 1

    send = notification_step(
        _event_payload(notification=sending, signal="sending_claimed")
    )
    assert send["protocol_version"] == 2
    assert send["action"] == "send_slack"

    delivered_step = notification_step(
        _event_payload(notification=sending, signal="slack_delivered")
    )
    assert delivered_step["action"] == "compare_and_swap"
    assert delivered_step["expected_notification"] == sending
    delivered = _record(delivered_step)
    assert delivered_step["next_signal"] == "delivered_persisted"
    assert delivered["status"] == "delivered"

    done = notification_step(
        _event_payload(notification=delivered, signal="delivered_persisted")
    )
    assert done == {"protocol_version": 2, "action": "promote_snapshot"}


def test_notification_protocol_restart_and_failure_paths() -> None:
    pending = _record(notification_step(_event_payload()))
    resumed = notification_step(_event_payload(notification=pending))
    assert resumed["action"] == "compare_and_swap"
    assert resumed["expected_notification"] == pending
    sending = _record(resumed)
    assert resumed["next_signal"] == "sending_claimed"
    assert sending["status"] == "sending"

    ambiguous = notification_step(_event_payload(notification=sending))
    assert ambiguous == {
        "protocol_version": 2,
        "action": "manual_reconciliation",
    }

    failed = notification_step(
        _event_payload(
            notification=sending,
            signal="slack_failed",
            error="timeout",
        )
    )
    assert failed["action"] == "compare_and_swap"
    assert failed["expected_notification"] == sending
    retry = _record(failed)
    assert failed["next_signal"] == "failure_persisted"
    assert retry["status"] == "pending"
    assert retry["attempt"] == 1
    assert retry["last_error"] == "timeout"
    stopped = notification_step(
        _event_payload(notification=retry, signal="failure_persisted")
    )
    assert stopped == {"protocol_version": 2, "action": "stop"}

    delivered = dict(sending)
    delivered["status"] = "delivered"
    assert notification_step(_event_payload(notification=delivered)) == {
        "protocol_version": 2,
        "action": "promote_snapshot",
    }


def test_local_notification_cas_persists_and_reads_back(tmp_path: Path) -> None:
    first = notification_step(_event_payload())
    applied = _run_local_cas(first, tmp_path)

    assert applied["protocol_version"] == 2
    assert applied["action"] == "compare_and_swap_applied"
    assert applied["next_signal"] == first["next_signal"]
    pending = _record(applied)
    event_id = str(pending["event_id"])
    notifications_dir = tmp_path / "notifications"
    record_path = notifications_dir / f"{event_id}.json"

    assert json.loads(record_path.read_text()) == pending
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(notifications_dir.stat().st_mode) & 0o077 == 0
    assert LocalNotificationStore(tmp_path, "example").read(event_id) == pending

    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    claimed = _run_local_cas(claim, tmp_path)
    assert claimed["action"] == "compare_and_swap_applied"
    assert _record(claimed)["status"] == "sending"
    assert json.loads(record_path.read_text()) == _record(claimed)


def test_local_notification_cas_conflict_returns_current_record(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    first_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_replacement = _record(second_claim)
    second_replacement["updated_at"] = "2000-01-01T00:00:00+00:00"
    second_claim["notification"] = second_replacement

    first_result = _run_local_cas(first_claim, tmp_path)
    assert first_result["action"] == "compare_and_swap_applied"
    winner = _record(first_result)

    conflict = _run_local_cas(second_claim, tmp_path)
    assert conflict == {
        "protocol_version": 2,
        "action": "compare_and_swap_conflict",
        "notification": winner,
    }
    event_id = str(winner["event_id"])
    assert (
        json.loads((tmp_path / "notifications" / f"{event_id}.json").read_text())
        == winner
    )


def test_local_notification_cas_rejects_present_record_for_absent_expected(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    claim["expected_notification"] = None

    assert _run_local_cas(claim, tmp_path) == {
        "protocol_version": 2,
        "action": "compare_and_swap_conflict",
        "notification": pending,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol_version", 1, "protocol_version"),
        ("action", "overwrite", "action"),
        ("next_signal", "sending_persisted", "next_signal"),
    ],
    ids=["wrong-version", "wrong-action", "wrong-signal"],
)
def test_local_notification_cas_rejects_invalid_directive(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    directive = notification_step(_event_payload())
    directive[field] = value

    with pytest.raises(WorkflowError, match=message):
        _run_local_cas(directive, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", True, "version"),
        ("attempt", False, "attempt"),
        ("extra", "value", "exactly"),
    ],
    ids=["boolean-version", "boolean-attempt", "extra-field"],
)
def test_local_notification_cas_rejects_invalid_record(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    directive = notification_step(_event_payload())
    replacement = _record(directive)
    replacement[field] = value
    directive["notification"] = replacement

    with pytest.raises(WorkflowError, match=message):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_unsafe_local_state(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").symlink_to(tmp_path / "outside.json")

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{", "valid JSON"),
        (b"x" * (1024 * 1024 + 1), "too large"),
    ],
    ids=["malformed-json", "oversized-record"],
)
def test_local_notification_cas_rejects_malformed_or_oversized_state(
    tmp_path: Path, content: bytes, message: str
) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").write_bytes(content)

    with pytest.raises(LocalStoreError, match=message):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_invalid_existing_schema(
    tmp_path: Path,
) -> None:
    directive = notification_step(_event_payload())
    record = dict(_record(directive))
    record["target_id"] = "different-target"
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").write_text(json.dumps(record))

    with pytest.raises(LocalStoreError, match="schema"):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_lock_symlink(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("not a lock")
    (notifications_dir / ".example.lock").symlink_to(lock_target)

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_nonregular_record(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").mkdir()

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_fsyncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    original_fsync = os.fsync
    synced: list[int] = []

    def tracking_fsync(file_descriptor: int) -> None:
        synced.append(file_descriptor)
        original_fsync(file_descriptor)

    monkeypatch.setattr(workflow.os, "fsync", tracking_fsync)
    assert _run_local_cas(claim, tmp_path)["action"] == "compare_and_swap_applied"
    assert len(synced) == 2


def test_local_notification_cas_preserves_old_record_on_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )

    def fail_fsync(_: int) -> None:
        raise OSError

    monkeypatch.setattr(workflow.os, "fsync", fail_fsync)
    with pytest.raises(LocalStoreError, match="temporary notification"):
        _run_local_cas(claim, tmp_path)

    event_id = str(pending["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    assert json.loads(record_path.read_text()) == pending
    assert list(record_path.parent.glob(".*.tmp")) == []


def test_local_notification_cas_reports_directory_fsync_failure_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )

    def fail_directory_fsync(_: Path) -> None:
        raise OSError

    monkeypatch.setattr(
        LocalNotificationStore,
        "_fsync_directory",
        staticmethod(fail_directory_fsync),
    )
    with pytest.raises(LocalStoreError, match="durably write"):
        _run_local_cas(claim, tmp_path)

    sending = _record(claim)
    event_id = str(pending["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    assert json.loads(record_path.read_text()) == sending
    assert list(record_path.parent.glob(".*.tmp")) == []
    assert notification_step(_event_payload(notification=sending)) == {
        "protocol_version": 2,
        "action": "manual_reconciliation",
    }


def test_local_notification_cas_serializes_separate_processes(tmp_path: Path) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    first_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_replacement = _record(second_claim)
    second_replacement["updated_at"] = "2000-01-01T00:00:00+00:00"
    second_claim["notification"] = second_replacement

    command = [
        sys.executable,
        str(Path(workflow.__file__).resolve()),
        "local-notification-cas",
        "--runtime-dir",
        str(tmp_path),
    ]
    processes = [
        subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [
        process.communicate(json.dumps(_local_cas_payload(directive)), timeout=10)
        for process, directive in zip(
            processes, (first_claim, second_claim), strict=True
        )
    ]
    assert [process.returncode for process in processes] == [0, 0]
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert {result["action"] for result in results} == {
        "compare_and_swap_applied",
        "compare_and_swap_conflict",
    }
    winner = next(
        result for result in results if result["action"] == "compare_and_swap_applied"
    )
    event_id = str(pending["event_id"])
    assert (
        json.loads((tmp_path / "notifications" / f"{event_id}.json").read_text())
        == winner["notification"]
    )


def test_notification_protocol_rejects_wrong_state_or_event() -> None:
    pending = _record(notification_step(_event_payload()))
    with pytest.raises(WorkflowError, match="sending notification"):
        notification_step(
            _event_payload(notification=pending, signal="slack_delivered")
        )

    sending = _record(
        notification_step(
            _event_payload(notification=pending, signal="pending_persisted")
        )
    )
    with pytest.raises(WorkflowError, match="signal is invalid"):
        notification_step(
            _event_payload(notification=sending, signal="sending_persisted")
        )

    bad = dict(pending)
    bad["sha256"] = "b" * 64
    with pytest.raises(WorkflowError, match="sha256"):
        notification_step(_event_payload(notification=bad))

    with pytest.raises(WorkflowError, match="lowercase hexadecimal"):
        notification_step(_event_payload(sha256="A" * 64))


def test_run_rejects_unknown_operation() -> None:
    with pytest.raises(WorkflowError, match="operation"):
        workflow.run("unknown", {})
