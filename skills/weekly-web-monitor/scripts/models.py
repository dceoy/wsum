"""Validated records shared across deterministic monitor scripts."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import SplitResult, unquote, urlsplit

from errors import MonitorError
from network_policy import has_credential_bearing_query

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
FETCH_MODES = frozenset({"static", "browser"})
RUN_RESULTS = frozenset({
    "baseline_created",
    "failed",
    "material",
    "minor",
    "non_material",
    "notified",
    "suppressed",
    "unchanged",
})
NOTIFICATION_STATUSES = frozenset({"pending", "sent", "failed", "suppressed"})

_MIN_PRINTABLE_CODEPOINT = 32
_DEL_CODEPOINT = 127
_MIN_PORT = 1
_MAX_PORT = 65535
_MAX_NAME_LENGTH = 200
_MAX_INCLUDE_SELECTOR_LENGTH = 500
_MAX_WATCH_FOCUS_LENGTH = 1_000
_MAX_EXCLUDE_SELECTOR_COUNT = 50
_MAX_EXCLUDE_SELECTOR_LENGTH = 500
_MAX_STATE_FIELD_LENGTH = 1_000
_MAX_RUN_ID_LENGTH = 200
_MIN_CHANGE_SCORE = 0
_MAX_CHANGE_SCORE = 100
_MAX_SUMMARY_LENGTH = 2_000
_MAX_ERROR_CODE_LENGTH = 100
_MAX_LAST_ERROR_LENGTH = 200


def _has_control_chars(value: str) -> bool:
    """Return whether ``value`` contains a C0 control character or DEL."""
    return any(
        ord(char) < _MIN_PRINTABLE_CODEPOINT or ord(char) == _DEL_CODEPOINT
        for char in value
    )


def utc_now() -> str:
    """Return the current UTC time as a ``Z``-suffixed ISO-8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_timestamp(
    value: str, field_name: str, *, allow_empty: bool = False
) -> str:
    """Validate that ``value`` is a timezone-aware ISO-8601 timestamp.

    Returns:
        ``value`` unchanged (or ``""`` if empty and ``allow_empty``).

    Raises:
        MonitorError: If ``value`` is not empty (when disallowed), not a
            valid ISO-8601 timestamp, or lacks a timezone.
    """
    if allow_empty and not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        msg = "invalid_record"
        raise MonitorError(msg, f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        msg = "invalid_record"
        raise MonitorError(msg, f"{field_name} must include a timezone")
    return value


def _has_embedded_credentials_or_bad_port(
    parsed: SplitResult, port: int | None
) -> bool:
    """Return whether ``parsed`` has a bad scheme/host, credentials, or bad port."""
    return (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not _MIN_PORT <= port <= _MAX_PORT)
    )


def validate_http_url(value: str, field_name: str = "url") -> str:
    """Validate that ``value`` is a safe, credential-free HTTP(S) URL.

    Returns:
        ``value`` unchanged.

    Raises:
        MonitorError: If ``value`` contains control characters, is
            malformed, uses a non-HTTP(S) scheme, embeds credentials or a
            fragment, carries a credential-like query parameter, or is a
            known webhook credential URL.
    """
    if _has_control_chars(value):
        msg = "invalid_record"
        raise MonitorError(msg, f"{field_name} contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        msg = "invalid_record"
        raise MonitorError(msg, f"{field_name} is malformed") from exc
    if _has_embedded_credentials_or_bad_port(parsed, port):
        msg = "invalid_record"
        raise MonitorError(
            msg,
            f"{field_name} must be an HTTP(S) URL without embedded credentials",
        )
    if has_credential_bearing_query(parsed.query, allow_path_relative=True):
        msg = "invalid_record"
        raise MonitorError(
            msg,
            f"{field_name} must not contain credential-like query parameters",
        )
    if parsed.fragment:
        msg = "invalid_record"
        raise MonitorError(
            msg,
            f"{field_name} must not contain a URL fragment",
        )
    host = (parsed.hostname or "").lower()
    decoded_path = unquote(parsed.path)
    if (host == "hooks.slack.com" and decoded_path.startswith("/services/")) or (
        host in {"discord.com", "discordapp.com"} and "/api/webhooks/" in decoded_path
    ):
        msg = "invalid_record"
        raise MonitorError(msg, f"{field_name} must not be a webhook credential URL")
    return value


def validate_target_id(value: str) -> str:
    """Validate that ``value`` is a well-formed target_id.

    Returns:
        ``value`` unchanged.

    Raises:
        MonitorError: If ``value`` is not a string matching
            :data:`TARGET_ID_RE`.
    """
    if (
        not isinstance(value, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        # target_id ultimately originates from untrusted, dynamically-typed
        # sheet/JSON data; callers may pass a non-str value at runtime
        # despite the declared type, so this check stays load-bearing.
        or not TARGET_ID_RE.fullmatch(value)
    ):
        msg = "invalid_record"
        raise MonitorError(
            msg,
            "target_id must contain only letters, digits, dot, underscore, or hyphen",
        )
    return value


def _parse_bool(value: object, field_name: str) -> bool:
    """Parse a loosely-typed sheet value into a strict bool.

    Returns:
        The parsed boolean.

    Raises:
        MonitorError: If ``value`` is not a recognized boolean encoding.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    msg = "invalid_record"
    raise MonitorError(msg, f"{field_name} must be a boolean")


def _parse_selectors(value: object) -> tuple[str, ...]:
    """Parse a target's exclude_selectors sheet value into a tuple.

    Returns:
        The parsed, non-empty selector strings.

    Raises:
        MonitorError: If ``value`` has the wrong shape, or exceeds the
            selector count or per-selector length limit.
    """
    if not value:
        return ()
    if isinstance(value, str):
        selectors = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in cast("Sequence[object]", value)
    ):
        str_items = cast("Sequence[str]", value)
        selectors = tuple(item.strip() for item in str_items if item.strip())
    else:
        msg = "invalid_record"
        raise MonitorError(
            msg,
            "exclude_selectors must be a comma-separated string or list",
        )
    if len(selectors) > _MAX_EXCLUDE_SELECTOR_COUNT or any(
        len(item) > _MAX_EXCLUDE_SELECTOR_LENGTH for item in selectors
    ):
        msg = "invalid_record"
        raise MonitorError(msg, "exclude_selectors exceeds the count or length limit")
    return selectors


@dataclass(frozen=True, slots=True)
class Target:
    """A validated monitor target loaded from the Targets sheet."""

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
        """Build a validated target from a raw sheet row mapping.

        Returns:
            The constructed, validated target.

        Raises:
            MonitorError: If any field is missing, malformed, or out of
                range.
        """
        target_id = validate_target_id(str(value.get("target_id", "")).strip())
        enabled = _parse_bool(value.get("enabled"), "enabled")
        name = str(value.get("name", "")).strip()
        if not name or len(name) > _MAX_NAME_LENGTH or _has_control_chars(name):
            msg = "invalid_record"
            raise MonitorError(
                msg, f"target {target_id}: name must be 1-200 characters"
            )
        url = validate_http_url(str(value.get("url", "")).strip())
        fetch_mode = str(value.get("fetch_mode", "static") or "static").strip().lower()
        if fetch_mode not in FETCH_MODES:
            msg = "invalid_record"
            raise MonitorError(
                msg,
                f"target {target_id}: fetch_mode must be static or browser",
            )
        include_selector = str(value.get("include_selector", "") or "").strip()
        watch_focus = str(value.get("watch_focus", "") or "").strip()
        notification_group = str(
            value.get("notification_group", "default") or "default"
        ).strip()
        if (
            len(include_selector) > _MAX_INCLUDE_SELECTOR_LENGTH
            or len(watch_focus) > _MAX_WATCH_FOCUS_LENGTH
        ):
            msg = "invalid_record"
            raise MonitorError(
                msg,
                f"target {target_id}: selector or watch_focus is too long",
            )
        if not TARGET_ID_RE.fullmatch(notification_group):
            msg = "invalid_record"
            raise MonitorError(
                msg,
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
        """Return a JSON-serializable representation of this target."""
        result = asdict(self)
        result["exclude_selectors"] = list(self.exclude_selectors)
        return result


@dataclass(frozen=True, slots=True)
class State:
    """A target's validated persisted fetch/normalize state."""

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
        """Build a validated state from a raw sheet row mapping.

        Returns:
            The constructed, validated state.

        Raises:
            MonitorError: If any field is malformed or out of range.
        """
        target_id = validate_target_id(str(value.get("target_id", "")).strip())
        timestamp = str(value.get("last_checked_at", "") or "").strip()
        validate_timestamp(timestamp, "last_checked_at", allow_empty=True)
        normalized_hash = str(value.get("normalized_hash", "") or "").strip().lower()
        if normalized_hash and not HASH_RE.fullmatch(normalized_hash):
            msg = "invalid_record"
            raise MonitorError(msg, f"state {target_id}: normalized_hash is invalid")
        try:
            failures = int(value.get("consecutive_failures", 0) or 0)
        except (TypeError, ValueError) as exc:
            msg = "invalid_record"
            raise MonitorError(
                msg,
                f"state {target_id}: consecutive_failures must be an integer",
            ) from exc
        if failures < 0:
            msg = "invalid_record"
            raise MonitorError(
                msg,
                f"state {target_id}: consecutive_failures cannot be negative",
            )
        etag = str(value.get("etag", "") or "")
        last_modified = str(value.get("last_modified", "") or "")
        snapshot_ref = str(value.get("snapshot_ref", "") or "")
        if any(
            len(item) > _MAX_STATE_FIELD_LENGTH or _has_control_chars(item)
            for item in (etag, last_modified, snapshot_ref)
        ):
            msg = "invalid_record"
            raise MonitorError(
                msg,
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
        """Return a JSON-serializable representation of this state."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Attempt:
    """A single retry attempt outcome, embedded in a :class:`RunRecord`."""

    number: int
    result: str
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this attempt."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A validated record of one monitor run's outcome for one target."""

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
        """Validate every field of the record.

        Raises:
            MonitorError: If any field is malformed or out of range.
        """
        if not self.run_id or len(self.run_id) > _MAX_RUN_ID_LENGTH:
            msg = "invalid_record"
            raise MonitorError(msg, "run_id is required")
        validate_target_id(self.target_id)
        if self.result not in RUN_RESULTS:
            msg = "invalid_record"
            raise MonitorError(msg, "run result is invalid")
        if not _MIN_CHANGE_SCORE <= self.change_score <= _MAX_CHANGE_SCORE:
            msg = "invalid_record"
            raise MonitorError(msg, "change_score must be 0-100")
        if (
            len(self.summary) > _MAX_SUMMARY_LENGTH
            or len(self.error_code) > _MAX_ERROR_CODE_LENGTH
        ):
            msg = "invalid_record"
            raise MonitorError(msg, "run summary or error_code is too long")
        validate_timestamp(self.started_at, "started_at")
        validate_timestamp(self.finished_at, "finished_at")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this run record."""
        result = asdict(self)
        result["attempts"] = [attempt.as_dict() for attempt in self.attempts]
        return result


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """A validated dedup/delivery-state record for one notification event."""

    event_id: str
    target_id: str
    status: str
    notified_at: str = ""
    kind: str = "change"
    last_error: str = ""

    def __post_init__(self) -> None:
        """Validate every field of the record.

        Raises:
            MonitorError: If any field is malformed or out of range.
        """
        if not HASH_RE.fullmatch(self.event_id):
            msg = "invalid_record"
            raise MonitorError(msg, "event_id must be a SHA-256 digest")
        validate_target_id(self.target_id)
        if self.status not in NOTIFICATION_STATUSES:
            msg = "invalid_record"
            raise MonitorError(msg, "notification status is invalid")
        validate_timestamp(self.notified_at, "notified_at", allow_empty=True)
        if self.kind not in {"change", "failure"}:
            msg = "invalid_record"
            raise MonitorError(msg, "notification kind is invalid")
        if len(self.last_error) > _MAX_LAST_ERROR_LENGTH:
            msg = "invalid_record"
            raise MonitorError(msg, "last_error is too long")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this record."""
        return asdict(self)
