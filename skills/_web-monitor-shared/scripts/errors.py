"""Stable, content-safe errors used by the monitoring pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MonitorError(Exception):
    """An expected terminal or retryable monitoring failure.

    Error messages must describe the class of failure without including response
    bodies, connector payloads, credentials, or other untrusted content.
    """

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        """Return the ``"code: message"`` rendering used in logs and CLI output."""
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this error."""
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            result["details"] = self.details
        return result


RETRYABLE_ERROR_CODES = frozenset({
    "connector_unavailable",
    "drive_write_failed",
    "fetch_connection_failed",
    "fetch_timeout",
    "http_rate_limited",
    "http_server_error",
    "notification_send_failed",
})


def is_retryable_error(error: BaseException) -> bool:
    """Return whether ``error`` represents a monitoring failure worth retrying."""
    return isinstance(error, MonitorError) and (
        error.retryable or error.code in RETRYABLE_ERROR_CODES
    )
