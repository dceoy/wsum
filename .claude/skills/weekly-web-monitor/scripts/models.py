"""Validated records shared across deterministic monitor scripts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from errors import MonitorError
from network_policy import is_sensitive_query_name

TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
FETCH_MODES = frozenset({"static", "browser"})
RUN_RESULTS = frozenset(
    {
        "baseline_created",
        "failed",
        "material",
        "minor",
        "non_material",
        "notified",
        "unchanged",
    }
)
NOTIFICATION_STATUSES = frozenset({"pending", "sent", "failed", "suppressed"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_timestamp(
    value: str, field_name: str, *, allow_empty: bool = False
) -> str:
    if allow_empty and not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MonitorError(
            "invalid_record", f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise MonitorError("invalid_record", f"{field_name} must include a timezone")
    return value


def validate_http_url(value: str, field_name: str = "url") -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MonitorError(
            "invalid_record", f"{field_name} contains control characters"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise MonitorError("invalid_record", f"{field_name} is malformed") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise MonitorError(
            "invalid_record",
            f"{field_name} must be an HTTP(S) URL without embedded credentials",
        )
    if any(
        is_sensitive_query_name(name)
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise MonitorError(
            "invalid_record",
            f"{field_name} must not contain credential-like query parameters",
        )
    if parsed.fragment:
        raise MonitorError(
            "invalid_record",
            f"{field_name} must not contain a URL fragment",
        )
    host = (parsed.hostname or "").lower()
    decoded_path = unquote(parsed.path)
    if (
        host == "hooks.slack.com"
        and decoded_path.startswith("/services/")
        or host in {"discord.com", "discordapp.com"}
        and "/api/webhooks/" in decoded_path
    ):
        raise MonitorError(
            "invalid_record", f"{field_name} must not be a webhook credential URL"
        )
    return value


def validate_target_id(value: str) -> str:
    if not isinstance(value, str) or not TARGET_ID_RE.fullmatch(value):
        raise MonitorError(
            "invalid_record",
            "target_id must contain only letters, digits, dot, underscore, or hyphen",
        )
    return value


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise MonitorError("invalid_record", f"{field_name} must be a boolean")


def _parse_selectors(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        selectors = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        selectors = tuple(item.strip() for item in value if item.strip())
    else:
        raise MonitorError(
            "invalid_record",
            "exclude_selectors must be a comma-separated string or list",
        )
    if len(selectors) > 50 or any(len(item) > 500 for item in selectors):
        raise MonitorError(
            "invalid_record", "exclude_selectors exceeds the count or length limit"
        )
    return selectors


@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    enabled: bool
    name: str
    url: str
    fetch_mode: str = "static"
    include_selector: str = ""
    exclude_selectors: tuple[str, ...] = ()
    watch_focus: str = ""
    notification_group: str = "default"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Target:
        target_id = validate_target_id(str(value.get("target_id", "")).strip())
        enabled = _parse_bool(value.get("enabled"), "enabled")
        name = str(value.get("name", "")).strip()
        if (
            not name
            or len(name) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise MonitorError(
                "invalid_record", f"target {target_id}: name must be 1-200 characters"
            )
        url = validate_http_url(str(value.get("url", "")).strip())
        fetch_mode = str(value.get("fetch_mode", "static") or "static").strip().lower()
        if fetch_mode not in FETCH_MODES:
            raise MonitorError(
                "invalid_record",
                f"target {target_id}: fetch_mode must be static or browser",
            )
        include_selector = str(value.get("include_selector", "") or "").strip()
        watch_focus = str(value.get("watch_focus", "") or "").strip()
        notification_group = str(
            value.get("notification_group", "default") or "default"
        ).strip()
        if len(include_selector) > 500 or len(watch_focus) > 1_000:
            raise MonitorError(
                "invalid_record",
                f"target {target_id}: selector or watch_focus is too long",
            )
        if not TARGET_ID_RE.fullmatch(notification_group):
            raise MonitorError(
                "invalid_record",
                f"target {target_id}: notification_group has invalid characters",
            )
        return cls(
            target_id=target_id,
            enabled=enabled,
            name=name,
            url=url,
            fetch_mode=fetch_mode,
            include_selector=include_selector,
            exclude_selectors=_parse_selectors(value.get("exclude_selectors")),
            watch_focus=watch_focus,
            notification_group=notification_group,
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["exclude_selectors"] = list(self.exclude_selectors)
        return result


@dataclass(frozen=True, slots=True)
class State:
    target_id: str
    last_checked_at: str = ""
    etag: str = ""
    last_modified: str = ""
    validated_url: str = ""
    normalized_hash: str = ""
    snapshot_ref: str = ""
    consecutive_failures: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> State:
        target_id = validate_target_id(str(value.get("target_id", "")).strip())
        timestamp = str(value.get("last_checked_at", "") or "").strip()
        validate_timestamp(timestamp, "last_checked_at", allow_empty=True)
        normalized_hash = str(value.get("normalized_hash", "") or "").strip().lower()
        if normalized_hash and not HASH_RE.fullmatch(normalized_hash):
            raise MonitorError(
                "invalid_record", f"state {target_id}: normalized_hash is invalid"
            )
        try:
            failures = int(value.get("consecutive_failures", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise MonitorError(
                "invalid_record",
                f"state {target_id}: consecutive_failures must be an integer",
            ) from exc
        if failures < 0:
            raise MonitorError(
                "invalid_record",
                f"state {target_id}: consecutive_failures cannot be negative",
            )
        etag = str(value.get("etag", "") or "")
        last_modified = str(value.get("last_modified", "") or "")
        snapshot_ref = str(value.get("snapshot_ref", "") or "")
        if any(
            len(item) > 1_000
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in (etag, last_modified, snapshot_ref)
        ):
            raise MonitorError(
                "invalid_record",
                f"state {target_id}: validator or snapshot reference is invalid",
            )
        validated_url = str(value.get("validated_url", "") or "").strip()
        if validated_url:
            validated_url = validate_http_url(validated_url, "validated_url")
        return cls(
            target_id=target_id,
            last_checked_at=timestamp,
            etag=etag,
            last_modified=last_modified,
            validated_url=validated_url,
            normalized_hash=normalized_hash,
            snapshot_ref=snapshot_ref,
            consecutive_failures=failures,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Attempt:
    number: int
    result: str
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    target_id: str
    result: str
    change_score: int
    summary: str
    error_code: str
    started_at: str
    finished_at: str
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.run_id or len(self.run_id) > 200:
            raise MonitorError("invalid_record", "run_id is required")
        validate_target_id(self.target_id)
        if self.result not in RUN_RESULTS:
            raise MonitorError("invalid_record", "run result is invalid")
        if not 0 <= self.change_score <= 100:
            raise MonitorError("invalid_record", "change_score must be 0-100")
        if len(self.summary) > 2_000 or len(self.error_code) > 100:
            raise MonitorError(
                "invalid_record", "run summary or error_code is too long"
            )
        validate_timestamp(self.started_at, "started_at")
        validate_timestamp(self.finished_at, "finished_at")

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["attempts"] = [attempt.as_dict() for attempt in self.attempts]
        return result


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    event_id: str
    target_id: str
    status: str
    notified_at: str = ""
    kind: str = "change"
    last_error: str = ""

    def __post_init__(self) -> None:
        if not HASH_RE.fullmatch(self.event_id):
            raise MonitorError("invalid_record", "event_id must be a SHA-256 digest")
        validate_target_id(self.target_id)
        if self.status not in NOTIFICATION_STATUSES:
            raise MonitorError("invalid_record", "notification status is invalid")
        validate_timestamp(self.notified_at, "notified_at", allow_empty=True)
        if self.kind not in {"change", "failure"}:
            raise MonitorError("invalid_record", "notification kind is invalid")
        if len(self.last_error) > 200:
            raise MonitorError("invalid_record", "last_error is too long")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
