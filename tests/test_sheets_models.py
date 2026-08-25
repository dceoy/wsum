"""Tests for the sheets_models module."""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING, Any, Never

import pytest
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

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def table(headers: tuple[str, ...], *rows: list[object]) -> list[list[object]]:
    """Build a values table with ``headers`` as the first row.

    Returns:
        The headers row followed by ``rows``.
    """
    return [list(headers), *rows]


class ModelsAndSheetsTests(unittest.TestCase):
    """Tests for ModelsAndSheetsTests."""

    def test_loads_only_enabled_targets(self) -> None:
        """Test that loads only enabled targets."""
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
        assert [target.target_id for target in targets] == ["one"]
        assert targets[0].exclude_selectors == (".ad", ".nav")

    def test_invalid_structure_empty_and_duplicate_ids_fail(self) -> None:
        """Test that invalid structure empty and duplicate ids fail."""
        with pytest.raises(MonitorError, match="header row"):
            load_enabled_targets([])
        with pytest.raises(MonitorError, match="missing required columns"):
            load_enabled_targets([["target_id"], ["one"]])
        duplicate = table(
            TARGET_COLUMNS,
            ["one", True, "One", "https://example.com", "static", "", "", "", "d"],
            ["one", True, "Two", "https://example.org", "static", "", "", "", "d"],
        )
        with pytest.raises(MonitorError, match="duplicate"):
            load_enabled_targets(duplicate)

    def test_rejects_bad_values_and_credential_urls(self) -> None:
        """Test that rejects bad values and credential urls."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com/?token=secret",
        }
        with pytest.raises(MonitorError, match="credential-like"):
            Target.from_mapping(base)
        base["url"] = "https://hooks.slack.com/%73ervices/T/B/secret"
        with pytest.raises(MonitorError, match="webhook"):
            Target.from_mapping(base)
        base["url"] = "file:///tmp/test"
        with pytest.raises(MonitorError):
            Target.from_mapping(base)

    def test_rejects_url_fragments(self) -> None:
        # Credential-like query parameters are only checked in parsed.query.
        # A fragment such as "#access_token=secret" survives that check and
        # is then copied verbatim into the summary model context and Slack
        # notification text, so fragments must be rejected outright.
        """Test that rejects url fragments."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com/#access_token=secret",
        }
        with pytest.raises(MonitorError, match="fragment"):
            Target.from_mapping(base)

    def test_rejects_provider_prefixed_signed_url_credentials(self) -> None:
        # An exact-name denylist misses namespaced signed-URL parameters:
        # a signed target URL would otherwise reach the summary model
        # context and the Slack notification text.
        """Test that rejects provider prefixed signed url credentials."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        signed_urls = (
            ("https://bucket.s3.amazonaws.com/key"
            "?X-Amz-Credential=AKIAEXAMPLE%2F20260101%2Fus-east-1%2Fs3%2Faws4_request"),
            "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc123",
            "https://storage.googleapis.com/bucket/key?X-Goog-Signature=abc123",
            "https://bucket.s3.amazonaws.com/key?AWSAccessKeyId=AKIAEXAMPLE",
        )
        for url in signed_urls:
            with self.subTest(url=url), pytest.raises(MonitorError, match="credential-like"):
                Target.from_mapping({**base, "url": url})

    def test_rejects_credential_bearing_urls_nested_in_query_values(self) -> None:
        # The outer query parameter name (e.g. "redirect") can be benign
        # while its decoded value is itself an HTTP(S) URL carrying a
        # credential in its own query or userinfo; that nested URL would
        # otherwise reach the summary model context and Slack notification
        # text unexamined.
        """Test that rejects credential bearing urls nested in query values."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        nested_urls = (
            ("https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
            "cb%3Faccess_token%3Dsecret"),
            ("https://example.com/?next=https%3A%2F%2Fbucket.s3.amazonaws.com"
            "%2Fkey%3FX-Amz-Signature%3Dabc123"),
            "https://example.com/?redirect=https%3A%2F%2Fuser%3Apass%40evil.com",
            # An OAuth implicit-flow token after "#" in the nested URL, not
            # its query string.
            ("https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
            "cb%23access_token%3Dsecret"),
        )
        for url in nested_urls:
            with self.subTest(url=url), pytest.raises(MonitorError, match="credential-like"):
                Target.from_mapping({**base, "url": url})

    def test_rejects_nested_webhook_urls_in_query_values(self) -> None:
        # A nested URL's own userinfo/query/fragment were checked, but its
        # host/path were not, so a benign-looking outer parameter could
        # still carry an encoded Slack/Discord webhook URL through
        # unexamined and later reach the summary model context and Slack
        # notification text.
        """Test that rejects nested webhook urls in query values."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        nested_webhook_urls = (
            ("https://example.com/?redirect=https%3A%2F%2Fhooks.slack.com"
            "%2Fservices%2FT00000000%2FB00000000%2FXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("https://example.com/?next=https%3A%2F%2Fdiscord.com"
            "%2Fapi%2Fwebhooks%2F123456789%2Fabcdef"),
        )
        for url in nested_webhook_urls:
            with self.subTest(url=url), pytest.raises(MonitorError, match="credential-like"):
                Target.from_mapping({**base, "url": url})

    def test_rejects_nested_webhook_url_carried_in_a_fragment_value(self) -> None:
        # A nested URL's fragment was only checked for sensitive parameter
        # *names*, so a benign fragment name (e.g. "next") whose value is
        # itself an encoded Slack/Discord webhook URL passed through
        # unexamined; it must now recurse into fragment values the same way
        # it recurses into query values.
        """Test that rejects nested webhook url carried in a fragment value."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": (
                "https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
                "cb%23next%3Dhttps%253A%252F%252Fhooks.slack.com%252Fservices"
                "%252FT00000000%252FB00000000%252FXXXXXXXXXXXXXXXXXXXXXXXX"
            ),
        }
        with pytest.raises(MonitorError, match="credential-like"):
            Target.from_mapping(base)

    def test_rejects_a_fragment_that_is_itself_a_nested_credential_url(self) -> None:
        # A nested URL's fragment was only ever decoded as key/value pairs
        # via parse_qsl, so a fragment that is itself a bare encoded URL
        # (rather than "#next=<url>") parsed as a single blank-valued
        # parameter name and _split_nested_url never inspected it, letting
        # a credential-bearing target reach the summary model and Slack.
        """Test that rejects a fragment that is itself a nested credential url."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": (
                "https://example.com/?redirect=https%3A%2F%2Fidp.example"
                "%2Fcb%23https%253A%252F%252Fuser%253Apass%2540example.com%2F"
            ),
        }
        with pytest.raises(MonitorError, match="credential-like"):
            Target.from_mapping(base)

    def test_rejects_scheme_relative_and_double_encoded_nested_webhook_urls(
        self,
    ) -> None:
        # The nested check only recognized an explicit http(s) scheme, and
        # only a single layer of percent-encoding, so a scheme-relative
        # ("//host/...", a network-path reference resolved against the
        # current scheme) or double-encoded nested webhook URL slipped
        # through with the credential fully recoverable.
        """Test that rejects scheme relative and double encoded nested webhook urls."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        bypassing_urls = (
            ("https://example.com/?redirect=%2F%2Fhooks.slack.com%2Fservices"
            "%2FT00000000%2FB00000000%2FXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("https://example.com/?redirect=https%253A%252F%252Fhooks.slack"
            ".com%252Fservices%252FT00000000%252FB00000000"
            "%252FXXXXXXXXXXXXXXXXXXXXXXXX"),
        )
        for url in bypassing_urls:
            with self.subTest(url=url), pytest.raises(MonitorError, match="credential-like"):
                Target.from_mapping({**base, "url": url})

    def test_malformed_nested_url_in_query_value_does_not_crash(self) -> None:
        # A decoded query value that merely looks like it could be a URL
        # (e.g. an unbalanced IPv6-literal-style host) must not escape as an
        # unhandled ValueError from urlsplit; it should just be treated as
        # an opaque, non-URL value.
        """Test that malformed nested url in query value does not crash."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com/?redirect=http%3A%2F%2F%5B%3A%3A1",
        }
        Target.from_mapping(base)

    def test_exclude_selectors_enforce_count_and_length_bounds(self) -> None:
        """Test that exclude selectors enforce count and length bounds."""
        base = {
            "target_id": "one",
            "enabled": True,
            "name": "One",
            "url": "https://example.com",
        }
        with pytest.raises(MonitorError, match="count or length limit"):
            Target.from_mapping({**base, "exclude_selectors": [".a"] * 51})
        with pytest.raises(MonitorError, match="count or length limit"):
            Target.from_mapping({**base, "exclude_selectors": ["." + "a" * 500]})

    def test_state_and_notification_queries(self) -> None:
        """Test that state and notification queries."""
        digest = "a" * 64
        states = load_states(
            table(
                STATE_COLUMNS,
                ["one", "", "", "", "", digest, "drive:1", 2],
            )
        )
        assert states["one"][0] == 2
        assert states["one"][1].consecutive_failures == 2
        notifications = load_notifications(
            table(
                NOTIFICATION_COLUMNS,
                [digest, "one", "sent", "2026-01-01T00:00:00Z"],
            )
        )
        assert notifications[digest][1].status == "sent"
        with pytest.raises(MonitorError, match="validator"):
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
        """Test that write payload generation."""
        state = State("one")
        assert replace_state_payload(3, state)["range"] == "State!A3:H3"
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
        assert payload["range"] == "Runs!A:H"
        assert '"attempts"' in payload["values"][0][4]
        notification = NotificationRecord("b" * 64, "one", "pending")
        assert upsert_notification_payload(notification)["mode"] == "append"
        assert upsert_notification_payload(notification)["range"] == "Notifications!A:F"
        assert upsert_notification_payload(notification, 4)["mode"] == "replace"
        assert upsert_notification_payload(notification, 4)["range"] == "Notifications!A4:F4"

    def test_record_parser_allows_extra_columns_but_not_extra_values(self) -> None:
        """Test that record parser allows extra columns but not extra values."""
        values = [["id", "extra"], ["one", "x"]]
        assert records_from_values(values, ("id",), "Test")[0]["extra"] == "x"
        with pytest.raises(MonitorError):
            records_from_values([["id"], ["one", "x"]], ("id",), "Test")

    def test_record_parser_rejects_reordered_required_columns(self) -> None:
        # Write paths serialize STATE_COLUMNS in fixed A:H order, so a header
        # row that merely contains every required column out of order would
        # otherwise load fine and then have the next write silently swap
        # values (e.g. etag/last_modified) under the wrong headers.
        """Test that record parser rejects reordered required columns."""
        digest = "a" * 64
        reordered = table(
            (
                "target_id",
                "last_checked_at",
                "last_modified",
                "etag",
                "validated_url",
                "normalized_hash",
                "snapshot_ref",
                "consecutive_failures",
            ),
            ["one", "", "", "", "", digest, "drive:1", 2],
        )
        with pytest.raises(MonitorError, match="must appear first, in this order"):
            load_states(reordered)

        with pytest.raises(MonitorError):
            records_from_values([["extra", "id"], ["x", "one"]], ("id",), "Test")

    def test_store_uses_raw_write_semantics_and_idempotent_runs(self) -> None:
        """Test that store uses raw write semantics and idempotent runs."""
        class Connector:
            """A fake Sheets connector used to exercise the tested behavior."""

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.values: dict[str, list[list[Any]]] = {
                    "State!A:H": [list(STATE_COLUMNS)],
                    "Runs!A:H": [list(RUN_COLUMNS)],
                    "Notifications!A:F": [list(NOTIFICATION_COLUMNS)],
                }

            def read_values(
                self, spreadsheet_id: str, range_name: str
            ) -> list[list[Any]]:
                self.assert_id = spreadsheet_id
                return self.values[range_name]

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.calls.append(("replace", value_input_option))

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, values
                self.calls.append(("append", value_input_option))

            def batch_replace_values(
                self,
                spreadsheet_id: str,
                data: Sequence[Mapping[str, Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, data, value_input_option
                msg = "batch_replace_values is not used by this test"
                raise AssertionError(msg)

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
        assert connector.calls
        assert all(option == "RAW" for _, option in connector.calls)

    def test_get_run_round_trips_result_and_attempts(self) -> None:
        """Test that get run round trips result and attempts."""
        class Connector:
            """A fake Sheets connector used to exercise the tested behavior."""

            def __init__(self) -> None:
                self.values: dict[str, list[list[Any]]] = {"Runs!A:H": [list(RUN_COLUMNS)]}

            def read_values(
                self, spreadsheet_id: str, range_name: str
            ) -> list[list[Any]]:
                del spreadsheet_id
                return self.values[range_name]

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, range_name, values, value_input_option
                msg = "runs are append-only"
                raise AssertionError(msg)

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, value_input_option
                self.values[range_name] = [*self.values[range_name], *(list(row) for row in values)]

            def batch_replace_values(
                self,
                spreadsheet_id: str,
                data: Sequence[Mapping[str, Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, data, value_input_option
                msg = "batch_replace_values is not used by this test"
                raise AssertionError(msg)

        connector = Connector()
        store = SheetsStore(connector, "runtime-only-id")
        run = RunRecord(
            "run-1:one",
            "one",
            "material",
            42,
            "重要な変更です",
            "",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            (Attempt(1, "succeeded"),),
        )
        store.append_run(run)
        assert store.get_run("run-1:two") is None
        assert run == store.get_run("run-1:one")

    def test_notification_round_trips_kind_and_last_error(self) -> None:
        """Test that notification round trips kind and last error."""
        class Connector:
            """A fake Sheets connector used to exercise the tested behavior."""

            def __init__(self) -> None:
                self.values: dict[str, list[list[Any]]] = {"Notifications!A:F": [list(NOTIFICATION_COLUMNS)]}

            def read_values(
                self, spreadsheet_id: str, range_name: str
            ) -> list[list[Any]]:
                del spreadsheet_id
                return self.values[range_name]

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, value_input_option
                self.values["Notifications!A:F"] = [
                    list(NOTIFICATION_COLUMNS),
                    *(list(row) for row in values),
                ]

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id, range_name, value_input_option
                self.values["Notifications!A:F"] = [
                    *self.values["Notifications!A:F"],
                    *(list(row) for row in values),
                ]

            def batch_replace_values(
                self,
                spreadsheet_id: str,
                data: Sequence[Mapping[str, Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, data, value_input_option
                msg = "batch_replace_values is not used by this test"
                raise AssertionError(msg)

        connector = Connector()
        store = SheetsStore(connector, "runtime-only-id")
        notification = NotificationRecord(
            "c" * 64,
            "one",
            "failed",
            kind="failure",
            last_error="notification_send_failed",
        )
        store.upsert_notification(notification)
        assert notification == store.get_notification("c" * 64)

    def test_notification_batch_uses_one_atomic_raw_connector_call(self) -> None:
        """Test that notification batch uses one atomic raw connector call."""
        first = NotificationRecord("d" * 64, "one", "pending")
        second = NotificationRecord("e" * 64, "two", "pending")

        class Connector:
            """A fake Sheets connector used to exercise the tested behavior."""

            def __init__(self) -> None:
                self.values: dict[str, list[list[Any]]] = {
                    "Notifications!A:F": [
                        list(NOTIFICATION_COLUMNS),
                        [first.event_id, "one", "pending", "", "change", ""],
                        [second.event_id, "two", "pending", "", "change", ""],
                    ]
                }
                self.batches: list[
                    tuple[Sequence[Mapping[str, Any]], str]
                ] = []

            def read_values(
                self, spreadsheet_id: str, range_name: str
            ) -> list[list[Any]]:
                del spreadsheet_id
                return self.values[range_name]

            def replace_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, range_name, values, value_input_option
                msg = "replace_values is not used by this test"
                raise AssertionError(msg)

            def append_values(
                self,
                spreadsheet_id: str,
                range_name: str,
                values: Sequence[Sequence[Any]],
                *,
                value_input_option: str,
            ) -> Never:
                del spreadsheet_id, range_name, values, value_input_option
                msg = "append_values is not used by this test"
                raise AssertionError(msg)

            def batch_replace_values(
                self,
                spreadsheet_id: str,
                data: Sequence[Mapping[str, Any]],
                *,
                value_input_option: str,
            ) -> None:
                del spreadsheet_id
                self.batches.append((data, value_input_option))

        connector = Connector()
        store = SheetsStore(connector, "runtime-only-id")
        store.upsert_notifications_atomically(
            [
                NotificationRecord(
                    first.event_id,
                    "one",
                    "sent",
                    notified_at="2026-01-01T00:00:00Z",
                ),
                NotificationRecord(
                    second.event_id,
                    "two",
                    "sent",
                    notified_at="2026-01-01T00:00:00Z",
                ),
            ]
        )
        assert len(connector.batches) == 1
        data, option = connector.batches[0]
        assert option == "RAW"
        assert [item["range"] for item in data] == ["Notifications!A2:F2", "Notifications!A3:F3"]


if __name__ == "__main__":
    unittest.main()
