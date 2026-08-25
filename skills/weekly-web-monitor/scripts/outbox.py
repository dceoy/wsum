"""Optional GAS Outbox records and a deterministic local dispatcher harness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from errors import MonitorError
from models import HASH_RE, utc_now, validate_target_id, validate_timestamp
from sheets import SheetsConnector, records_from_values

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

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

_MIN_ATTEMPT_COUNT = 0
_MAX_ATTEMPT_COUNT = 100
_MAX_PAYLOAD_LENGTH = 4_000
_MAX_LAST_ERROR_LENGTH = 200


class OutboxStore(Protocol):
    """Durable storage for the optional GAS Outbox's records."""

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        """Return the stored Outbox record for ``event_id``, if any."""
        ...

    def upsert_outbox(self, record: OutboxRecord) -> None:
        """Insert or update a single Outbox record."""
        ...


class OutboxDeliveryError(Exception):
    """A sender-confirmed non-delivery that may be retried safely."""

    def __init__(self, *, retryable: bool) -> None:
        """Record whether the confirmed non-delivery may be retried."""
        super().__init__("confirmed outbox delivery failure")
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A single row of the optional GAS Outbox sheet."""

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
        """Validate every field of the record.

        Raises:
            MonitorError: If any field is malformed or out of range.
        """
        if not HASH_RE.fullmatch(self.event_id):
            msg = "outbox_invalid"
            raise MonitorError(msg, "event_id must be a SHA-256 digest")
        validate_target_id(self.target_id)
        if self.status not in OUTBOX_STATUSES:
            msg = "outbox_invalid"
            raise MonitorError(msg, "outbox status is invalid")
        if not _MIN_ATTEMPT_COUNT <= self.attempt_count <= _MAX_ATTEMPT_COUNT:
            msg = "outbox_invalid"
            raise MonitorError(msg, "attempt_count is invalid")
        validate_timestamp(self.created_at, "created_at")
        validate_timestamp(self.updated_at, "updated_at")
        validate_timestamp(self.next_attempt_at, "next_attempt_at", allow_empty=True)
        if (
            len(self.payload) > _MAX_PAYLOAD_LENGTH
            or len(self.last_error) > _MAX_LAST_ERROR_LENGTH
        ):
            msg = "outbox_invalid"
            raise MonitorError(msg, "outbox payload or error is too long")

    def as_row(self) -> list[Any]:
        """Return this record's fields as a Sheets row, in column order."""
        value = asdict(self)
        return [value[column] for column in OUTBOX_COLUMNS]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OutboxRecord:
        """Build a validated record from a raw Sheets row mapping.

        Returns:
            The constructed, validated Outbox record.

        Raises:
            MonitorError: If ``attempt_count`` is not an integer, or any
                field fails validation.
        """
        try:
            attempt_count = int(value.get("attempt_count", 0))
        except (TypeError, ValueError) as exc:
            msg = "outbox_invalid"
            raise MonitorError(msg, "attempt_count must be an integer") from exc
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
    """Parse raw Outbox sheet rows into records keyed by event ID.

    Returns:
        A mapping of event ID to (1-based sheet row number, record).

    Raises:
        MonitorError: If the sheet is malformed or contains a duplicate
            event_id.
    """
    records = records_from_values(values, OUTBOX_COLUMNS, "Outbox", allow_empty=False)
    result: dict[str, tuple[int, OutboxRecord]] = {}
    for value in records:
        record = OutboxRecord.from_mapping(value)
        if record.event_id in result:
            msg = "sheet_duplicate_id"
            raise MonitorError(
                msg,
                f"Outbox: duplicate event_id: {record.event_id}",
            )
        result[record.event_id] = (int(value["_row_number"]), record)
    return result


class OutboxSheetsStore:
    """RAW-value Google Sheets adapter for the optional Outbox."""

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

    def _records(self) -> dict[str, tuple[int, OutboxRecord]]:
        """Reload every Outbox record from the sheet, keyed by event ID.

        Returns:
            A mapping of event ID to (1-based sheet row number, record).
        """
        return load_outbox(
            self._connector.read_values(self._spreadsheet_id, "Outbox!A:I")
        )

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        """Return the stored Outbox record for ``event_id``, if any."""
        value = self._records().get(event_id)
        return value[1] if value else None

    def upsert_outbox(self, record: OutboxRecord) -> None:
        """Insert or update a single Outbox record via a RAW-value write."""
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
    """Build a new pending Outbox record for a Slack notification.

    Returns:
        The constructed pending Outbox record.

    Raises:
        MonitorError: If ``notification_group`` is invalid or ``message``
            contains a webhook URL.
    """
    validate_target_id(notification_group)
    if "hooks.slack.com/services/" in message.lower():
        msg = "outbox_invalid"
        raise MonitorError(msg, "Outbox message must not contain a webhook URL")
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


def _parse_outbox_payload(payload: str) -> tuple[str, str]:
    """Parse an Outbox record's JSON payload into (group, message).

    Returns:
        The notification group and message text.

    Raises:
        ValueError: If the payload's shape is wrong.
        TypeError: If ``notification_group`` or ``message`` is not a string.
    """
    parsed = json.loads(payload)
    if set(parsed) != {"notification_group", "message"}:
        msg = "outbox payload must contain exactly notification_group and message"
        raise ValueError(msg)
    group = parsed["notification_group"]
    message = parsed["message"]
    if not isinstance(group, str) or not isinstance(message, str):
        msg = "outbox payload fields must be strings"
        raise TypeError(msg)
    return group, message


def _send_outbox_message(
    sender: Callable[[str, str], str], group: str, message: str
) -> str:
    """Send one Outbox message and return its delivery reference.

    Returns:
        The sender's delivery reference.

    Raises:
        MonitorError: If the sender accepted the send but returned no
            delivery reference.
    """
    delivery_ref = sender(group, message)
    if not delivery_ref:
        msg = "notification_send_failed"
        raise MonitorError(msg, "sender returned no delivery reference")
    return delivery_ref


def dispatch_record(
    record: OutboxRecord,
    sender: Callable[[str, str], str],
    *,
    persist_transition: Callable[[OutboxRecord], None],
    max_attempts: int = 5,
    now: str | None = None,
) -> OutboxRecord:
    """Dispatch one pending/retry Outbox record, transitioning its status.

    Returns:
        The record's next state: unchanged if already terminal/in-flight,
        ``poison`` if the payload is malformed or retries are exhausted,
        ``retry`` if a confirmed retryable failure occurred, ``sending`` if
        the delivery outcome is ambiguous, or ``sent`` on success.
    """
    if record.status in {"sent", "sending", "poison"}:
        return record
    timestamp = now or utc_now()
    try:
        group, message = _parse_outbox_payload(record.payload)
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
        _send_outbox_message(sender, group, message)
    except OutboxDeliveryError as exc:
        attempts = sending.attempt_count
        if attempts >= max_attempts or not exc.retryable:
            status = "poison"
            next_attempt = ""
        else:
            status = "retry"
            delay = min(60, 2**attempts)
            parsed = datetime.fromisoformat(timestamp)
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
    except Exception:  # ruff: ignore[blind-except] -- any sender failure is ambiguous by design:
        # `sender` is a caller-supplied boundary whose failure modes cannot be
        # enumerated. The external side effect may have happened, so keep the
        # record in the non-retryable ambiguous state instead of risking
        # duplicate delivery.
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
