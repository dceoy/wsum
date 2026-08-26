"""Regression tests for failure-alert identity and retry behavior."""

from __future__ import annotations

from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import (
    EvidenceSummaryClient,
    FixtureFetcher,
    MemoryDriveConnector,
    MemoryOperationalStore,
)
from models import Target
from notifications import ConfirmedDeliveryFailure, failure_event_id
from retry import RetryConfig
from routine import RoutineConfig, WebUpdateMonitorRoutine


class ConfirmedThenSuccessfulSlack:
    """Fail one confirmed send, then accept the retry."""

    def __init__(self) -> None:
        """Initialize the send-call counter."""
        self.calls = 0

    def send_message(self, notification_group: str, message: str) -> str:
        """Fail the first call and return a delivery reference thereafter.

        Returns:
            A deterministic delivery reference after the first call.

        Raises:
            ConfirmedDeliveryFailure: On the first send attempt.
        """
        del notification_group, message
        self.calls += 1
        if self.calls == 1:
            msg = "notification_send_failed"
            raise ConfirmedDeliveryFailure(msg, "fixture confirmed non-delivery")
        return f"slack:{self.calls}"


def _target() -> Target:
    """Build one deterministic monitor target.

    Returns:
        A validated target fixture.
    """
    return Target.from_mapping({
        "target_id": "one",
        "enabled": True,
        "name": "One",
        "url": "https://example.com/one",
        "fetch_mode": "static",
        "include_selector": "main",
        "exclude_selectors": "",
        "watch_focus": "price",
        "notification_group": "default",
    })


def test_failure_event_id_encodes_variable_fields_unambiguously() -> None:
    """Different target/episode tuples must never share concatenated material."""
    assert failure_event_id("a", "bc", 3) != failure_event_id("ab", "c", 3)


def test_confirmed_failure_alert_is_retried_above_threshold() -> None:
    """A confirmed non-delivery at the threshold must retry in the same incident."""
    target = _target()
    store = MemoryOperationalStore([target])
    slack = ConfirmedThenSuccessfulSlack()
    transient = MonitorError("fetch_timeout", "fixture timeout", retryable=True)

    for index in range(1, 5):
        routine = WebUpdateMonitorRoutine(
            store=store,
            snapshots=SnapshotStore(MemoryDriveConnector()),
            summary_client=EvidenceSummaryClient(),
            slack=slack,
            fetcher=FixtureFetcher({"one": transient}),
            config=RoutineConfig(
                failure_alert_threshold=3,
                retry=RetryConfig(max_attempts=1),
            ),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id=f"run-{index}")
        assert result.metrics.failed == 1

    event_id = failure_event_id("one", "initial", 3)
    assert store.states["one"].consecutive_failures == 4
    assert store.notifications[event_id].status == "sent"
    assert slack.calls == 2
