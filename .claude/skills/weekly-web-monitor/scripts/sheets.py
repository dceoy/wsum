"""Pure Google Sheets parsing plus connector-backed storage operations.

The connector is injected by the Routine. This module never loads credentials or
stores a spreadsheet identifier in source control.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any, Protocol

from errors import MonitorError
from models import NotificationRecord, RunRecord, State, Target

TARGET_COLUMNS = (
    "target_id",
    "enabled",
    "name",
    "url",
    "fetch_mode",
    "include_selector",
    "exclude_selectors",
    "watch_focus",
    "notification_group",
)
STATE_COLUMNS = (
    "target_id",
    "last_checked_at",
    "etag",
    "last_modified",
    "validated_url",
    "normalized_hash",
    "snapshot_ref",
    "consecutive_failures",
)
RUN_COLUMNS = (
    "run_id",
    "target_id",
    "result",
    "change_score",
    "summary",
    "error_code",
    "started_at",
    "finished_at",
)
NOTIFICATION_COLUMNS = ("event_id", "target_id", "status", "notified_at")


class SheetsConnector(Protocol):
    """Minimal least-privilege connector surface required by SheetsStore."""

    def read_values(
        self, spreadsheet_id: str, range_name: str
    ) -> Sequence[Sequence[Any]]: ...

    def replace_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[Any]],
        *,
        value_input_option: str,
    ) -> None: ...

    def append_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[Any]],
        *,
        value_input_option: str,
    ) -> None: ...


def _normalize_table(values: Sequence[Sequence[Any]], sheet: str) -> list[list[Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise MonitorError(
            "sheet_invalid_structure",
            f"{sheet}: expected a two-dimensional values array",
        )
    table: list[list[Any]] = []
    for row in values:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise MonitorError(
                "sheet_invalid_structure", f"{sheet}: every row must be an array"
            )
        table.append(list(row))
    return table


def records_from_values(
    values: Sequence[Sequence[Any]],
    required_columns: Sequence[str],
    sheet: str,
    *,
    allow_empty: bool = True,
) -> list[dict[str, Any]]:
    table = _normalize_table(values, sheet)
    if not table:
        if allow_empty:
            return []
        raise MonitorError("sheet_empty", f"{sheet}: header row is missing")
    headers = [str(value).strip() for value in table[0]]
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        raise MonitorError(
            "sheet_invalid_structure", f"{sheet}: headers must be non-empty and unique"
        )
    missing = [column for column in required_columns if column not in headers]
    if missing:
        raise MonitorError(
            "sheet_missing_columns",
            f"{sheet}: missing required columns: {', '.join(missing)}",
            details={"missing_columns": missing},
        )
    records: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(table[1:], start=2):
        if not raw_row or all(str(value).strip() == "" for value in raw_row):
            continue
        if len(raw_row) > len(headers):
            raise MonitorError(
                "sheet_invalid_row", f"{sheet}: row {row_number} has too many values"
            )
        padded = raw_row + [""] * (len(headers) - len(raw_row))
        record = dict(zip(headers, padded, strict=True))
        record["_row_number"] = row_number
        records.append(record)
    return records


def _ensure_unique(
    records: Iterable[Mapping[str, Any]], key: str, sheet: str
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        value = str(record.get(key, "")).strip()
        if not value:
            raise MonitorError(
                "sheet_invalid_row",
                f"{sheet}: row {record.get('_row_number', '?')} has no {key}",
            )
        if value in seen:
            raise MonitorError(
                "sheet_duplicate_id", f"{sheet}: duplicate {key}: {value}"
            )
        seen.add(value)
        result.append(record)
    return result


def load_enabled_targets(values: Sequence[Sequence[Any]]) -> list[Target]:
    records = _ensure_unique(
        records_from_values(values, TARGET_COLUMNS, "Targets", allow_empty=False),
        "target_id",
        "Targets",
    )
    targets = [Target.from_mapping(record) for record in records]
    return [target for target in targets if target.enabled]


def load_states(values: Sequence[Sequence[Any]]) -> dict[str, tuple[int, State]]:
    records = _ensure_unique(
        records_from_values(values, STATE_COLUMNS, "State", allow_empty=False),
        "target_id",
        "State",
    )
    return {
        state.target_id: (int(record["_row_number"]), state)
        for record in records
        for state in (State.from_mapping(record),)
    }


def load_notifications(
    values: Sequence[Sequence[Any]],
) -> dict[str, tuple[int, NotificationRecord]]:
    records = _ensure_unique(
        records_from_values(
            values, NOTIFICATION_COLUMNS, "Notifications", allow_empty=False
        ),
        "event_id",
        "Notifications",
    )
    result: dict[str, tuple[int, NotificationRecord]] = {}
    for record in records:
        notification = NotificationRecord(
            event_id=str(record["event_id"]).strip(),
            target_id=str(record["target_id"]).strip(),
            status=str(record["status"]).strip(),
            notified_at=str(record["notified_at"]).strip(),
            kind=str(record.get("kind", "change") or "change").strip(),
            last_error=str(record.get("last_error", "") or "").strip(),
        )
        result[notification.event_id] = (int(record["_row_number"]), notification)
    return result


def state_row(state: State) -> list[Any]:
    value = state.as_dict()
    return [value[column] for column in STATE_COLUMNS]


def run_row(run: RunRecord) -> list[Any]:
    value = run.as_dict()
    summary = value["summary"]
    if value["attempts"]:
        summary = json.dumps(
            {"text": summary, "attempts": value["attempts"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    value["summary"] = summary
    return [value[column] for column in RUN_COLUMNS]


def notification_row(notification: NotificationRecord) -> list[Any]:
    value = asdict(notification)
    return [value[column] for column in NOTIFICATION_COLUMNS]


def replace_state_payload(row_number: int, state: State) -> dict[str, Any]:
    if row_number < 2:
        raise MonitorError("sheet_invalid_row", "State row_number must be at least 2")
    return {
        "range": f"State!A{row_number}:H{row_number}",
        "values": [state_row(state)],
    }


def append_state_payload(state: State) -> dict[str, Any]:
    return {"range": "State!A:H", "values": [state_row(state)]}


def append_run_payload(run: RunRecord) -> dict[str, Any]:
    return {"range": "Runs!A:H", "values": [run_row(run)]}


def upsert_notification_payload(
    notification: NotificationRecord, row_number: int | None = None
) -> dict[str, Any]:
    row = notification_row(notification)
    if row_number is None:
        return {"range": "Notifications!A:D", "values": [row], "mode": "append"}
    if row_number < 2:
        raise MonitorError(
            "sheet_invalid_row", "Notifications row_number must be at least 2"
        )
    return {
        "range": f"Notifications!A{row_number}:D{row_number}",
        "values": [row],
        "mode": "replace",
    }


class SheetsStore:
    """Operational store that delegates all I/O to an injected connector."""

    def __init__(self, connector: SheetsConnector, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise MonitorError(
                "connector_configuration_missing",
                "spreadsheet_id must be supplied at runtime",
            )
        self._connector = connector
        self._spreadsheet_id = spreadsheet_id

    def load_enabled_targets(self) -> list[Target]:
        return load_enabled_targets(
            self._connector.read_values(self._spreadsheet_id, "Targets!A:I")
        )

    def _states(self) -> dict[str, tuple[int, State]]:
        return load_states(
            self._connector.read_values(self._spreadsheet_id, "State!A:H")
        )

    def get_state(self, target_id: str) -> State | None:
        record = self._states().get(target_id)
        return record[1] if record else None

    def replace_state(self, state: State) -> None:
        existing = self._states().get(state.target_id)
        payload = (
            replace_state_payload(existing[0], state)
            if existing
            else append_state_payload(state)
        )
        if existing:
            self._connector.replace_values(
                self._spreadsheet_id,
                payload["range"],
                payload["values"],
                value_input_option="RAW",
            )
        else:
            self._connector.append_values(
                self._spreadsheet_id,
                payload["range"],
                payload["values"],
                value_input_option="RAW",
            )

    def append_run(self, run: RunRecord) -> None:
        values = self._connector.read_values(self._spreadsheet_id, "Runs!A:H")
        existing = records_from_values(values, RUN_COLUMNS, "Runs", allow_empty=False)
        if any(str(record["run_id"]).strip() == run.run_id for record in existing):
            return
        payload = append_run_payload(run)
        self._connector.append_values(
            self._spreadsheet_id,
            payload["range"],
            payload["values"],
            value_input_option="RAW",
        )

    def get_notification(self, event_id: str) -> NotificationRecord | None:
        values = self._connector.read_values(self._spreadsheet_id, "Notifications!A:F")
        record = load_notifications(values).get(event_id)
        return record[1] if record else None

    def upsert_notification(self, notification: NotificationRecord) -> None:
        existing = load_notifications(
            self._connector.read_values(self._spreadsheet_id, "Notifications!A:F")
        ).get(notification.event_id)
        payload = upsert_notification_payload(
            notification, existing[0] if existing else None
        )
        operation: Callable[..., None] = (
            self._connector.replace_values
            if payload["mode"] == "replace"
            else self._connector.append_values
        )
        operation(
            self._spreadsheet_id,
            payload["range"],
            payload["values"],
            value_input_option="RAW",
        )
