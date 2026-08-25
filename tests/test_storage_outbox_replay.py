"""Tests for the storage_outbox_replay module."""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- deliberately used
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from audit import configuration_digest, make_audit_record
from diff import compare_content
from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import MemoryDriveConnector
from normalize import normalize_content
from outbox import (
    OUTBOX_COLUMNS,
    OutboxDeliveryError,
    OutboxRecord,
    OutboxSheetsStore,
    dispatch_record,
    enqueue_record,
    load_outbox,
)
from replay import main as replay_main
from replay import replay_manifest

from tests import support

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class DriveTests(unittest.TestCase):
    """Tests for DriveTests."""

    def test_snapshot_upload_lookup_and_duplicate_are_idempotent(self) -> None:
        """Test that snapshot upload lookup and duplicate are idempotent."""
        connector = MemoryDriveConnector()
        store = SnapshotStore(connector)
        content = normalize_content(
            b"<main><p>Hello</p></main>", content_type="text/html"
        )
        reference = store.save("one", content)
        second = store.save("one", content)
        assert reference == second
        assert len(connector.files) == 2
        assert content.text == store.load_normalized(reference)

    def test_failed_write_and_missing_snapshot_fail_closed(self) -> None:
        """Test that failed write and missing snapshot fail closed."""
        connector = MemoryDriveConnector()
        connector.fail_upload = True
        store = SnapshotStore(connector)
        content = normalize_content(b"<p>Hello</p>", content_type="text/html")
        with pytest.raises(MonitorError, match="fixture upload"):
            store.save("one", content)
        assert connector.files == {}
        with pytest.raises(MonitorError, match="missing"):
            store.load_normalized("drive:missing")
        bounded_connector = MemoryDriveConnector()
        bounded_connector.files["drive:large"] = b"x" * 2_000
        bounded = SnapshotStore(bounded_connector, max_snapshot_bytes=1_024)
        with pytest.raises(MonitorError, match="size limit"):
            bounded.load_normalized("drive:large")

    def test_diff_artifact_is_versioned_by_previous_hash_on_a_b_c_b_cycle(
        self,
    ) -> None:
        """Test that diff artifact is versioned by previous hash on a b c b cycle."""
        connector = MemoryDriveConnector()
        store = SnapshotStore(connector)
        content_a = normalize_content(b"<p>A</p>", content_type="text/html")
        content_b = normalize_content(b"<p>B</p>", content_type="text/html")
        content_c = normalize_content(b"<p>C</p>", content_type="text/html")
        diff_a_to_b = compare_content(
            content_a.text,
            content_b.text,
            previous_hash=content_a.normalized_hash,
            current_hash=content_b.normalized_hash,
        )
        diff_c_to_b = compare_content(
            content_c.text,
            content_b.text,
            previous_hash=content_c.normalized_hash,
            current_hash=content_b.normalized_hash,
        )
        ref_first_b = store.save(
            "one", content_b, diff_a_to_b, previous_hash=content_a.normalized_hash
        )
        store.save("one", content_c, previous_hash=content_b.normalized_hash)
        ref_second_b = store.save(
            "one", content_b, diff_c_to_b, previous_hash=content_c.normalized_hash
        )
        assert ref_first_b == ref_second_b
        b_prefix = f"snapshots/one/{content_b.normalized_hash}/"
        diff_paths = [
            path
            for path in connector.paths
            if path.startswith(b_prefix) and "diff" in path
        ]
        assert len(diff_paths) == 2
        contents = {connector.files[connector.paths[path]] for path in diff_paths}
        assert len(contents) == 2

    def test_retention_plan_never_deletes_current_reference(self) -> None:
        """Test that retention plan never deletes current reference."""
        connector = MemoryDriveConnector()
        store = SnapshotStore(connector)
        references: list[str] = []
        for value in ("one", "two", "three"):
            content = normalize_content(
                f"<p>{value}</p>".encode(), content_type="text/html"
            )
            references.append(store.save("target", content))
        candidates = store.plan_cleanup(
            "target", current_ref=references[0], retain_snapshots=1
        )
        assert references[0] not in [item.file_ref for item in candidates]

    def test_retention_plan_retains_the_entire_current_reference_group(
        self,
    ) -> None:
        """Test that retention plan retains the entire current reference group."""
        # The current baseline's hash can fall outside the newest
        # ``retain_snapshots`` groups (as here, with retain_snapshots=1 and
        # the oldest snapshot still the active baseline). The whole group --
        # not just the file matching current_ref -- must be retained so its
        # metadata/diff audit artifacts survive alongside normalized.txt.
        connector = MemoryDriveConnector()
        store = SnapshotStore(connector)
        references: list[str] = []
        for value in ("one", "two", "three"):
            content = normalize_content(
                f"<p>{value}</p>".encode(), content_type="text/html"
            )
            references.append(store.save("target", content))
        current_ref = references[0]
        current_path = next(
            path
            for path, file_ref in connector.paths.items()
            if file_ref == current_ref
        )
        current_prefix = current_path.rsplit("/", 1)[0] + "/"
        sibling_paths = {
            path
            for path in connector.paths
            if path.startswith(current_prefix) and path != current_path
        }
        assert sibling_paths
        candidates = store.plan_cleanup(
            "target", current_ref=current_ref, retain_snapshots=1
        )
        candidate_paths = {item.path for item in candidates}
        assert set() == sibling_paths & candidate_paths


class OutboxTests(unittest.TestCase):
    """Tests for OutboxTests."""

    def test_sheet_parsing_duplicate_detection_and_raw_upsert(self) -> None:
        """Test that sheet parsing duplicate detection and raw upsert."""
        record = enqueue_record(
            "e" * 64,
            "one",
            "default",
            "更新",
            now="2026-01-01T00:00:00Z",
        )
        parsed = load_outbox([list(OUTBOX_COLUMNS), record.as_row()])
        assert parsed[record.event_id][1].status == "pending"
        with pytest.raises(MonitorError, match="duplicate"):
            load_outbox([list(OUTBOX_COLUMNS), record.as_row(), record.as_row()])

        class Connector:
            """A fake Drive/Sheets connector used to exercise the tested behavior."""

            def __init__(self) -> None:
                self.values: list[list[str]] = [list(OUTBOX_COLUMNS)]
                self.options: list[str] = []

            def read_values(
                self, spreadsheet_id: str, range_name: str
            ) -> list[list[str]]:
                del spreadsheet_id, range_name
                return self.values

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[object]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.options.append(value_input_option)

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[object]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.options.append(value_input_option)

            def batch_replace_values(
                self,
                spreadsheet_id: str,
                data: Sequence[Mapping[str, object]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, data
                self.options.append(value_input_option)

        connector = Connector()
        store = OutboxSheetsStore(connector, "runtime-only-id")
        store.upsert_outbox(record)
        assert connector.options == ["RAW"]

    def test_rejects_reordered_outbox_header_columns(self) -> None:
        # upsert_outbox writes OUTBOX_COLUMNS in fixed A:I order, so a header
        # row that merely contains every required column out of order would
        # otherwise load fine and then have the next write silently place
        # values (e.g. status/attempt_count) under the wrong headers.
        """Test that rejects reordered outbox header columns."""
        reordered = [
            "event_id",
            "target_id",
            "payload",
            "attempt_count",
            "status",
            "created_at",
            "updated_at",
            "next_attempt_at",
            "last_error",
        ]
        record = enqueue_record(
            "f" * 64,
            "one",
            "default",
            "更新",
            now="2026-01-01T00:00:00Z",
        )
        with pytest.raises(MonitorError, match="must appear first, in this order"):
            load_outbox([reordered, record.as_row()])

    def test_enqueue_success_duplicate_and_retry_states(self) -> None:
        """Test that enqueue success duplicate and retry states."""
        record = enqueue_record(
            "a" * 64,
            "one",
            "default",
            "更新があります",
            now="2026-01-01T00:00:00Z",
        )
        calls: list[tuple[str, str]] = []

        def sender(group: str, message: str) -> str:
            calls.append((group, message))
            return "sent:1"

        sent = dispatch_record(
            record,
            sender,
            persist_transition=lambda _: None,
            now="2026-01-01T00:01:00Z",
        )
        assert sent.status == "sent"
        duplicate = dispatch_record(
            sent,
            sender,
            persist_transition=lambda _: None,
            now="2026-01-01T00:02:00Z",
        )
        assert duplicate.status == "sent"
        assert len(calls) == 1

        def failed_sender(group: str, message: str) -> str:
            del group, message
            raise OutboxDeliveryError(retryable=True)

        retried = dispatch_record(
            enqueue_record(
                "b" * 64,
                "two",
                "default",
                "更新",
                now="2026-01-01T00:00:00Z",
            ),
            failed_sender,
            persist_transition=lambda _: None,
            now="2026-01-01T00:01:00Z",
        )
        assert retried.status == "retry"
        assert retried.next_attempt_at
        with pytest.raises(MonitorError, match="webhook"):
            enqueue_record(
                "f" * 64,
                "one",
                "default",
                "https://hooks.slack.com/services/T/B/secret",
            )

    def test_dispatch_requires_and_commits_sending_before_delivery(self) -> None:
        """Test that dispatch requires and commits sending before delivery."""
        record = enqueue_record(
            "f" * 64,
            "one",
            "default",
            "message",
            now="2026-01-01T00:00:00Z",
        )
        calls: list[str] = []

        def sender(group: str, message: str) -> str:
            del group, message
            calls.append("send")
            return "sent:1"

        with pytest.raises(TypeError):
            dispatch_record(record, sender)  # type: ignore[call-arg]
        assert calls == []

        def failed_persistence(_: object) -> None:
            msg = "store unavailable"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="store unavailable"):
            dispatch_record(
                record,
                sender,
                persist_transition=failed_persistence,
            )
        assert calls == []

    def test_ambiguous_failure_remains_sending_after_persist_transition(self) -> None:
        """Test that ambiguous failure remains sending after persist transition."""
        record = enqueue_record(
            "d" * 64,
            "one",
            "default",
            "message",
            now="2026-01-01T00:00:00Z",
        )
        transitions: list[OutboxRecord] = []

        def ambiguous_sender(group: str, message: str) -> str:
            del group, message
            raise RuntimeError

        result = dispatch_record(
            record,
            ambiguous_sender,
            persist_transition=transitions.append,
            now="2026-01-01T00:01:00Z",
        )
        assert transitions[0].status == "sending"
        assert result.status == "sending"
        assert result.last_error == "delivery_ambiguous"

    def test_poison_and_sending_are_not_delivered(self) -> None:
        """Test that poison and sending are not delivered."""
        record = enqueue_record(
            "c" * 64,
            "one",
            "default",
            "message",
            now="2026-01-01T00:00:00Z",
        )
        poison = type(record)(
            record.event_id,
            record.target_id,
            "{}",
            "pending",
            0,
            record.created_at,
            record.updated_at,
        )

        def unreachable_sender(group: str, message: str) -> str:
            del group, message
            return "sent"

        def noop_persist(record: OutboxRecord) -> None:
            del record

        result = dispatch_record(
            poison, unreachable_sender, persist_transition=noop_persist
        )
        assert result.status == "poison"
        sending = type(record)(
            record.event_id,
            record.target_id,
            record.payload,
            "sending",
            1,
            record.created_at,
            record.updated_at,
        )
        assert sending is dispatch_record(
            sending, unreachable_sender, persist_transition=noop_persist
        )


class ReplayAndAuditTests(unittest.TestCase):
    """Tests for ReplayAndAuditTests."""

    def test_replay_validates_hashes_and_expected_diff(self) -> None:
        """Test that replay validates hashes and expected diff."""
        previous = normalize_content(b"<p>Price $10</p>", content_type="text/html")
        current = normalize_content(b"<p>Price $20</p>", content_type="text/html")
        expected = compare_content(
            previous.text,
            current.text,
            previous_hash=previous.normalized_hash,
            current_hash=current.normalized_hash,
        )
        manifest = {
            "previous": previous.as_dict(),
            "current": current.as_dict(),
            "expected": {
                "result": expected.result,
                "change_score": expected.change_score,
                "significance": expected.significance,
            },
        }
        result = replay_manifest(manifest)
        assert result["hashes_valid"]
        tampered = json.loads(json.dumps(manifest))
        tampered["current"]["text"] = "tampered"
        with pytest.raises(MonitorError, match="hash"):
            replay_manifest(tampered)

    def test_audit_rejects_sensitive_fields(self) -> None:
        """Test that audit rejects sensitive fields."""
        digest = configuration_digest({"threshold": 3})
        record = make_audit_record(
            "configuration_loaded",
            outcome="succeeded",
            run_id="run-1",
            metadata={"configuration_digest": digest},
        )
        assert digest == record.metadata["configuration_digest"]
        with pytest.raises(MonitorError, match="sensitive"):
            make_audit_record(
                "target_execution",
                outcome="failed",
                run_id="run-1",
                metadata={"response_body": "secret"},
            )

    def test_schemas_and_gas_dispatcher_are_safe_static_artifacts(self) -> None:
        """Test that schemas and gas dispatcher are safe static artifacts."""
        schema_dir = (
            support.REPO_ROOT / "skills" / "_weekly-web-monitor-shared" / "schemas"
        )
        schemas = list(schema_dir.glob("*.json"))
        assert len(schemas) >= 10
        for schema in schemas:
            with self.subTest(schema=schema.name):
                value = json.loads(schema.read_text(encoding="utf-8"))
                assert "$schema" in value
                assert value["type"] == "object"
        gas = (support.SCRIPTS / "gas" / "Code.gs").read_text(encoding="utf-8")
        assert "PropertiesService.getScriptProperties()" in gas
        assert "'sending'" in gas
        assert not re.search(r"https://hooks\.slack\.com/", gas)

    def test_gas_dispatcher_poisons_the_wrong_notification_group(self) -> None:
        # The Outbox dispatcher fixes a single Slack destination for a
        # single notification_group. A row tagged for a different group
        # must be poisoned, not silently delivered to that one webhook,
        # since that would leak one team's notification to another team's
        # channel. Only running the real Code.gs through a JS engine (not a
        # Python re-implementation of its logic) proves this.
        """Test that gas dispatcher poisons the wrong notification group."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available to execute Code.gs")
        gas = (support.SCRIPTS / "gas" / "Code.gs").read_text(encoding="utf-8")
        header = [
            "event_id",
            "target_id",
            "payload",
            "status",
            "attempt_count",
            "created_at",
            "updated_at",
            "next_attempt_at",
            "last_error",
        ]
        allowed_row = [
            "event-allowed",
            "target-1",
            json.dumps({"message": "hello-a", "notification_group": "team-a"}),
            "pending",
            0,
            "",
            "",
            "",
            "",
        ]
        mismatched_row = [
            "event-mismatched",
            "target-2",
            json.dumps({"message": "hello-b", "notification_group": "team-b"}),
            "pending",
            0,
            "",
            "",
            "",
            "",
        ]
        harness = f"""
'use strict';
const rows = [{json.dumps(header)}, {json.dumps(allowed_row)}, {json.dumps(mismatched_row)}];
const fetchCalls = [];
global.LockService = {{
  getScriptLock: () => ({{ tryLock: () => true, releaseLock: () => {{}} }}),
}};
global.PropertiesService = {{
  getScriptProperties: () => ({{
    getProperty: (name) => ({{
      SLACK_WEBHOOK_URL: 'https://example.test/webhook',
      ALLOWED_NOTIFICATION_GROUP: 'team-a',
    }}[name] || null),
  }}),
}};
global.SpreadsheetApp = {{
  getActive: () => ({{
    getSheetByName: () => ({{
      getDataRange: () => ({{ getValues: () => rows.map((row) => row.slice()) }}),
      getRange: (rowNumber, columnNumber) => ({{
        setValue: (value) => {{ rows[rowNumber - 1][columnNumber - 1] = value; }},
      }}),
    }}),
  }}),
  flush: () => {{}},
}};
global.UrlFetchApp = {{
  fetch: (url, options) => {{
    fetchCalls.push({{url, body: JSON.parse(options.payload)}});
    return {{ getResponseCode: () => 200 }};
  }},
}};
{gas}
dispatchOutbox();
const byEventId = Object.fromEntries(
  rows.slice(1).map((row) => [row[0], {{status: row[3], last_error: row[8]}}])
);
console.log(JSON.stringify({{fetchCalls, byEventId}}));
"""
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert len(outcome["fetchCalls"]) == 1
        assert outcome["fetchCalls"][0]["body"]["text"] == "hello-a"
        assert outcome["byEventId"]["event-allowed"]["status"] == "sent"
        assert outcome["byEventId"]["event-mismatched"]["status"] == "poison"
        assert (
            outcome["byEventId"]["event-mismatched"]["last_error"]
            == "notification_group_mismatch"
        )

    def test_gas_dispatcher_treats_existing_sending_row_as_delivery_claim(
        self,
    ) -> None:
        # A "sending" row means a prior invocation already dispatched this
        # event and the outcome is ambiguous (see dispatchRow_'s own
        # comment: "sending" is never retried automatically). If a
        # pending/retry row for the same event ID exists too -- e.g. after
        # an ambiguous Slack response or a concurrent duplicate enqueue --
        # it must be poisoned rather than dispatched again. Only running the
        # real Code.gs proves the upfront claimed-event index actually
        # covers "sending", not just "sent".
        """Test that gas dispatcher treats existing sending row as delivery claim."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available to execute Code.gs")
        gas = (support.SCRIPTS / "gas" / "Code.gs").read_text(encoding="utf-8")
        header = [
            "event_id",
            "target_id",
            "payload",
            "status",
            "attempt_count",
            "created_at",
            "updated_at",
            "next_attempt_at",
            "last_error",
        ]
        sending_row = [
            "event-dup",
            "target-1",
            json.dumps({"message": "hello", "notification_group": "team-a"}),
            "sending",
            1,
            "",
            "",
            "",
            "",
        ]
        duplicate_pending_row = [
            "event-dup",
            "target-1",
            json.dumps({"message": "hello", "notification_group": "team-a"}),
            "pending",
            0,
            "",
            "",
            "",
            "",
        ]
        rows_json = json.dumps([header, sending_row, duplicate_pending_row])
        harness = f"""
'use strict';
const rows = {rows_json};
const fetchCalls = [];
global.LockService = {{
  getScriptLock: () => ({{ tryLock: () => true, releaseLock: () => {{}} }}),
}};
global.PropertiesService = {{
  getScriptProperties: () => ({{
    getProperty: (name) => ({{
      SLACK_WEBHOOK_URL: 'https://example.test/webhook',
      ALLOWED_NOTIFICATION_GROUP: 'team-a',
    }}[name] || null),
  }}),
}};
global.SpreadsheetApp = {{
  getActive: () => ({{
    getSheetByName: () => ({{
      getDataRange: () => ({{ getValues: () => rows.map((row) => row.slice()) }}),
      getRange: (rowNumber, columnNumber) => ({{
        setValue: (value) => {{ rows[rowNumber - 1][columnNumber - 1] = value; }},
      }}),
    }}),
  }}),
  flush: () => {{}},
}};
global.UrlFetchApp = {{
  fetch: (url, options) => {{
    fetchCalls.push({{url, body: JSON.parse(options.payload)}});
    return {{ getResponseCode: () => 200 }};
  }},
}};
{gas}
dispatchOutbox();
console.log(JSON.stringify({{
  fetchCalls,
  rows: rows.slice(1).map((row) => ({{status: row[3], last_error: row[8]}})),
}}));
"""
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert len(outcome["fetchCalls"]) == 0
        assert outcome["rows"][0]["status"] == "sending"
        assert outcome["rows"][1]["status"] == "poison"
        assert outcome["rows"][1]["last_error"] == "duplicate_event_id"


class ReplayCliTest(unittest.TestCase):
    """Tests for replay.py's `main` CLI entry point's stdout/stderr contract."""

    def test_usage_error_writes_to_stderr(self) -> None:
        """Test that incorrect argc writes a usage message to stderr and returns 2."""
        with patch("sys.stderr", new_callable=StringIO) as stderr:
            code = replay_main(["replay.py"])
        assert code == 2
        assert "usage" in stderr.getvalue()

    def test_success_writes_json_result_to_stdout(self) -> None:
        """Test that a valid manifest writes the JSON replay result to stdout."""
        previous = normalize_content(b"<p>Price $10</p>", content_type="text/html")
        current = normalize_content(b"<p>Price $20</p>", content_type="text/html")
        manifest = {
            "previous": previous.as_dict(),
            "current": current.as_dict(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = replay_main(["replay.py", str(manifest_path)])
        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["hashes_valid"] is True

    def test_invalid_input_writes_error_json_to_stdout(self) -> None:
        """Test that a missing manifest file writes {"error": ...} JSON to stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = replay_main(["replay.py", str(missing)])
        assert code == 1
        payload = json.loads(stdout.getvalue())
        assert payload["error"]["code"] == "replay_invalid"


if __name__ == "__main__":
    unittest.main()
