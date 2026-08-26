"""Content-free structured audit records and redaction checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from errors import MonitorError
from models import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping

SENSITIVE_KEY_RE = re.compile(
    r"(?:body|content|credential|html|password|payload|secret|text|token|webhook)",
    re.IGNORECASE,
)

_MAX_METADATA_VALUE_LENGTH = 200


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A single content-free structured audit-log entry."""

    event_type: str
    target_id: str
    outcome: str
    occurred_at: str
    run_id: str
    metadata: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this record."""
        return asdict(self)


class AuditSink(Protocol):
    """A destination that can persist ``AuditRecord`` entries."""

    def append_audit(self, record: AuditRecord) -> None:
        """Persist ``record`` to the sink."""
        ...


def configuration_digest(configuration: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of ``configuration``.

    Returns:
        The hex-encoded digest of the canonical JSON encoding of
        ``configuration``.
    """
    encoded = json.dumps(
        configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_audit_record(
    event_type: str,
    *,
    target_id: str = "",
    outcome: str,
    run_id: str,
    metadata: Mapping[str, object] | None = None,
) -> AuditRecord:
    """Build a validated, content-free :class:`AuditRecord`.

    Returns:
        The constructed audit record with a fresh UTC timestamp.

    Raises:
        MonitorError: If ``event_type``, ``outcome``, or any metadata key or
            value fails the content-free audit constraints.
    """
    safe_metadata: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            msg = "audit_invalid"
            raise MonitorError(msg, "audit metadata key is not lower snake case")
        if SENSITIVE_KEY_RE.search(key):
            msg = "audit_sensitive_field"
            raise MonitorError(
                msg,
                "audit metadata key may contain sensitive content",
            )
        rendered = str(value)
        if len(rendered) > _MAX_METADATA_VALUE_LENGTH:
            msg = "audit_field_too_long"
            raise MonitorError(msg, "audit metadata value exceeds 200 characters")
        safe_metadata[key] = rendered
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", event_type):
        msg = "audit_invalid"
        raise MonitorError(msg, "audit event_type is invalid")
    if outcome not in {"attempted", "failed", "skipped", "succeeded"}:
        msg = "audit_invalid"
        raise MonitorError(msg, "audit outcome is invalid")
    return AuditRecord(
        event_type=event_type,
        target_id=target_id,
        outcome=outcome,
        occurred_at=utc_now(),
        run_id=run_id,
        metadata=safe_metadata,
    )
