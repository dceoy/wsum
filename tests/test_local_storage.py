"""Tests for the local filesystem monitoring adapters."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest
from errors import MonitorError
from local_storage import LocalOperationalStore, LocalSnapshotStore
from models import Attempt, NotificationRecord, RunRecord, State
from normalize import NormalizedContent

_TIMESTAMP = "2026-08-26T00:00:00Z"


def _target_records() -> list[dict[str, object]]:
    """Build one enabled and one disabled local target fixture.

    Returns:
        The two target mappings in deterministic order.
    """
    return [
        {
            "target_id": "enabled",
            "enabled": True,
            "name": "Enabled target",
            "url": "https://example.com/enabled",
            "fetch_mode": "static",
            "include_selector": "",
            "exclude_selectors": [],
            "watch_focus": "",
            "notification_group": "default",
        },
        {
            "target_id": "disabled",
            "enabled": False,
            "name": "Disabled target",
            "url": "https://example.com/disabled",
            "fetch_mode": "static",
            "include_selector": "",
            "exclude_selectors": [],
            "watch_focus": "",
            "notification_group": "default",
        },
    ]


def _write_targets(root: Path) -> None:
    """Write the local target fixtures to ``targets.json``."""
    (root / "targets.json").write_text(json.dumps(_target_records()), encoding="utf-8")


def _run_record(*, summary: str = "") -> RunRecord:
    """Build one validated local run fixture.

    Returns:
        The run record.
    """
    return RunRecord(
        run_id="run-1",
        target_id="enabled",
        result="unchanged",
        change_score=0,
        summary=summary,
        error_code="",
        started_at=_TIMESTAMP,
        finished_at=_TIMESTAMP,
        attempts=(Attempt(number=1, result="success"),),
    )


def _notification(event_id: str) -> NotificationRecord:
    """Build one pending notification fixture.

    Returns:
        The notification record.
    """
    return NotificationRecord(
        event_id=event_id,
        target_id="enabled",
        status="pending",
    )


def _normalized(text: str = "hello\n", digest: str = "a" * 64) -> NormalizedContent:
    """Build one normalized text fixture with a caller-controlled hash.

    Returns:
        The normalized content record.
    """
    return NormalizedContent(
        kind="text",
        text=text,
        normalized_hash=digest,
        normalization_version="2026-01",
        hash_algorithm="sha256",
        metadata={"content_type": "text/plain"},
    )


class LocalOperationalStoreTest(unittest.TestCase):
    """Tests for JSON-backed operational persistence."""

    def setUp(self) -> None:
        """Create an isolated runtime root for each test."""
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        """Remove the isolated runtime root."""
        self._temp.cleanup()

    def test_round_trip_and_idempotency(self) -> None:
        """Persist targets, state, runs, and notification records across instances."""
        _write_targets(self.root)
        store = LocalOperationalStore(self.root)
        assert [item.target_id for item in store.load_enabled_targets()] == ["enabled"]

        digest = "a" * 64
        state = State(
            target_id="enabled",
            last_checked_at=_TIMESTAMP,
            normalized_hash=digest,
            snapshot_ref=f"snapshots/enabled/{digest}/normalized.txt",
        )
        store.replace_state(state)
        assert store.get_state("enabled") == state
        assert store.get_state("missing") is None

        run = _run_record()
        store.append_run(run)
        store.append_run(_run_record(summary="must not replace the first record"))
        assert store.get_run("run-1") == run
        assert store.get_run("missing") is None

        first = _notification("b" * 64)
        second = _notification("c" * 64)
        store.upsert_notification(first)
        store.upsert_notifications_atomically((first, second))
        assert store.get_notification(first.event_id) == first
        assert store.get_notification(second.event_id) == second
        assert store.get_notification("d" * 64) is None

        reopened = LocalOperationalStore(self.root)
        assert reopened.get_state("enabled") == state
        assert reopened.get_run("run-1") == run
        assert reopened.get_notification(second.event_id) == second

    def test_bad_configuration_fails_closed(self) -> None:
        """Reject missing, malformed, and duplicate target configuration."""
        store = LocalOperationalStore(self.root)
        with pytest.raises(MonitorError) as missing:
            store.load_enabled_targets()
        assert missing.value.code == "local_configuration_missing"

        (self.root / "targets.json").write_text("{}", encoding="utf-8")
        with pytest.raises(MonitorError) as malformed:
            store.load_enabled_targets()
        assert malformed.value.code == "local_storage_invalid"

        targets = _target_records()
        targets.append(targets[0].copy())
        (self.root / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
        with pytest.raises(MonitorError) as duplicate:
            store.load_enabled_targets()
        assert duplicate.value.code == "local_storage_invalid"

    def test_duplicate_notification_batch_is_rejected(self) -> None:
        """Reject duplicate event IDs before modifying local notification state."""
        store = LocalOperationalStore(self.root)
        notification = _notification("b" * 64)
        with pytest.raises(MonitorError) as duplicate:
            store.upsert_notifications_atomically((notification, notification))
        assert duplicate.value.code == "notification_invalid"


class LocalSnapshotStoreTest(unittest.TestCase):
    """Tests for content-addressed filesystem snapshots."""

    def setUp(self) -> None:
        """Create an isolated runtime root for each test."""
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        """Remove the isolated runtime root."""
        self._temp.cleanup()

    def test_round_trip_idempotency_and_collision_detection(self) -> None:
        """Round-trip snapshots, preserve duplicates, and reject hash collisions."""
        store = LocalSnapshotStore(self.root)
        content = _normalized()
        reference = store.save("enabled", content)
        expected = f"snapshots/enabled/{content.normalized_hash}/normalized.txt"
        assert reference == expected
        assert store.load_normalized(reference) == content.text
        assert (self.root / reference).is_file()
        assert (
            self.root
            / "snapshots"
            / "enabled"
            / content.normalized_hash
            / "metadata.json"
        ).is_file()
        assert store.save("enabled", content) == reference

        with pytest.raises(MonitorError) as collision:
            store.save("enabled", _normalized(text="different\n"))
        assert collision.value.code == "snapshot_collision"

    def test_invalid_references_are_rejected(self) -> None:
        """Reject traversal, malformed hashes, and missing local snapshots."""
        store = LocalSnapshotStore(self.root)
        with pytest.raises(MonitorError) as traversal:
            store.load_normalized("../outside")
        assert traversal.value.code == "snapshot_invalid"

        malformed = "snapshots/enabled/not-a-hash/normalized.txt"
        with pytest.raises(MonitorError) as bad_hash:
            store.load_normalized(malformed)
        assert bad_hash.value.code == "snapshot_invalid"

        missing = f"snapshots/enabled/{'c' * 64}/normalized.txt"
        with pytest.raises(MonitorError) as absent:
            store.load_normalized(missing)
        assert absent.value.code == "snapshot_missing"

    def test_size_limit_is_enforced(self) -> None:
        """Reject normalized local snapshots above the configured byte limit."""
        store = LocalSnapshotStore(self.root, max_snapshot_bytes=1_024)
        with pytest.raises(MonitorError) as oversized:
            store.save("enabled", _normalized(text="x" * 1_025))
        assert oversized.value.code == "snapshot_too_large"
