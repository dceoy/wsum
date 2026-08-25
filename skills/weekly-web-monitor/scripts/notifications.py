"""Idempotent grouped Slack notification planning and delivery."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from errors import MonitorError
from models import HASH_RE, NotificationRecord, Target, utc_now, validate_target_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 100
_MIN_MAX_MESSAGE_CHARS = 500
_MAX_MAX_MESSAGE_CHARS = 10_000


class NotificationStore(Protocol):
    """Durable storage for notification dedup and delivery state."""

    def get_notification(self, event_id: str) -> NotificationRecord | None:
        """Return the stored record for ``event_id``, if any."""
        ...

    def upsert_notification(self, notification: NotificationRecord) -> None:
        """Insert or update a single notification record."""
        ...

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None:
        """Insert or update all ``notifications`` as a single atomic batch."""
        ...


class SlackConnector(Protocol):
    """Destination mapping and credentials remain inside this connector."""

    def send_message(self, notification_group: str, message: str) -> str:
        """Send ``message`` to ``notification_group`` and return a delivery ref."""
        ...


class ConfirmedDeliveryFailureError(MonitorError):
    """The connector confirms that Slack did not accept the message."""


class AmbiguousDeliveryFailureError(MonitorError):
    """The connector cannot determine whether Slack accepted the message."""


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A validated, ready-to-send change notification."""

    event_id: str
    target: Target
    normalized_hash: str
    summary_ja: str
    recommended_action_ja: str
    notification_text_ja: str


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """The result of attempting to deliver one :class:`NotificationEvent`."""

    event_id: str
    status: str
    delivery_ref: str = ""
    error_code: str = ""


def change_event_id(target_id: str, normalized_hash: str) -> str:
    """Return the stable dedup ID for a change notification.

    Returns:
        A SHA-256 hex digest derived from ``target_id`` and ``normalized_hash``.

    Raises:
        MonitorError: If ``target_id`` or ``normalized_hash`` is invalid.
    """
    validate_target_id(target_id)
    if not HASH_RE.fullmatch(normalized_hash):
        msg = "notification_invalid"
        raise MonitorError(msg, "normalized_hash must be a SHA-256 digest")
    return hashlib.sha256(f"{target_id}{normalized_hash}".encode()).hexdigest()


def failure_event_id(target_id: str, year_week: str, threshold: int) -> str:
    """Return the stable dedup ID for a weekly failure-threshold notification.

    Returns:
        A SHA-256 hex digest derived from ``target_id``, ``year_week``, and
        ``threshold``.

    Raises:
        MonitorError: If ``target_id``, ``year_week``, or ``threshold`` is
            invalid.
    """
    validate_target_id(target_id)
    if (
        not re.fullmatch(r"\d{4}-W\d{2}", year_week)
        or not _MIN_THRESHOLD <= threshold <= _MAX_THRESHOLD
    ):
        msg = "notification_invalid"
        raise MonitorError(msg, "failure event inputs are invalid")
    material = f"failure{target_id}{year_week}{threshold}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def escape_slack_text(value: str) -> str:
    """Escape Slack markup metacharacters (``&``, ``<``, ``>``) in ``value``.

    Returns:
        ``value`` with Slack's markup metacharacters HTML-escaped.
    """
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_change_event(
    target: Target, normalized_hash: str, summary: Mapping[str, object]
) -> NotificationEvent:
    """Build a validated :class:`NotificationEvent` from a model summary.

    Returns:
        The constructed change notification event.

    Raises:
        MonitorError: If ``summary`` was not marked material, or its
            notification text does not contain the target's source URL.
    """
    if summary.get("material") is not True:
        msg = "notification_suppressed"
        raise MonitorError(msg, "only validated material summaries may notify")
    notification_text = str(summary.get("notification_text_ja", ""))
    if target.url not in notification_text:
        msg = "notification_invalid"
        raise MonitorError(msg, "notification text must contain the source URL")
    return NotificationEvent(
        event_id=change_event_id(target.target_id, normalized_hash),
        target=target,
        normalized_hash=normalized_hash,
        summary_ja=str(summary.get("summary_ja", "")),
        recommended_action_ja=str(summary.get("recommended_action_ja", "")),
        notification_text_ja=notification_text,
    )


def _format_group(events: Sequence[NotificationEvent], maximum: int) -> str:
    """Render a grouped Slack message for ``events``.

    Returns:
        The formatted message text.

    Raises:
        MonitorError: If the rendered message would exceed ``maximum``
            characters.
    """
    parts = ["*Webサイト更新のお知らせ*"]
    for event in events:
        safe_name = escape_slack_text(event.target.name)
        item = f"\n*{safe_name}*\n{event.notification_text_ja}"
        if event.recommended_action_ja:
            item += f"\n推奨対応: {event.recommended_action_ja}"
        candidate = "\n".join((*parts, item))
        if len(candidate) > maximum:
            msg = "notification_too_long"
            raise MonitorError(
                msg,
                "grouped notification exceeds the configured length limit",
            )
        parts.append(item)
    return "\n".join(parts)


def _chunk_group(
    events: Sequence[NotificationEvent], maximum: int
) -> tuple[list[list[NotificationEvent]], list[NotificationEvent]]:
    """Split ``events`` into Slack-sized chunks, separating oversized events.

    Returns:
        A tuple of (chunks that fit within ``maximum`` characters, events
        that are individually too large to ever fit).
    """
    chunks: list[list[NotificationEvent]] = []
    oversized: list[NotificationEvent] = []
    current: list[NotificationEvent] = []
    for event in events:
        try:
            _format_group([*current, event], maximum)
        except MonitorError:
            if current:
                chunks.append(current)
                current = []
            try:
                _format_group([event], maximum)
            except MonitorError:
                oversized.append(event)
            else:
                current = [event]
        else:
            current.append(event)
    if current:
        chunks.append(current)
    return chunks, oversized


def _partition_by_dedup_status(
    events: Sequence[NotificationEvent], store: NotificationStore
) -> tuple[dict[str, DeliveryOutcome], dict[str, list[NotificationEvent]]]:
    """Split ``events`` into already-resolved outcomes and events left to send.

    Returns:
        A tuple of (outcomes for events already resolved by dedup state,
        events still to send, grouped by notification group).
    """
    outcomes: dict[str, DeliveryOutcome] = {}
    sendable: dict[str, list[NotificationEvent]] = {}
    pending_records: list[NotificationRecord] = []
    for event in events:
        existing = store.get_notification(event.event_id)
        if existing and existing.status == "sent":
            outcomes[event.event_id] = DeliveryOutcome(event.event_id, "suppressed")
            continue
        if existing and existing.status == "suppressed":
            # An operator-suppressed record means this event was never sent
            # and never will be, unlike the "sent" dedup case above. Tag it
            # distinctly so callers do not count it as a delivered notification.
            outcomes[event.event_id] = DeliveryOutcome(
                event.event_id, "suppressed", error_code="operator_suppressed"
            )
            continue
        if existing and existing.status == "pending":
            # A pending record may mean delivery succeeded before the final state
            # update. Fail closed instead of risking a duplicate message.
            outcomes[event.event_id] = DeliveryOutcome(
                event.event_id, "pending", error_code="delivery_ambiguous"
            )
            continue
        pending_records.append(
            NotificationRecord(
                event.event_id,
                event.target.target_id,
                "pending",
                kind="change",
            )
        )
        sendable.setdefault(event.target.notification_group, []).append(event)
    if pending_records:
        store.upsert_notifications_atomically(pending_records)
    return outcomes, sendable


def _send_chunk(
    group: str, chunk: list[NotificationEvent], connector: SlackConnector, maximum: int
) -> str:
    """Format and send one chunk, returning its delivery reference.

    Returns:
        The connector's delivery reference for the sent chunk.

    Raises:
        AmbiguousDeliveryFailureError: If the connector accepted the send
            but returned no delivery reference.
    """
    message = _format_group(chunk, maximum)
    delivery_ref = connector.send_message(group, message)
    if not delivery_ref:
        msg = "notification_send_failed"
        raise AmbiguousDeliveryFailureError(
            msg,
            "Slack connector returned no delivery reference",
        )
    return delivery_ref


def _deliver_chunk(
    group: str,
    chunk: list[NotificationEvent],
    *,
    store: NotificationStore,
    connector: SlackConnector,
    max_message_chars: int,
) -> dict[str, DeliveryOutcome]:
    """Send one chunk of events and record the resulting delivery outcomes.

    Returns:
        The delivery outcome for every event in ``chunk``.
    """
    try:
        delivery_ref = _send_chunk(group, chunk, connector, max_message_chars)
    except ConfirmedDeliveryFailureError as exc:
        code = exc.code
        failed_records = [
            NotificationRecord(
                event.event_id,
                event.target.target_id,
                "failed",
                kind="change",
                last_error=code,
            )
            for event in chunk
        ]
        store.upsert_notifications_atomically(failed_records)
        return {
            event.event_id: DeliveryOutcome(event.event_id, "failed", error_code=code)
            for event in chunk
        }
    except Exception:  # ruff: ignore[blind-except] -- any connector failure is ambiguous by
        # design: SlackConnector is a third-party-implemented boundary whose
        # failure modes cannot be enumerated. Preserve the pending state and
        # require operator resolution rather than risk duplicating a send.
        return {
            event.event_id: DeliveryOutcome(
                event.event_id,
                "pending",
                error_code="delivery_ambiguous",
            )
            for event in chunk
        }
    sent_records = [
        NotificationRecord(
            event.event_id,
            event.target.target_id,
            "sent",
            notified_at=utc_now(),
            kind="change",
        )
        for event in chunk
    ]
    try:
        store.upsert_notifications_atomically(sent_records)
    except Exception:  # ruff: ignore[blind-except] -- any store failure here is ambiguous by
        # design: NotificationStore is a third-party-implemented boundary.
        # Slack accepted the complete chunk, but no per-event sent state
        # was committed. The store's atomic batch contract guarantees
        # that every event remains pending instead of leaving a partial
        # group that cannot be reconciled consistently.
        return {
            event.event_id: DeliveryOutcome(
                event.event_id,
                "pending",
                delivery_ref=delivery_ref,
                error_code="delivery_ambiguous",
            )
            for event in chunk
        }
    return {
        event.event_id: DeliveryOutcome(
            event.event_id, "sent", delivery_ref=delivery_ref
        )
        for event in chunk
    }


def deliver_grouped(
    events: Sequence[NotificationEvent],
    *,
    store: NotificationStore,
    connector: SlackConnector,
    max_message_chars: int = 3_500,
) -> dict[str, DeliveryOutcome]:
    """Deduplicate, group, and deliver change notifications over Slack.

    Returns:
        The per-event delivery outcome, keyed by event ID.

    Raises:
        MonitorError: If ``max_message_chars`` is outside the allowed range.
    """
    if not _MIN_MAX_MESSAGE_CHARS <= max_message_chars <= _MAX_MAX_MESSAGE_CHARS:
        msg = "invalid_configuration"
        raise MonitorError(msg, "Slack message length limit is invalid")
    outcomes, sendable = _partition_by_dedup_status(events, store)

    for group, group_events in sendable.items():
        chunks, oversized = _chunk_group(group_events, max_message_chars)
        failed_oversized = [
            NotificationRecord(
                event.event_id,
                event.target.target_id,
                "failed",
                kind="change",
                last_error="notification_too_long",
            )
            for event in oversized
        ]
        if failed_oversized:
            store.upsert_notifications_atomically(failed_oversized)
        for event in oversized:
            outcomes[event.event_id] = DeliveryOutcome(
                event.event_id, "failed", error_code="notification_too_long"
            )
        for chunk in chunks:
            outcomes.update(
                _deliver_chunk(
                    group,
                    chunk,
                    store=store,
                    connector=connector,
                    max_message_chars=max_message_chars,
                )
            )
    return outcomes
