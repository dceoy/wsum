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


class NotificationStore(Protocol):
    def get_notification(self, event_id: str) -> NotificationRecord | None: ...

    def upsert_notification(self, notification: NotificationRecord) -> None: ...

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None: ...


class SlackConnector(Protocol):
    """Destination mapping and credentials remain inside this connector."""

    def send_message(self, notification_group: str, message: str) -> str: ...


class ConfirmedDeliveryFailure(MonitorError):
    """The connector confirms that Slack did not accept the message."""


class AmbiguousDeliveryFailure(MonitorError):
    """The connector cannot determine whether Slack accepted the message."""


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    target: Target
    normalized_hash: str
    summary_ja: str
    recommended_action_ja: str
    notification_text_ja: str


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    event_id: str
    status: str
    delivery_ref: str = ""
    error_code: str = ""


def change_event_id(target_id: str, normalized_hash: str) -> str:
    validate_target_id(target_id)
    if not HASH_RE.fullmatch(normalized_hash):
        msg = "notification_invalid"
        raise MonitorError(
            msg, "normalized_hash must be a SHA-256 digest"
        )
    return hashlib.sha256(f"{target_id}{normalized_hash}".encode()).hexdigest()


def failure_event_id(target_id: str, year_week: str, threshold: int) -> str:
    validate_target_id(target_id)
    if not re.fullmatch(r"\d{4}-W\d{2}", year_week) or not 1 <= threshold <= 100:
        msg = "notification_invalid"
        raise MonitorError(msg, "failure event inputs are invalid")
    material = f"failure{target_id}{year_week}{threshold}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def escape_slack_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_change_event(
    target: Target, normalized_hash: str, summary: Mapping[str, object]
) -> NotificationEvent:
    if summary.get("material") is not True:
        msg = "notification_suppressed"
        raise MonitorError(
            msg, "only validated material summaries may notify"
        )
    notification_text = str(summary.get("notification_text_ja", ""))
    if target.url not in notification_text:
        msg = "notification_invalid"
        raise MonitorError(
            msg, "notification text must contain the source URL"
        )
    return NotificationEvent(
        event_id=change_event_id(target.target_id, normalized_hash),
        target=target,
        normalized_hash=normalized_hash,
        summary_ja=str(summary.get("summary_ja", "")),
        recommended_action_ja=str(summary.get("recommended_action_ja", "")),
        notification_text_ja=notification_text,
    )


def _format_group(events: Sequence[NotificationEvent], maximum: int) -> str:
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


def deliver_grouped(
    events: Sequence[NotificationEvent],
    *,
    store: NotificationStore,
    connector: SlackConnector,
    max_message_chars: int = 3_500,
) -> dict[str, DeliveryOutcome]:
    if not 500 <= max_message_chars <= 10_000:
        msg = "invalid_configuration"
        raise MonitorError(
            msg, "Slack message length limit is invalid"
        )
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
            try:
                message = _format_group(chunk, max_message_chars)
                delivery_ref = connector.send_message(group, message)
                if not delivery_ref:
                    msg = "notification_send_failed"
                    raise AmbiguousDeliveryFailure(
                        msg,
                        "Slack connector returned no delivery reference",
                    )
            except ConfirmedDeliveryFailure as exc:
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
                for event in chunk:
                    outcomes[event.event_id] = DeliveryOutcome(
                        event.event_id, "failed", error_code=code
                    )
                continue
            except Exception:
                # Unknown connector failures are ambiguous. Preserve the pending
                # state and require operator resolution rather than duplicating.
                for event in chunk:
                    outcomes[event.event_id] = DeliveryOutcome(
                        event.event_id,
                        "pending",
                        error_code="delivery_ambiguous",
                    )
                continue
            now = utc_now()
            sent_records = [
                NotificationRecord(
                    event.event_id,
                    event.target.target_id,
                    "sent",
                    notified_at=now,
                    kind="change",
                )
                for event in chunk
            ]
            try:
                store.upsert_notifications_atomically(sent_records)
            except Exception:
                # Slack accepted the complete chunk, but no per-event sent state
                # was committed. The store's atomic batch contract guarantees
                # that every event remains pending instead of leaving a partial
                # group that cannot be reconciled consistently.
                for event in chunk:
                    outcomes[event.event_id] = DeliveryOutcome(
                        event.event_id,
                        "pending",
                        delivery_ref=delivery_ref,
                        error_code="delivery_ambiguous",
                    )
                continue
            for event in chunk:
                outcomes[event.event_id] = DeliveryOutcome(
                    event.event_id, "sent", delivery_ref=delivery_ref
                )
    return outcomes
