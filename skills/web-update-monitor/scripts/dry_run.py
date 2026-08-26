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
from routine import RoutineConfig, WebUpdateMonitorRoutine

_EXPECTED_ARGC = 2


def run_fixture(value: dict[str, Any]) -> dict[str, Any]:
    """Replay one or more monitor cycles against an in-memory fixture.

    Returns:
        A summary containing each cycle's run result, the Slack messages that
        would have been sent, the resulting state target IDs, and the audit
        record count.
    """
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
        routine = WebUpdateMonitorRoutine(
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
    """Run the CLI entry point: load the fixture named in ``argv[1]`` and run it.

    On success, writes the JSON-encoded :func:`run_fixture` result to
    stdout. On a handled failure, writes ``{"error": ...}`` to stdout
    instead. Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if the fixture is invalid or the run fails, 2 for
        incorrect CLI usage.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write("usage: dry_run.py FIXTURE_JSON\n")
        return 2
    try:
        value = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        result = run_fixture(value)
    except (OSError, ValueError, KeyError, TypeError, MonitorError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "fixture_invalid",
                "message": type(exc).__name__,
                "retryable": False,
            }
        )
        json.dump({"error": error}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
