"""Tests for deterministic workflow decisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

import pytest
import workflow
from workflow import WorkflowError, change_action, notification_step, validate_targets


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


def _record(result: Mapping[str, object]) -> dict[str, object]:
    value = result.get("notification")
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


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
            "watch_focus": "",
            "notification_group": "",
            "fetch_mode": "static",
        }
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target_id": "bad/id"}, "invalid_target_id"),
        ({"url": "file:///tmp/a"}, "absolute HTTP"),
        ({"url": "https://user:pass@example.com/"}, "credentials"),
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
    assert first["action"] == "persist"
    pending = _record(first)
    assert pending["status"] == "pending"
    assert pending["attempt"] == 0
    assert re.fullmatch(r"[0-9a-f]{64}", str(pending["event_id"]))

    sending_step = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    sending = _record(sending_step)
    assert sending_step["next_signal"] == "sending_persisted"
    assert sending["status"] == "sending"
    assert sending["attempt"] == 1

    send = notification_step(
        _event_payload(notification=sending, signal="sending_persisted")
    )
    assert send["action"] == "send_slack"

    delivered_step = notification_step(
        _event_payload(notification=sending, signal="slack_delivered")
    )
    delivered = _record(delivered_step)
    assert delivered["status"] == "delivered"

    done = notification_step(
        _event_payload(notification=delivered, signal="delivered_persisted")
    )
    assert done == {"action": "promote_snapshot"}


def test_notification_protocol_restart_and_failure_paths() -> None:
    pending = _record(notification_step(_event_payload()))
    resumed = notification_step(_event_payload(notification=pending))
    sending = _record(resumed)
    assert sending["status"] == "sending"

    ambiguous = notification_step(_event_payload(notification=sending))
    assert ambiguous == {"action": "manual_reconciliation"}

    failed = notification_step(
        _event_payload(
            notification=sending,
            signal="slack_failed",
            error="timeout",
        )
    )
    retry = _record(failed)
    assert failed["action"] == "persist_and_stop"
    assert retry["status"] == "pending"
    assert retry["attempt"] == 1
    assert retry["last_error"] == "timeout"

    delivered = dict(sending)
    delivered["status"] = "delivered"
    assert notification_step(_event_payload(notification=delivered)) == {
        "action": "promote_snapshot"
    }


def test_notification_protocol_rejects_wrong_state_or_event() -> None:
    pending = _record(notification_step(_event_payload()))
    with pytest.raises(WorkflowError, match="sending notification"):
        notification_step(
            _event_payload(notification=pending, signal="slack_delivered")
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
