"""Tests for deterministic workflow decisions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest
import workflow
from workflow import WorkflowError, change_action, notification_step, validate_targets

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


class _AtomicNotificationStore:
    """Minimal exact compare-and-swap store for protocol tests."""

    def __init__(self, record: dict[str, object]) -> None:
        self.record = record

    def compare_and_swap(
        self,
        expected: Mapping[str, object],
        replacement: Mapping[str, object],
    ) -> bool:
        if self.record != dict(expected):
            return False
        self.record = dict(replacement)
        return True


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


def test_notification_claims_are_exact_compare_and_swap() -> None:
    pending = _record(notification_step(_event_payload()))
    first_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    store = _AtomicNotificationStore(pending)

    first_expected = _dict_field(first_claim, "expected_notification")
    first_replacement = _record(first_claim)
    second_expected = _dict_field(second_claim, "expected_notification")
    second_replacement = _record(second_claim)
    assert store.compare_and_swap(first_expected, first_replacement)
    assert not store.compare_and_swap(second_expected, second_replacement)

    winner = notification_step(
        _event_payload(notification=store.record, signal="sending_claimed")
    )
    assert winner["action"] == "send_slack"


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
