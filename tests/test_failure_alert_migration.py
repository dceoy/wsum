"""Regression tests for migration from weekly failure-alert IDs."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import (
    EvidenceSummaryClient,
    FixtureFetcher,
    MemoryDriveConnector,
    MemoryOperationalStore,
    MemorySlackConnector,
)
from models import NotificationRecord, State, Target
from notifications import failure_event_id
from outbox import enqueue_record
from retry import RetryConfig
from routine import RoutineConfig, WebUpdateMonitorRoutine

_THRESHOLD = 3
_LEGACY_CHECKED_AT = "2026-08-25T12:00:00Z"


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


def _legacy_event_id() -> str:
    """Reproduce the weekly failure ID emitted by the pre-migration main branch.

    Returns:
        The legacy SHA-256 event ID.
    """
    checked_at = datetime.fromisoformat(_LEGACY_CHECKED_AT)
    calendar = checked_at.isocalendar()
    year_week = f"{calendar.year}-W{calendar.week:02d}"
    material = f"failureone{year_week}{_THRESHOLD}".encode()
    return hashlib.sha256(material).hexdigest()


def _seed_legacy_state(store: MemoryOperationalStore) -> None:
    """Seed state representing a threshold-crossed incident from old main."""
    store.states["one"] = State(
        target_id="one",
        last_checked_at=_LEGACY_CHECKED_AT,
        consecutive_failures=_THRESHOLD,
    )


def _transient() -> MonitorError:
    """Return the failure used to extend the seeded incident."""
    return MonitorError("fetch_timeout", "fixture timeout", retryable=True)


def test_direct_delivery_migrates_legacy_sent_alert_without_resending() -> None:
    """A sent weekly alert must suppress the first stable-ID alert after upgrade."""
    target = _target()
    store = MemoryOperationalStore([target])
    _seed_legacy_state(store)
    legacy_id = _legacy_event_id()
    store.upsert_notification(
        NotificationRecord(
            legacy_id,
            "one",
            "sent",
            notified_at=_LEGACY_CHECKED_AT,
            kind="failure",
        )
    )
    slack = MemorySlackConnector()
    routine = WebUpdateMonitorRoutine(
        store=store,
        snapshots=SnapshotStore(MemoryDriveConnector()),
        summary_client=EvidenceSummaryClient(),
        slack=slack,
        fetcher=FixtureFetcher({"one": _transient()}),
        config=RoutineConfig(
            failure_alert_threshold=_THRESHOLD,
            retry=RetryConfig(max_attempts=1),
        ),
        sleeper=lambda _: None,
    )

    result = routine.run(run_id="run-after-upgrade")

    stable_id = failure_event_id("one", _LEGACY_CHECKED_AT, _THRESHOLD)
    assert result.metrics.failed == 1
    assert store.states["one"].consecutive_failures == _THRESHOLD + 1
    assert slack.messages == []
    assert store.notifications[stable_id].status == "sent"


def test_outbox_delivery_recognizes_legacy_pending_alert_without_requeueing() -> None:
    """A queued weekly alert must not be duplicated under the stable ID."""
    target = _target()
    store = MemoryOperationalStore([target])
    _seed_legacy_state(store)
    legacy_id = _legacy_event_id()
    store.upsert_outbox(
        enqueue_record(
            legacy_id,
            "one",
            "default",
            "legacy failure alert",
            now=_LEGACY_CHECKED_AT,
        )
    )
    routine = WebUpdateMonitorRoutine(
        store=store,
        snapshots=SnapshotStore(MemoryDriveConnector()),
        summary_client=EvidenceSummaryClient(),
        outbox_store=store,
        fetcher=FixtureFetcher({"one": _transient()}),
        config=RoutineConfig(
            failure_alert_threshold=_THRESHOLD,
            retry=RetryConfig(max_attempts=1),
            delivery_mode="outbox",
        ),
        sleeper=lambda _: None,
    )

    result = routine.run(run_id="run-after-upgrade")

    stable_id = failure_event_id("one", _LEGACY_CHECKED_AT, _THRESHOLD)
    assert result.metrics.failed == 1
    assert store.states["one"].consecutive_failures == _THRESHOLD + 1
    assert legacy_id in store.outbox
    assert stable_id not in store.outbox


def test_recovered_target_does_not_reuse_stale_weekly_alert_for_new_episode() -> None:
    """A recovered target must stop consulting weekly IDs before new failures."""
    target = _target()
    store = MemoryOperationalStore([target])
    store.states["one"] = State(
        target_id="one",
        last_checked_at=_LEGACY_CHECKED_AT,
        consecutive_failures=0,
    )
    legacy_id = _legacy_event_id()
    store.upsert_notification(
        NotificationRecord(
            legacy_id,
            "one",
            "sent",
            notified_at=_LEGACY_CHECKED_AT,
            kind="failure",
        )
    )
    slack = MemorySlackConnector()
    routine = WebUpdateMonitorRoutine(
        store=store,
        snapshots=SnapshotStore(MemoryDriveConnector()),
        summary_client=EvidenceSummaryClient(),
        slack=slack,
        fetcher=FixtureFetcher({"one": _transient()}),
        config=RoutineConfig(
            failure_alert_threshold=_THRESHOLD,
            retry=RetryConfig(max_attempts=1),
        ),
        sleeper=lambda _: None,
    )

    first = routine.run(run_id="new-episode-1")
    assert first.metrics.failed == 1
    assert store.states["one"].consecutive_failures == 1
    assert slack.messages == []

    store.replace_state(replace(store.states["one"], consecutive_failures=_THRESHOLD))
    second = routine.run(run_id="new-episode-after-threshold-crash")

    stable_id = failure_event_id("one", _LEGACY_CHECKED_AT, _THRESHOLD)
    assert second.metrics.failed == 1
    assert store.states["one"].consecutive_failures == _THRESHOLD + 1
    assert len(slack.messages) == 1
    assert store.notifications[stable_id].status == "sent"
