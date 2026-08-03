"""Content-free structured audit records and redaction checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from errors import MonitorError
from models import utc_now

SENSITIVE_KEY_RE = re.compile(
    r"(?:body|content|credential|html|password|payload|secret|text|token|webhook)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event_type: str
    target_id: str
    outcome: str
    occurred_at: str
    run_id: str
    metadata: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditSink(Protocol):
    def append_audit(self, record: AuditRecord) -> None: ...


def configuration_digest(configuration: Mapping[str, Any]) -> str:
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
    safe_metadata: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise MonitorError(
                "audit_invalid", "audit metadata key is not lower snake case"
            )
        if SENSITIVE_KEY_RE.search(key):
            raise MonitorError(
                "audit_sensitive_field",
                "audit metadata key may contain sensitive content",
            )
        rendered = str(value)
        if len(rendered) > 200:
            raise MonitorError(
                "audit_field_too_long", "audit metadata value exceeds 200 characters"
            )
        safe_metadata[key] = rendered
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", event_type):
        raise MonitorError("audit_invalid", "audit event_type is invalid")
    if outcome not in {"attempted", "failed", "skipped", "succeeded"}:
        raise MonitorError("audit_invalid", "audit outcome is invalid")
    return AuditRecord(
        event_type=event_type,
        target_id=target_id,
        outcome=outcome,
        occurred_at=utc_now(),
        run_id=run_id,
        metadata=safe_metadata,
    )
