"""Regression tests for SQLite-backed local operational persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

from local_storage import LocalOperationalStore
from models import NotificationRecord, RunRecord

_TIMESTAMP = "2026-08-26T00:00:00Z"


def _run(index: int) -> RunRecord:
    """Build one terminal run record."""
    return RunRecord(
        run_id=f"run-{index}",
        target_id="target",
        result="unchanged",
        change_score=0,
        summary="",
        error_code="",
        started_at=_TIMESTAMP,
        finished_at=_TIMESTAMP,
    )


def _notification(index: int) -> NotificationRecord:
    """Build one notification record."""
    return NotificationRecord(
        event_id=f"{index:064x}",
        target_id="target",
        status="sent",
        notified_at=_TIMESTAMP,
    )


def test_multiple_store_instances_preserve_independent_history_writes() -> None:
    """Separate store instances must not overwrite each other's history."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = LocalOperationalStore(root)
        second = LocalOperationalStore(root)

        for index in range(64):
            store = first if index % 2 == 0 else second
            store.append_run(_run(index))
            store.upsert_notification(_notification(index))

        reopened = LocalOperationalStore(root)
        for index in range(64):
            assert reopened.get_run(f"run-{index}") == _run(index)
            event_id = f"{index:064x}"
            assert reopened.get_notification(event_id) == _notification(index)

        assert (root / "operations.sqlite3").is_file()
        assert not (root / "runs.json").exists()
        assert not (root / "notifications.json").exists()
