"""Bounded deterministic retry helper with explicit attempt records."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from errors import MonitorError, is_retryable_error
from models import Attempt

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "max_attempts must be between 1 and 5"
            )
        if not 0 <= self.initial_delay_seconds <= 60:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "initial retry delay is invalid"
            )
        if not 1 <= self.backoff_multiplier <= 10:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "retry backoff multiplier is invalid"
            )
        if not 0 <= self.max_delay_seconds <= 300:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "maximum retry delay is invalid"
            )


@dataclass(frozen=True, slots=True)
class RetryResult(Generic[T]):
    value: T
    attempts: tuple[Attempt, ...]


def run_with_retry(
    operation: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> RetryResult[T]:
    active = config or RetryConfig()
    attempts: list[Attempt] = []
    delay = active.initial_delay_seconds
    for number in range(1, active.max_attempts + 1):
        try:
            value = operation()
            attempts.append(Attempt(number, "succeeded"))
            return RetryResult(value, tuple(attempts))
        except Exception as exc:
            code = exc.code if isinstance(exc, MonitorError) else "unexpected_error"
            attempts.append(Attempt(number, "failed", code))
            if number >= active.max_attempts or not is_retryable_error(exc):
                if isinstance(exc, MonitorError):
                    exc.details = {
                        **(exc.details or {}),
                        "attempts": [attempt.as_dict() for attempt in attempts],
                    }
                    raise
                msg = "unexpected_error"
                raise MonitorError(
                    msg,
                    "operation failed unexpectedly",
                    details={"attempts": [attempt.as_dict() for attempt in attempts]},
                ) from exc
            sleeper(min(delay, active.max_delay_seconds))
            delay = min(delay * active.backoff_multiplier, active.max_delay_seconds)
    msg = "retry loop must return or raise"
    raise AssertionError(msg)
