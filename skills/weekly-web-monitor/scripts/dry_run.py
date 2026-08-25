"""Run the complete Routine against a local JSON fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import (
    EvidenceSummaryClient,
    FixtureFetcher,
    FixtureResponse,
    MemoryDriveConnector,
    MemoryOperationalStore,
    MemorySlackConnector,
)
from models import Target
from routine import RoutineConfig, WeeklyMonitorRoutine


def run_fixture(value: dict[str, Any]) -> dict[str, Any]:
    targets = [Target.from_mapping(item) for item in value["targets"]]
    store = MemoryOperationalStore(targets)
    drive_connector = MemoryDriveConnector()
    snapshots = SnapshotStore(drive_connector)
    slack = MemorySlackConnector(value.get("fail_slack_groups", []))
    cycles = value.get("cycles", [])
    results: list[dict[str, Any]] = []
    for index, cycle in enumerate(cycles, start=1):
        responses = {
            target_id: FixtureResponse(
                body=str(item.get("body", "")).encode("utf-8"),
                content_type=str(item.get("content_type", "text/html")),
                status=int(item.get("status", 200)),
                etag=str(item.get("etag", "")),
                last_modified=str(item.get("last_modified", "")),
            )
            for target_id, item in cycle.items()
        }
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=slack,
            fetcher=FixtureFetcher(responses),
            audit_sink=store,
            config=RoutineConfig(max_concurrency=min(4, max(1, len(targets)))),
            sleeper=lambda _: None,
        )
        results.append(routine.run(run_id=f"dry-run-{index}").as_dict())
    return {
        "cycles": results,
        "slack_messages": [
            {"notification_group": group, "delivery_ref": reference}
            for group, _, reference in slack.messages
        ],
        "state_target_ids": sorted(store.states),
        "audit_record_count": len(store.audit),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        value = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        run_fixture(value)
    except (OSError, ValueError, KeyError, TypeError, MonitorError) as exc:
        (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "fixture_invalid",
                "message": type(exc).__name__,
                "retryable": False,
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
