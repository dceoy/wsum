"""Deterministic workflow decisions for the web update monitor skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TypedDict, cast
from urllib.parse import urlsplit

import monitor

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERSISTENCE_MODES = {"local", "google-drive"}
_FETCH_MODES = {"static", "browser"}
_NOTIFICATION_STATUSES = {"pending", "sending", "delivered"}
_NOTIFICATION_PROTOCOL_VERSION = 2


class WorkflowError(RuntimeError):
    """Expected workflow input or state-transition failure."""


class NotificationRecord(TypedDict):
    """Validated durable notification record."""

    version: int
    event_id: str
    target_id: str
    sha256: str
    destination: str
    message: str
    status: str
    attempt: int
    last_error: str
    updated_at: str


def _require_string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        kind = "string" if allow_empty else "non-empty string"
        raise WorkflowError(f"{field} must be a {kind}")
    return value


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{field} must be an object")
    return cast("Mapping[str, object]", value)


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowError(f"{field} must be an array")
    return cast("list[object]", value)


def _validate_url(value: object) -> str:
    url = _require_string(value, "url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise WorkflowError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WorkflowError("url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise WorkflowError("url must not contain credentials")
    if parsed.fragment:
        raise WorkflowError("url must not contain a fragment")
    if monitor.url_has_credentials(url):
        raise WorkflowError("url must not contain credentials")
    return url


def validate_target(value: object) -> dict[str, object]:
    """Validate and normalize one monitoring target."""
    target = _require_mapping(value, "target")
    if target.get("include_selector") or target.get("exclude_selectors"):
        raise WorkflowError("selector_migration_required")

    target_id = _require_string(target.get("target_id"), "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    name = _require_string(target.get("name"), "name")
    url = _validate_url(target.get("url"))
    enabled = target.get("enabled")
    if not isinstance(enabled, bool):
        raise WorkflowError("enabled must be a boolean")
    watch_focus = _require_string(
        target.get("watch_focus", ""), "watch_focus", allow_empty=True
    )
    notification_group = _require_string(
        target.get("notification_group", ""),
        "notification_group",
        allow_empty=True,
    )
    fetch_mode = _require_string(target.get("fetch_mode", "static"), "fetch_mode")
    if fetch_mode not in _FETCH_MODES:
        raise WorkflowError("fetch_mode must be static or browser")

    return {
        "version": 1,
        "target_id": target_id,
        "name": name,
        "url": url,
        "enabled": enabled,
        "action": "monitor" if enabled else "skip_disabled",
        "watch_focus": watch_focus,
        "notification_group": notification_group,
        "fetch_mode": fetch_mode,
    }


def validate_targets(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one run's persistence mode and target set."""
    persistence_mode = _require_string(
        payload.get("persistence_mode"), "persistence_mode"
    )
    if persistence_mode not in _PERSISTENCE_MODES:
        raise WorkflowError("persistence_mode must be local or google-drive")
    raw_targets = _require_list(payload.get("targets"), "targets")
    targets = [validate_target(target) for target in raw_targets]
    ids = [str(target["target_id"]) for target in targets]
    if len(ids) != len(set(ids)):
        raise WorkflowError("duplicate_target_id")
    return {"persistence_mode": persistence_mode, "targets": targets}


def change_action(payload: Mapping[str, object]) -> dict[str, object]:
    """Choose the next workflow action from a monitor result and LLM judgment."""
    status = _require_string(payload.get("status"), "status")
    if status == "baseline":
        return {"action": "promote_snapshot"}
    if status == "unchanged":
        return {"action": "discard_candidate"}
    if status != "changed":
        raise WorkflowError("status must be baseline, unchanged, or changed")

    diff_truncated = payload.get("diff_truncated")
    if not isinstance(diff_truncated, bool):
        raise WorkflowError("diff_truncated must be a boolean")
    materiality = payload.get("materiality")
    if materiality is None:
        return {"action": "assess_materiality"}
    if not isinstance(materiality, bool):
        raise WorkflowError("materiality must be a boolean or null")
    if diff_truncated and not materiality:
        return {"action": "manual_review"}
    if materiality:
        return {"action": "notify"}
    return {"action": "promote_snapshot"}


def notification_event_id(target_id: str, sha256: str) -> str:
    """Return the stable event ID for one target and normalized snapshot hash."""
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    if not _SHA256_RE.fullmatch(sha256):
        raise WorkflowError("sha256 must be a lowercase hexadecimal SHA-256")
    return hashlib.sha256(f"{target_id}\0{sha256}".encode()).hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _notification_context(
    payload: Mapping[str, object],
) -> tuple[str, str, str, str, str]:
    target_id = _require_string(payload.get("target_id"), "target_id")
    sha256 = _require_string(payload.get("sha256"), "sha256")
    event_id = notification_event_id(target_id, sha256)
    destination = _require_string(payload.get("destination"), "destination")
    message = _require_string(payload.get("message"), "message")
    return target_id, sha256, event_id, destination, message


def _validate_notification(
    value: object,
    *,
    target_id: str,
    sha256: str,
    event_id: str,
    destination: str,
) -> NotificationRecord:
    record = _require_mapping(value, "notification")
    if record.get("version") != 1:
        raise WorkflowError("notification version must be 1")
    expected = {
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
    }
    for field, expected_value in expected.items():
        if record.get(field) != expected_value:
            raise WorkflowError(f"notification {field} does not match the event")
    message = _require_string(record.get("message"), "notification message")
    status = _require_string(record.get("status"), "notification status")
    if status not in _NOTIFICATION_STATUSES:
        raise WorkflowError("notification status is invalid")
    attempt = record.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise WorkflowError("notification attempt must be a non-negative integer")
    last_error = _require_string(
        record.get("last_error", ""), "notification last_error", allow_empty=True
    )
    updated_at = _require_string(record.get("updated_at"), "notification updated_at")
    return {
        "version": 1,
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
        "message": message,
        "status": status,
        "attempt": attempt,
        "last_error": last_error,
        "updated_at": updated_at,
    }


def _new_notification(
    *, target_id: str, sha256: str, event_id: str, destination: str, message: str
) -> NotificationRecord:
    return {
        "version": 1,
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
        "message": message,
        "status": "pending",
        "attempt": 0,
        "last_error": "",
        "updated_at": _timestamp(),
    }


def _replace_status(
    record: NotificationRecord,
    status: str,
    *,
    attempt: int | None = None,
    last_error: str | None = None,
) -> NotificationRecord:
    updated = record.copy()
    updated["status"] = status
    if attempt is not None:
        updated["attempt"] = attempt
    if last_error is not None:
        updated["last_error"] = last_error
    updated["updated_at"] = _timestamp()
    return updated


def _protocol_response(action: str, **fields: object) -> dict[str, object]:
    """Return one versioned notification protocol response."""
    response: dict[str, object] = {
        "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
        "action": action,
    }
    response.update(fields)
    return response


def _compare_and_swap_response(
    expected: NotificationRecord | None,
    replacement: NotificationRecord,
    next_signal: str,
) -> dict[str, object]:
    """Describe one exact durable notification replacement."""
    return _protocol_response(
        "compare_and_swap",
        expected_notification=expected,
        notification=replacement,
        next_signal=next_signal,
    )


def notification_step(payload: Mapping[str, object]) -> dict[str, object]:
    """Advance the durable notification protocol by one verified step."""
    target_id, sha256, event_id, destination, message = _notification_context(payload)
    signal = _require_string(payload.get("signal", "start"), "signal")
    raw_record = payload.get("notification")
    record = (
        None
        if raw_record is None
        else _validate_notification(
            raw_record,
            target_id=target_id,
            sha256=sha256,
            event_id=event_id,
            destination=destination,
        )
    )

    if signal == "start":
        if record is None:
            return _compare_and_swap_response(
                None,
                _new_notification(
                    target_id=target_id,
                    sha256=sha256,
                    event_id=event_id,
                    destination=destination,
                    message=message,
                ),
                "pending_persisted",
            )
        status = record["status"]
        if status == "pending":
            return _compare_and_swap_response(
                record,
                _replace_status(
                    record,
                    "sending",
                    attempt=record["attempt"] + 1,
                    last_error="",
                ),
                "sending_claimed",
            )
        if status == "sending":
            return _protocol_response("manual_reconciliation")
        return _protocol_response("promote_snapshot")

    if record is None:
        raise WorkflowError("notification is required after start")
    status = record["status"]
    if signal == "pending_persisted":
        if status != "pending":
            raise WorkflowError("pending_persisted requires a pending notification")
        return _compare_and_swap_response(
            record,
            _replace_status(
                record,
                "sending",
                attempt=record["attempt"] + 1,
                last_error="",
            ),
            "sending_claimed",
        )
    if signal == "sending_claimed":
        if status != "sending":
            raise WorkflowError("sending_claimed requires a sending notification")
        return _protocol_response("send_slack", notification=record)
    if signal == "slack_delivered":
        if status != "sending":
            raise WorkflowError("slack_delivered requires a sending notification")
        return _compare_and_swap_response(
            record,
            _replace_status(record, "delivered", last_error=""),
            "delivered_persisted",
        )
    if signal == "slack_failed":
        if status != "sending":
            raise WorkflowError("slack_failed requires a sending notification")
        error = _require_string(payload.get("error"), "error")
        return _compare_and_swap_response(
            record,
            _replace_status(record, "pending", last_error=error),
            "failure_persisted",
        )
    if signal == "delivered_persisted":
        if status != "delivered":
            raise WorkflowError("delivered_persisted requires a delivered notification")
        return _protocol_response("promote_snapshot")
    if signal == "failure_persisted":
        if status != "pending":
            raise WorkflowError("failure_persisted requires a pending notification")
        return _protocol_response("stop")
    raise WorkflowError("signal is invalid")


def _read_payload() -> dict[str, object]:
    try:
        payload: object = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise WorkflowError("stdin must contain one JSON object") from exc
    if not isinstance(payload, dict):
        raise WorkflowError("stdin must contain one JSON object")
    return cast("dict[str, object]", payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic workflow state."
    )
    parser.add_argument(
        "operation",
        choices=("validate-targets", "change-action", "notification-step"),
    )
    return parser


def run(operation: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Execute one workflow operation."""
    if operation == "validate-targets":
        return validate_targets(payload)
    if operation == "change-action":
        return change_action(payload)
    if operation == "notification-step":
        return notification_step(payload)
    raise WorkflowError("operation is invalid")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    try:
        args = _parser().parse_args(argv)
        result = run(args.operation, _read_payload())
    except (WorkflowError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
