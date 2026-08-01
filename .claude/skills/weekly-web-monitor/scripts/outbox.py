"""Optional GAS Outbox records and a deterministic local dispatcher harness."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from errors import MonitorError
from models import HASH_RE, utc_now, validate_target_id, validate_timestamp
from sheets import SheetsConnector, records_from_values

OUTBOX_COLUMNS = (
    "event_id",
    "target_id",
    "payload",
    "status",
    "attempt_count",
    "created_at",
    "updated_at",
    "next_attempt_at",
    "last_error",
)
OUTBOX_STATUSES = frozenset({"pending", "sending", "sent", "retry", "poison"})


class OutboxStore(Protocol):
    def get_outbox(self, event_id: str) -> OutboxRecord | None: ...

    def upsert_outbox(self, record: OutboxRecord) -> None: ...


class OutboxDeliveryError(Exception):
    """A sender-confirmed non-delivery that may be retried safely."""

    def __init__(self, *, retryable: bool) -> None:
        super().__init__("confirmed outbox delivery failure")
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: str
    target_id: str
    payload: str
    status: str
    attempt_count: int
    created_at: str
    updated_at: str
    next_attempt_at: str = ""
    last_error: str = ""

    def __post_init__(self) -> None:
        if not HASH_RE.fullmatch(self.event_id):
            raise MonitorError("outbox_invalid", "event_id must be a SHA-256 digest")
        validate_target_id(self.target_id)
        if self.status not in OUTBOX_STATUSES:
            raise MonitorError("outbox_invalid", "outbox status is invalid")
        if not 0 <= self.attempt_count <= 100:
            raise MonitorError("outbox_invalid", "attempt_count is invalid")
        validate_timestamp(self.created_at, "created_at")
        validate_timestamp(self.updated_at, "updated_at")
        validate_timestamp(self.next_attempt_at, "next_attempt_at", allow_empty=True)
        if len(self.payload) > 4_000 or len(self.last_error) > 200:
            raise MonitorError("outbox_invalid", "outbox payload or error is too long")

    def as_row(self) -> list[Any]:
        value = asdict(self)
        return [value[column] for column in OUTBOX_COLUMNS]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OutboxRecord:
        try:
            attempt_count = int(value.get("attempt_count", 0))
        except (TypeError, ValueError) as exc:
            raise MonitorError(
                "outbox_invalid", "attempt_count must be an integer"
            ) from exc
        return cls(
            event_id=str(value.get("event_id", "")).strip(),
            target_id=str(value.get("target_id", "")).strip(),
            payload=str(value.get("payload", "")),
            status=str(value.get("status", "")).strip(),
            attempt_count=attempt_count,
            created_at=str(value.get("created_at", "")).strip(),
            updated_at=str(value.get("updated_at", "")).strip(),
            next_attempt_at=str(value.get("next_attempt_at", "")).strip(),
            last_error=str(value.get("last_error", "")).strip(),
        )


def load_outbox(
    values: Sequence[Sequence[Any]],
) -> dict[str, tuple[int, OutboxRecord]]:
    records = records_from_values(values, OUTBOX_COLUMNS, "Outbox", allow_empty=False)
    result: dict[str, tuple[int, OutboxRecord]] = {}
    for value in records:
        record = OutboxRecord.from_mapping(value)
        if record.event_id in result:
            raise MonitorError(
                "sheet_duplicate_id",
                f"Outbox: duplicate event_id: {record.event_id}",
            )
        result[record.event_id] = (int(value["_row_number"]), record)
    return result


class OutboxSheetsStore:
    """RAW-value Google Sheets adapter for the optional Outbox."""

    def __init__(self, connector: SheetsConnector, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise MonitorError(
                "connector_configuration_missing",
                "spreadsheet_id must be supplied at runtime",
            )
        self._connector = connector
        self._spreadsheet_id = spreadsheet_id

    def _records(self) -> dict[str, tuple[int, OutboxRecord]]:
        return load_outbox(
            self._connector.read_values(self._spreadsheet_id, "Outbox!A:I")
        )

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        value = self._records().get(event_id)
        return value[1] if value else None

    def upsert_outbox(self, record: OutboxRecord) -> None:
        existing = self._records().get(record.event_id)
        values = [record.as_row()]
        if existing:
            row = existing[0]
            self._connector.replace_values(
                self._spreadsheet_id,
                f"Outbox!A{row}:I{row}",
                values,
                value_input_option="RAW",
            )
        else:
            self._connector.append_values(
                self._spreadsheet_id,
                "Outbox!A:I",
                values,
                value_input_option="RAW",
            )


def enqueue_record(
    event_id: str,
    target_id: str,
    notification_group: str,
    message: str,
    *,
    now: str | None = None,
) -> OutboxRecord:
    validate_target_id(notification_group)
    if "hooks.slack.com/services/" in message.lower():
        raise MonitorError(
            "outbox_invalid", "Outbox message must not contain a webhook URL"
        )
    timestamp = now or utc_now()
    payload = json.dumps(
        {"notification_group": notification_group, "message": message},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return OutboxRecord(
        event_id,
        target_id,
        payload,
        "pending",
        0,
        timestamp,
        timestamp,
    )


def dispatch_record(
    record: OutboxRecord,
    sender: Callable[[str, str], str],
    *,
    persist_transition: Callable[[OutboxRecord], None],
    max_attempts: int = 5,
    now: str | None = None,
) -> OutboxRecord:
    if record.status in {"sent", "sending", "poison"}:
        return record
    timestamp = now or utc_now()
    try:
        payload = json.loads(record.payload)
        if set(payload) != {"notification_group", "message"}:
            raise ValueError
        group = payload["notification_group"]
        message = payload["message"]
        if not isinstance(group, str) or not isinstance(message, str):
            raise ValueError
    except (json.JSONDecodeError, ValueError, TypeError):
        return OutboxRecord(
            record.event_id,
            record.target_id,
            record.payload,
            "poison",
            record.attempt_count,
            record.created_at,
            timestamp,
            last_error="outbox_payload_invalid",
        )
    # Callers must persist this sending transition before invoking the sender.
    sending = OutboxRecord(
        record.event_id,
        record.target_id,
        record.payload,
        "sending",
        record.attempt_count + 1,
        record.created_at,
        timestamp,
    )
    persist_transition(sending)
    try:
        delivery_ref = sender(group, message)
        if not delivery_ref:
            raise MonitorError(
                "notification_send_failed", "sender returned no delivery reference"
            )
    except OutboxDeliveryError as exc:
        attempts = sending.attempt_count
        if attempts >= max_attempts or not exc.retryable:
            status = "poison"
            next_attempt = ""
        else:
            status = "retry"
            delay = min(60, 2**attempts)
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            next_attempt = (parsed + timedelta(minutes=delay)).astimezone(UTC)
            next_attempt = next_attempt.isoformat().replace("+00:00", "Z")
        return OutboxRecord(
            record.event_id,
            record.target_id,
            record.payload,
            status,
            attempts,
            record.created_at,
            timestamp,
            next_attempt_at=next_attempt,
            last_error="notification_send_failed",
        )
    except Exception:
        # The external side effect may have happened. Keep the record in the
        # non-retryable ambiguous state instead of risking duplicate delivery.
        return OutboxRecord(
            record.event_id,
            record.target_id,
            record.payload,
            "sending",
            sending.attempt_count,
            record.created_at,
            timestamp,
            last_error="delivery_ambiguous",
        )
    return OutboxRecord(
        record.event_id,
        record.target_id,
        record.payload,
        "sent",
        sending.attempt_count,
        record.created_at,
        timestamp,
    )
