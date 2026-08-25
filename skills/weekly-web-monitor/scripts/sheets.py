"""Pure Google Sheets parsing plus connector-backed storage operations.

The connector is injected by the Routine. This module never loads credentials or
stores a spreadsheet identifier in source control.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any, Protocol, cast

from errors import MonitorError
from models import Attempt, NotificationRecord, RunRecord, State, Target

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
NOTIFICATION_COLUMNS = (
    "event_id",
    "target_id",
    "status",
    "notified_at",
    "kind",
    "last_error",
)

_FIRST_DATA_ROW = 2


class SheetsConnector(Protocol):
    """Minimal least-privilege connector surface required by SheetsStore."""

    def read_values(
        self, spreadsheet_id: str, range_name: str
    ) -> Sequence[Sequence[Any]]:
        """Return the raw values in ``range_name``."""
        ...

    def replace_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[Any]],
        *,
        value_input_option: str,
    ) -> None:
        """Replace the values in ``range_name`` with ``values``."""
        ...

    def append_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: Sequence[Sequence[Any]],
        *,
        value_input_option: str,
    ) -> None:
        """Append ``values`` as new rows to ``range_name``."""
        ...

    def batch_replace_values(
        self,
        spreadsheet_id: str,
        data: Sequence[Mapping[str, Any]],
        *,
        value_input_option: str,
    ) -> None:
        """Atomically replace every range in ``data`` or apply none of them."""
        ...


def _normalize_table(values: Sequence[Sequence[Any]], sheet: str) -> list[list[Any]]:
    """Validate and copy ``values`` into a plain two-dimensional list.

    Returns:
        The copied table.

    Raises:
        MonitorError: If ``values`` or any of its rows is not an array.
    """
    if not isinstance(
        values, Sequence
    ) or isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
        # values ultimately originates from an untrusted Sheets API
        # response; callers may pass a non-conforming value at runtime
        # despite the declared type, so this check stays load-bearing.
        values,
        (str, bytes),
    ):
        msg = "sheet_invalid_structure"
        raise MonitorError(
            msg,
            f"{sheet}: expected a two-dimensional values array",
        )
    table: list[list[Any]] = []
    for row in values:
        if not isinstance(
            row, Sequence
        ) or isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            row, (str, bytes)
        ):
            msg = "sheet_invalid_structure"
            raise MonitorError(
                msg, f"{sheet}: every row must be an array"
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
    """Parse a sheet's values into header-keyed records.

    Returns:
        One dict per data row, keyed by header, plus a ``_row_number``
        key giving its 1-based sheet row.

    Raises:
        MonitorError: If the sheet is empty (when disallowed), its
            headers are malformed or missing required columns, or a row
            has too many values.
    """
    table = _normalize_table(values, sheet)
    if not table:
        if allow_empty:
            return []
        msg = "sheet_empty"
        raise MonitorError(msg, f"{sheet}: header row is missing")
    headers = [str(value).strip() for value in table[0]]
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        msg = "sheet_invalid_structure"
        raise MonitorError(
            msg, f"{sheet}: headers must be non-empty and unique"
        )
    missing = [column for column in required_columns if column not in headers]
    if missing:
        msg = "sheet_missing_columns"
        raise MonitorError(
            msg,
            f"{sheet}: missing required columns: {', '.join(missing)}",
            details={"missing_columns": missing},
        )
    if headers[: len(required_columns)] != list(required_columns):
        msg = "sheet_invalid_structure"
        raise MonitorError(
            msg,
            f"{sheet}: required columns must appear first, in this order: "
            f"{', '.join(required_columns)}",
            details={"required_columns": list(required_columns)},
        )
    records: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(table[1:], start=2):
        if not raw_row or all(not str(value).strip() for value in raw_row):
            continue
        if len(raw_row) > len(headers):
            msg = "sheet_invalid_row"
            raise MonitorError(
                msg, f"{sheet}: row {row_number} has too many values"
            )
        padded = raw_row + [""] * (len(headers) - len(raw_row))
        record = dict(zip(headers, padded, strict=True))
        record["_row_number"] = row_number
        records.append(record)
    return records


def _ensure_unique(
    records: Iterable[Mapping[str, Any]], key: str, sheet: str
) -> list[Mapping[str, Any]]:
    """Validate that every record's ``key`` is present and unique.

    Returns:
        ``records``, unchanged.

    Raises:
        MonitorError: If any record is missing ``key`` or duplicates a
            previous record's value.
    """
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        value = str(record.get(key, "")).strip()
        if not value:
            msg = "sheet_invalid_row"
            raise MonitorError(
                msg,
                f"{sheet}: row {record.get('_row_number', '?')} has no {key}",
            )
        if value in seen:
            msg = "sheet_duplicate_id"
            raise MonitorError(
                msg, f"{sheet}: duplicate {key}: {value}"
            )
        seen.add(value)
        result.append(record)
    return result


def load_enabled_targets(values: Sequence[Sequence[Any]]) -> list[Target]:
    """Parse and return only the enabled targets from the Targets sheet.

    Returns:
        The enabled targets.
    """
    records = _ensure_unique(
        records_from_values(values, TARGET_COLUMNS, "Targets", allow_empty=False),
        "target_id",
        "Targets",
    )
    targets = [Target.from_mapping(record) for record in records]
    return [target for target in targets if target.enabled]


def load_states(values: Sequence[Sequence[Any]]) -> dict[str, tuple[int, State]]:
    """Parse the State sheet into a mapping of target_id to (row, state).

    Returns:
        A mapping of target_id to (1-based sheet row, state).
    """
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
    """Parse the Notifications sheet into a mapping keyed by event_id.

    Returns:
        A mapping of event_id to (1-based sheet row, record).
    """
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
    """Return ``state`` as a State sheet row, in column order."""
    value = state.as_dict()
    return [value[column] for column in STATE_COLUMNS]


def run_row(run: RunRecord) -> list[Any]:
    """Return ``run`` as a Runs sheet row, in column order.

    Returns:
        The row, with ``attempts`` folded into the ``summary`` cell as
        JSON when present.
    """
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


def _parse_folded_summary(raw_summary: str) -> tuple[str, tuple[Attempt, ...]]:
    """Unfold a Runs sheet ``summary`` cell into (text, attempts).

    Returns:
        The plain summary text and any JSON-folded attempts; falls back to
        ``(raw_summary, ())`` if it is not a JSON-folded attempts payload.
    """
    try:
        parsed: object = json.loads(raw_summary)
    except json.JSONDecodeError:
        return raw_summary, ()
    if not isinstance(parsed, dict):
        return raw_summary, ()
    parsed = cast("dict[str, Any]", parsed)
    raw_attempts = parsed.get("attempts")
    if not isinstance(raw_attempts, list):
        return raw_summary, ()
    attempts = tuple(
        Attempt(
            int(item["number"]),
            str(item["result"]),
            str(item.get("error_code", "")),
        )
        for item in cast("list[Mapping[str, Any]]", raw_attempts)
    )
    return str(parsed.get("text", "")), attempts


def run_record_from_row(record: Mapping[str, Any]) -> RunRecord:
    """Parse one Runs sheet record back into a RunRecord.

    Returns:
        The parsed run record, with any JSON-folded attempts restored.
    """
    raw_summary = str(record.get("summary", ""))
    summary, attempts = (
        _parse_folded_summary(raw_summary) if raw_summary else (raw_summary, ())
    )
    return RunRecord(
        run_id=str(record["run_id"]).strip(),
        target_id=str(record["target_id"]).strip(),
        result=str(record["result"]).strip(),
        change_score=int(record["change_score"] or 0),
        summary=summary,
        error_code=str(record["error_code"]).strip(),
        started_at=str(record["started_at"]).strip(),
        finished_at=str(record["finished_at"]).strip(),
        attempts=attempts,
    )


def notification_row(notification: NotificationRecord) -> list[Any]:
    """Return ``notification`` as a Notifications sheet row, in column order."""
    value = asdict(notification)
    return [value[column] for column in NOTIFICATION_COLUMNS]


def replace_state_payload(row_number: int, state: State) -> dict[str, Any]:
    """Build the range/values payload to replace an existing State row.

    Returns:
        The connector payload.

    Raises:
        MonitorError: If ``row_number`` is below the first data row.
    """
    if row_number < _FIRST_DATA_ROW:
        msg = "sheet_invalid_row"
        raise MonitorError(msg, "State row_number must be at least 2")
    return {
        "range": f"State!A{row_number}:H{row_number}",
        "values": [state_row(state)],
    }


def append_state_payload(state: State) -> dict[str, Any]:
    """Build the range/values payload to append a new State row.

    Returns:
        The connector payload.
    """
    return {"range": "State!A:H", "values": [state_row(state)]}


def append_run_payload(run: RunRecord) -> dict[str, Any]:
    """Build the range/values payload to append a new Runs row.

    Returns:
        The connector payload.
    """
    return {"range": "Runs!A:H", "values": [run_row(run)]}


def upsert_notification_payload(
    notification: NotificationRecord, row_number: int | None = None
) -> dict[str, Any]:
    """Build the payload to append or replace one Notifications row.

    Returns:
        The connector payload, appending if ``row_number`` is ``None`` and
        replacing otherwise.

    Raises:
        MonitorError: If ``row_number`` is below the first data row.
    """
    row = notification_row(notification)
    if row_number is None:
        return {"range": "Notifications!A:F", "values": [row], "mode": "append"}
    if row_number < _FIRST_DATA_ROW:
        msg = "sheet_invalid_row"
        raise MonitorError(
            msg, "Notifications row_number must be at least 2"
        )
    return {
        "range": f"Notifications!A{row_number}:F{row_number}",
        "values": [row],
        "mode": "replace",
    }


class SheetsStore:
    """Operational store that delegates all I/O to an injected connector."""

    def __init__(self, connector: SheetsConnector, spreadsheet_id: str) -> None:
        """Bind this store to a Sheets connector and spreadsheet.

        Raises:
            MonitorError: If ``spreadsheet_id`` is empty.
        """
        if not spreadsheet_id:
            msg = "connector_configuration_missing"
            raise MonitorError(
                msg,
                "spreadsheet_id must be supplied at runtime",
            )
        self._connector = connector
        self._spreadsheet_id = spreadsheet_id

    def load_enabled_targets(self) -> list[Target]:
        """Read and return the enabled targets from the Targets sheet.

        Returns:
            The enabled targets.
        """
        return load_enabled_targets(
            self._connector.read_values(self._spreadsheet_id, "Targets!A:I")
        )

    def _states(self) -> dict[str, tuple[int, State]]:
        """Reload every state from the sheet, keyed by target ID.

        Returns:
            A mapping of target_id to (1-based sheet row, state).
        """
        return load_states(
            self._connector.read_values(self._spreadsheet_id, "State!A:H")
        )

    def get_state(self, target_id: str) -> State | None:
        """Return the stored state for ``target_id``, if any."""
        record = self._states().get(target_id)
        return record[1] if record else None

    def replace_state(self, state: State) -> None:
        """Insert or replace the stored state for its target, via a RAW write."""
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

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the stored run record for ``run_id``, if any."""
        values = self._connector.read_values(self._spreadsheet_id, "Runs!A:H")
        records = records_from_values(values, RUN_COLUMNS, "Runs", allow_empty=False)
        for record in records:
            if str(record["run_id"]).strip() == run_id:
                return run_record_from_row(record)
        return None

    def append_run(self, run: RunRecord) -> None:
        """Idempotently append a run record, keyed by run_id."""
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
        """Return the stored notification record for ``event_id``, if any."""
        values = self._connector.read_values(self._spreadsheet_id, "Notifications!A:F")
        record = load_notifications(values).get(event_id)
        return record[1] if record else None

    def upsert_notification(self, notification: NotificationRecord) -> None:
        """Insert or update a single notification record, via a RAW write."""
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

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None:
        """Insert or update all ``notifications`` as a single atomic batch.

        Raises:
            MonitorError: If ``notifications`` contains a duplicate event_id.
        """
        if not notifications:
            return
        event_ids = [item.event_id for item in notifications]
        if len(event_ids) != len(set(event_ids)):
            msg = "notification_invalid"
            raise MonitorError(
                msg, "notification batch contains duplicate IDs"
            )
        existing = load_notifications(
            self._connector.read_values(self._spreadsheet_id, "Notifications!A:F")
        )
        next_row = max((row for row, _ in existing.values()), default=1) + 1
        data: list[dict[str, Any]] = []
        for notification in notifications:
            current = existing.get(notification.event_id)
            if current:
                row_number = current[0]
            else:
                row_number = next_row
                next_row += 1
            payload = upsert_notification_payload(notification, row_number)
            data.append({"range": payload["range"], "values": payload["values"]})
        self._connector.batch_replace_values(
            self._spreadsheet_id,
            data,
            value_input_option="RAW",
        )
