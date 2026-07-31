from __future__ import annotations

import json
import shutil
import subprocess
import unittest

import support
from audit import configuration_digest, make_audit_record
from diff import compare_content
from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import MemoryDriveConnector
from normalize import normalize_content
from outbox import (
    OUTBOX_COLUMNS,
    OutboxDeliveryError,
    OutboxSheetsStore,
    dispatch_record,
    enqueue_record,
    load_outbox,
)
from replay import replay_manifest


class DriveTests(unittest.TestCase):
    def test_snapshot_upload_lookup_and_duplicate_are_idempotent(self) -> None:
        connector = MemoryDriveConnector()
        store = SnapshotStore(connector)
        content = normalize_content(
            b"<main><p>Hello</p></main>", content_type="text/html"
        )
        reference = store.save("one", content)
        second = store.save("one", content)
        self.assertEqual(reference, second)
        self.assertEqual(2, len(connector.files))
        self.assertEqual(content.text, store.load_normalized(reference))

    def test_failed_write_and_missing_snapshot_fail_closed(self) -> None:
        connector = MemoryDriveConnector()
        connector.fail_upload = True
        store = SnapshotStore(connector)
        content = normalize_content(b"<p>Hello</p>", content_type="text/html")
        with self.assertRaisesRegex(MonitorError, "fixture upload"):
            store.save("one", content)
        self.assertEqual({}, connector.files)
        with self.assertRaisesRegex(MonitorError, "missing"):
            store.load_normalized("drive:missing")
        bounded_connector = MemoryDriveConnector()
        bounded_connector.files["drive:large"] = b"x" * 2_000
        bounded = SnapshotStore(bounded_connector, max_snapshot_bytes=1_024)
        with self.assertRaisesRegex(MonitorError, "size limit"):
            bounded.load_normalized("drive:large")

    def test_diff_artifact_is_versioned_by_previous_hash_on_a_b_c_b_cycle(
        self,
    ) -> None:
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
        self.assertEqual(ref_first_b, ref_second_b)
        b_prefix = f"snapshots/one/{content_b.normalized_hash}/"
        diff_paths = [
            path
            for path in connector.paths
            if path.startswith(b_prefix) and "diff" in path
        ]
        self.assertEqual(2, len(diff_paths))
        contents = {connector.files[connector.paths[path]] for path in diff_paths}
        self.assertEqual(2, len(contents))

    def test_retention_plan_never_deletes_current_reference(self) -> None:
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
        self.assertNotIn(references[0], [item.file_ref for item in candidates])

    def test_retention_plan_retains_the_entire_current_reference_group(
        self,
    ) -> None:
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
        self.assertTrue(sibling_paths)
        candidates = store.plan_cleanup(
            "target", current_ref=current_ref, retain_snapshots=1
        )
        candidate_paths = {item.path for item in candidates}
        self.assertEqual(set(), sibling_paths & candidate_paths)


class OutboxTests(unittest.TestCase):
    def test_sheet_parsing_duplicate_detection_and_raw_upsert(self) -> None:
        record = enqueue_record(
            "e" * 64,
            "one",
            "default",
            "更新",
            now="2026-01-01T00:00:00Z",
        )
        parsed = load_outbox([list(OUTBOX_COLUMNS), record.as_row()])
        self.assertEqual("pending", parsed[record.event_id][1].status)
        with self.assertRaisesRegex(MonitorError, "duplicate"):
            load_outbox([list(OUTBOX_COLUMNS), record.as_row(), record.as_row()])

        class Connector:
            def __init__(self) -> None:
                self.values = [list(OUTBOX_COLUMNS)]
                self.options: list[str] = []

            def read_values(self, spreadsheet_id: str, range_name: str):
                del spreadsheet_id, range_name
                return self.values

            def replace_values(self, *args, value_input_option: str) -> None:
                del args
                self.options.append(value_input_option)

            def append_values(self, *args, value_input_option: str) -> None:
                del args
                self.options.append(value_input_option)

        connector = Connector()
        store = OutboxSheetsStore(connector, "runtime-only-id")
        store.upsert_outbox(record)
        self.assertEqual(["RAW"], connector.options)

    def test_enqueue_success_duplicate_and_retry_states(self) -> None:
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

        sent = dispatch_record(record, sender, now="2026-01-01T00:01:00Z")
        self.assertEqual("sent", sent.status)
        duplicate = dispatch_record(sent, sender, now="2026-01-01T00:02:00Z")
        self.assertEqual("sent", duplicate.status)
        self.assertEqual(1, len(calls))

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
            now="2026-01-01T00:01:00Z",
        )
        self.assertEqual("retry", retried.status)
        self.assertTrue(retried.next_attempt_at)
        with self.assertRaisesRegex(MonitorError, "webhook"):
            enqueue_record(
                "f" * 64,
                "one",
                "default",
                "https://hooks.slack.com/services/T/B/secret",
            )

    def test_ambiguous_failure_remains_sending_after_persist_transition(self) -> None:
        record = enqueue_record(
            "d" * 64,
            "one",
            "default",
            "message",
            now="2026-01-01T00:00:00Z",
        )
        transitions = []

        def ambiguous_sender(group: str, message: str) -> str:
            del group, message
            raise RuntimeError

        result = dispatch_record(
            record,
            ambiguous_sender,
            persist_transition=transitions.append,
            now="2026-01-01T00:01:00Z",
        )
        self.assertEqual("sending", transitions[0].status)
        self.assertEqual("sending", result.status)
        self.assertEqual("delivery_ambiguous", result.last_error)

    def test_poison_and_sending_are_not_delivered(self) -> None:
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
        result = dispatch_record(poison, lambda *_: "sent")
        self.assertEqual("poison", result.status)
        sending = type(record)(
            record.event_id,
            record.target_id,
            record.payload,
            "sending",
            1,
            record.created_at,
            record.updated_at,
        )
        self.assertIs(sending, dispatch_record(sending, lambda *_: "sent"))


class ReplayAndAuditTests(unittest.TestCase):
    def test_replay_validates_hashes_and_expected_diff(self) -> None:
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
        self.assertTrue(result["hashes_valid"])
        tampered = json.loads(json.dumps(manifest))
        tampered["current"]["text"] = "tampered"
        with self.assertRaisesRegex(MonitorError, "hash"):
            replay_manifest(tampered)

    def test_audit_rejects_sensitive_fields(self) -> None:
        digest = configuration_digest({"threshold": 3})
        record = make_audit_record(
            "configuration_loaded",
            outcome="succeeded",
            run_id="run-1",
            metadata={"configuration_digest": digest},
        )
        self.assertEqual(digest, record.metadata["configuration_digest"])
        with self.assertRaisesRegex(MonitorError, "sensitive"):
            make_audit_record(
                "target_execution",
                outcome="failed",
                run_id="run-1",
                metadata={"response_body": "secret"},
            )

    def test_schemas_and_gas_dispatcher_are_safe_static_artifacts(self) -> None:
        schema_dir = (
            support.REPO_ROOT / ".claude" / "skills" / "weekly-web-monitor" / "schemas"
        )
        schemas = list(schema_dir.glob("*.json"))
        self.assertGreaterEqual(len(schemas), 10)
        for schema in schemas:
            with self.subTest(schema=schema.name):
                value = json.loads(schema.read_text(encoding="utf-8"))
                self.assertIn("$schema", value)
                self.assertEqual("object", value["type"])
        gas = (support.SCRIPTS / "gas" / "Code.gs").read_text(encoding="utf-8")
        self.assertIn("PropertiesService.getScriptProperties()", gas)
        self.assertIn("'sending'", gas)
        self.assertNotRegex(gas, r"https://hooks\.slack\.com/")

    def test_gas_dispatcher_poisons_the_wrong_notification_group(self) -> None:
        # The Outbox dispatcher fixes a single Slack destination for a
        # single notification_group. A row tagged for a different group
        # must be poisoned, not silently delivered to that one webhook,
        # since that would leak one team's notification to another team's
        # channel. Only running the real Code.gs through a JS engine (not a
        # Python re-implementation of its logic) proves this.
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available to execute Code.gs")
        gas = (support.SCRIPTS / "gas" / "Code.gs").read_text(encoding="utf-8")
        header = [
            "event_id", "target_id", "payload", "status", "attempt_count",
            "created_at", "updated_at", "next_attempt_at", "last_error",
        ]
        allowed_row = [
            "event-allowed", "target-1",
            json.dumps({"message": "hello-a", "notification_group": "team-a"}),
            "pending", 0, "", "", "", "",
        ]
        mismatched_row = [
            "event-mismatched", "target-2",
            json.dumps({"message": "hello-b", "notification_group": "team-b"}),
            "pending", 0, "", "", "", "",
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
            [node, "-e", harness], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(0, result.returncode, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(1, len(outcome["fetchCalls"]))
        self.assertEqual("hello-a", outcome["fetchCalls"][0]["body"]["text"])
        self.assertEqual("sent", outcome["byEventId"]["event-allowed"]["status"])
        self.assertEqual(
            "poison", outcome["byEventId"]["event-mismatched"]["status"]
        )
        self.assertEqual(
            "notification_group_mismatch",
            outcome["byEventId"]["event-mismatched"]["last_error"],
        )


if __name__ == "__main__":
    unittest.main()
