from __future__ import annotations

import unittest

import support  # noqa: F401
from errors import MonitorError
from models import Attempt, NotificationRecord, RunRecord, State, Target
from sheets import (
    NOTIFICATION_COLUMNS,
    RUN_COLUMNS,
    STATE_COLUMNS,
    TARGET_COLUMNS,
    SheetsStore,
    append_run_payload,
    load_enabled_targets,
    load_notifications,
    load_states,
    records_from_values,
    replace_state_payload,
    upsert_notification_payload,
)


def table(headers: tuple[str, ...], *rows: list[object]) -> list[list[object]]:
    return [list(headers), *rows]


class ModelsAndSheetsTests(unittest.TestCase):
    def test_loads_only_enabled_targets(self) -> None:
        values = table(
            TARGET_COLUMNS,
            [
                "one",
                "TRUE",
                "One",
                "https://example.com/one",
                "static",
                "main",
                ".ad,.nav",
                "price",
                "default",
            ],
            [
                "two",
                "false",
                "Two",
                "https://example.com/two",
                "static",
                "",
                "",
                "",
                "default",
            ],
        )
        targets = load_enabled_targets(values)
        self.assertEqual(["one"], [target.target_id for target in targets])
        self.assertEqual((".ad", ".nav"), targets[0].exclude_selectors)

    def test_invalid_structure_empty_and_duplicate_ids_fail(self) -> None:
        with self.assertRaisesRegex(MonitorError, "header row"):
            load_enabled_targets([])
        with self.assertRaisesRegex(MonitorError, "missing required columns"):
            load_enabled_targets([["target_id"], ["one"]])
        duplicate = table(
            TARGET_COLUMNS,
            ["one", True, "One", "https://example.com", "static", "", "", "", "d"],
            ["one", True, "Two", "https://example.org", "static", "", "", "", "d"],
        )
        with self.assertRaisesRegex(MonitorError, "duplicate"):
            load_enabled_targets(duplicate)

    def test_rejects_bad_values_and_credential_urls(self) -> None:
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com/?token=secret",
        }
        with self.assertRaisesRegex(MonitorError, "credential-like"):
            Target.from_mapping(base)
        base["url"] = "https://hooks.slack.com/%73ervices/T/B/secret"
        with self.assertRaisesRegex(MonitorError, "webhook"):
            Target.from_mapping(base)
        base["url"] = "file:///tmp/test"
        with self.assertRaises(MonitorError):
            Target.from_mapping(base)

    def test_exclude_selectors_enforce_count_and_length_bounds(self) -> None:
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        with self.assertRaisesRegex(MonitorError, "count or length limit"):
            Target.from_mapping({**base, "exclude_selectors": [".a"] * 51})
        with self.assertRaisesRegex(MonitorError, "count or length limit"):
            Target.from_mapping({**base, "exclude_selectors": ["." + "a" * 500]})

    def test_state_and_notification_queries(self) -> None:
        digest = "a" * 64
        states = load_states(
            table(
                STATE_COLUMNS,
                ["one", "", "", "", "", digest, "drive:1", 2],
            )
        )
        self.assertEqual(2, states["one"][0])
        self.assertEqual(2, states["one"][1].consecutive_failures)
        notifications = load_notifications(
            table(
                NOTIFICATION_COLUMNS,
                [digest, "one", "sent", "2026-01-01T00:00:00Z"],
            )
        )
        self.assertEqual("sent", notifications[digest][1].status)
        with self.assertRaisesRegex(MonitorError, "validator"):
            State.from_mapping(
                {
                    "target_id": "one",
                    "etag": "bad\r\nvalue",
                    "last_checked_at": "",
                    "last_modified": "",
                    "normalized_hash": "",
                    "snapshot_ref": "",
                    "consecutive_failures": 0,
                }
            )

    def test_write_payload_generation(self) -> None:
        state = State("one")
        self.assertEqual("State!A3:H3", replace_state_payload(3, state)["range"])
        run = RunRecord(
            "run:one",
            "one",
            "unchanged",
            0,
            "",
            "",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            (Attempt(1, "succeeded"),),
        )
        payload = append_run_payload(run)
        self.assertEqual("Runs!A:H", payload["range"])
        self.assertIn('"attempts"', payload["values"][0][4])
        notification = NotificationRecord("b" * 64, "one", "pending")
        self.assertEqual("append", upsert_notification_payload(notification)["mode"])
        self.assertEqual(
            "replace", upsert_notification_payload(notification, 4)["mode"]
        )

    def test_record_parser_allows_extra_columns_but_not_extra_values(self) -> None:
        values = [["id", "extra"], ["one", "x"]]
        self.assertEqual("x", records_from_values(values, ("id",), "Test")[0]["extra"])
        with self.assertRaises(MonitorError):
            records_from_values([["id"], ["one", "x"]], ("id",), "Test")

    def test_store_uses_raw_write_semantics_and_idempotent_runs(self) -> None:
        class Connector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.values = {
                    "State!A:H": [list(STATE_COLUMNS)],
                    "Runs!A:H": [list(RUN_COLUMNS)],
                    "Notifications!A:F": [list(NOTIFICATION_COLUMNS)],
                }

            def read_values(self, spreadsheet_id: str, range_name: str):
                self.assert_id = spreadsheet_id
                return self.values[range_name]

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values,
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.calls.append(("replace", value_input_option))

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values,
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.calls.append(("append", value_input_option))

        connector = Connector()
        store = SheetsStore(connector, "runtime-only-id")
        store.replace_state(State("one", etag="=UNTRUSTED()"))
        run = RunRecord(
            "run:one",
            "one",
            "unchanged",
            0,
            "",
            "",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
        )
        store.append_run(run)
        self.assertTrue(connector.calls)
        self.assertTrue(all(option == "RAW" for _, option in connector.calls))


if __name__ == "__main__":
    unittest.main()
