"""End-to-end web update monitor orchestration with per-target isolation."""

from __future__ import annotations

import secrets
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from audit import AuditSink, configuration_digest, make_audit_record
from diff import DiffConfig, DiffResult, compare_content
from errors import MonitorError
from fetch import FetchConfig, FetchResult, fetch_url
from fetch_browser import BrowserFetchConfig, fetch_rendered
from metrics import RunMetrics, calculate_metrics
from models import Attempt, NotificationRecord, RunRecord, State, Target, utc_now
from normalize import NormalizedContent, normalize_content
from notifications import (
    AmbiguousDeliveryFailure,
    ConfirmedDeliveryFailure,
    DeliveryOutcome,
    NotificationEvent,
    NotificationStore,
    SlackConnector,
    build_change_event,
    deliver_grouped,
    escape_slack_text,
    failure_event_id,
)
from outbox import OutboxStore, enqueue_record
from retry import RetryConfig, run_with_retry
from summary import build_summary_request
from validate_summary import validate_summary

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class OperationalStore(NotificationStore, Protocol):
    """The persistence surface the routine needs beyond notifications."""

    def load_enabled_targets(self) -> list[Target]:
        """Return every target enabled for monitoring."""
        ...

    def get_state(self, target_id: str) -> State | None:
        """Return the target's last-known state, or None if never checked."""
        ...

    def replace_state(self, state: State) -> None:
        """Persist ``state`` as the target's new current state."""
        ...

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record for ``run_id``, or None if not found."""
        ...

    def append_run(self, run: RunRecord) -> None:
        """Durably record ``run``, deduplicated by its run id."""
        ...


class SnapshotStorage(Protocol):
    """Storage for normalized content snapshots, addressed by reference."""

    def save(
        self,
        target_id: str,
        content: NormalizedContent,
        diff: DiffResult | None = None,
        previous_hash: str = "",
    ) -> str:
        """Persist ``content`` and return its snapshot reference."""
        ...

    def load_normalized(self, snapshot_ref: str) -> str:
        """Return the normalized text stored at ``snapshot_ref``."""
        ...


class SummaryClient(Protocol):
    """A client that can produce a natural-language change summary."""

    def summarize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a summary response for ``request``."""
        ...


class TargetFetcher(Protocol):
    """Something that can fetch one target's current content."""

    def fetch(self, target: Target, state: State, workspace: Path) -> FetchResult:
        """Fetch ``target``'s current content, given its prior ``state``."""
        ...


_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 4
_MIN_FAILURE_ALERT_THRESHOLD = 1
_MAX_FAILURE_ALERT_THRESHOLD = 100
_MIN_NOTIFICATION_CHARS = 100
_MAX_NOTIFICATION_CHARS = 3_500


@dataclass(frozen=True, slots=True)
class RoutineConfig:
    """Validated tunables for one :class:`WebUpdateMonitorRoutine`."""

    max_concurrency: int = 2
    failure_alert_threshold: int = 3
    retry: RetryConfig = field(default_factory=RetryConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    browser: BrowserFetchConfig = field(default_factory=BrowserFetchConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    max_notification_chars: int = 1_500
    delivery_mode: str = "direct"

    def __post_init__(self) -> None:
        """Validate every field's bounds.

        Raises:
            MonitorError: If any field is outside its allowed bounds.
        """
        if not _MIN_CONCURRENCY <= self.max_concurrency <= _MAX_CONCURRENCY:
            msg = "invalid_configuration"
            raise MonitorError(msg, "max_concurrency must be between 1 and 4")
        if not (
            _MIN_FAILURE_ALERT_THRESHOLD
            <= self.failure_alert_threshold
            <= _MAX_FAILURE_ALERT_THRESHOLD
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "failure alert threshold is invalid")
        if self.delivery_mode not in {"direct", "outbox"}:
            msg = "invalid_configuration"
            raise MonitorError(msg, "delivery_mode must be direct or outbox")
        if not (
            _MIN_NOTIFICATION_CHARS
            <= self.max_notification_chars
            <= _MAX_NOTIFICATION_CHARS
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "notification length limit is invalid")


@dataclass(frozen=True, slots=True)
class RoutineResult:
    """The outcome of one full routine run across every enabled target."""

    run_id: str
    started_at: str
    finished_at: str
    metrics: RunMetrics
    runs: tuple[RunRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this result.

        Returns:
            This result's fields, with ``metrics``/``runs`` recursively
            converted to plain dicts/lists.
        """
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metrics": self.metrics.as_dict(),
            "runs": [run.as_dict() for run in self.runs],
        }


class _PartialCommitError(Exception):
    """append_run committed but the paired replace_state call failed.

    append_run dedups by run_id, so the Run row is now the durable,
    unoverwritable record of this run_id's outcome. Routing this through
    _finish_failure would append-run a no-op (the success row survives) but
    still replace_state with a reverted/failure-incremented baseline behind
    a Run that already says success. Propagate instead and leave State
    exactly as it was; the next run re-detects any drift from the stale
    baseline rather than silently masking it.
    """


@dataclass(slots=True)
class _TargetRunContext:
    """Mutable per-target run state, tracked so exception handlers see progress."""

    previous_state: State
    state_loaded: bool = False
    attempts: tuple[Attempt, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingMaterial:
    """Everything computed for a detected change, before it is persisted/delivered."""

    target: Target
    previous_state: State
    next_state: State
    event: NotificationEvent
    started_at: str
    change_score: int
    summary_ja: str
    attempts: tuple[Attempt, ...]


class DefaultFetcher:
    """Fetches a target via static HTTP or a headless browser, per its mode."""

    def __init__(
        self,
        static_config: FetchConfig,
        browser_config: BrowserFetchConfig,
    ) -> None:
        """Store the configs used for each fetch mode."""
        self._static = static_config
        self._browser = browser_config

    def fetch(self, target: Target, state: State, workspace: Path) -> FetchResult:
        """Fetch ``target``'s current content via its configured fetch mode.

        Returns:
            The fetch outcome.
        """
        del workspace
        if target.fetch_mode == "browser":
            return fetch_rendered(target.url, config=self._browser)
        return fetch_url(
            target.url,
            etag=state.etag,
            last_modified=state.last_modified,
            validated_url=state.validated_url,
            config=self._static,
        )


class WebUpdateMonitorRoutine:
    """Orchestrates one monitoring cycle across every enabled target."""

    def __init__(
        self,
        *,
        store: OperationalStore,
        snapshots: SnapshotStorage,
        summary_client: SummaryClient,
        slack: SlackConnector | None = None,
        outbox_store: OutboxStore | None = None,
        fetcher: TargetFetcher | None = None,
        audit_sink: AuditSink | None = None,
        config: RoutineConfig | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """Wire up the routine's stores/connectors, validating delivery mode.

        Raises:
            MonitorError: If the connector supplied for delivery does not
                match ``config.delivery_mode`` (exactly one of ``slack``/
                ``outbox_store`` must be set, matching the mode).
        """
        self.config = config or RoutineConfig()
        self.store = store
        self.snapshots = snapshots
        self.summary_client = summary_client
        if self.config.delivery_mode == "direct":
            if slack is None or outbox_store is not None:
                msg = "invalid_configuration"
                raise MonitorError(
                    msg,
                    "direct delivery requires only a Slack connector",
                )
        elif outbox_store is None or slack is not None:
            msg = "invalid_configuration"
            raise MonitorError(
                msg,
                "outbox delivery requires only an Outbox store",
            )
        self.slack = slack
        self.outbox_store = outbox_store
        self.fetcher = fetcher or DefaultFetcher(self.config.fetch, self.config.browser)
        self.audit_sink = audit_sink
        self.sleeper = sleeper
        self._store_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._summary_lock = threading.RLock()
        self._slack_lock = threading.RLock()

    def _audit(
        self,
        event_type: str,
        *,
        target_id: str = "",
        outcome: str,
        run_id: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if self.audit_sink is None:
            return
        record = make_audit_record(
            event_type,
            target_id=target_id,
            outcome=outcome,
            run_id=run_id,
            metadata=metadata,
        )
        try:
            with self._store_lock:
                self.audit_sink.append_audit(record)
        except Exception:  # ruff: ignore[blind-except] -- audit-sink failure modes are ambiguous by design:
            # Audit availability must not change fetch, state, or delivery
            # decisions. Routine metrics still expose the primary outcome.
            return

    def _state(self, target: Target) -> State:
        with self._store_lock:
            return self.store.get_state(target.target_id) or State(target.target_id)

    def _append_run(self, run: RunRecord) -> RunRecord:
        with self._store_lock:
            self.store.append_run(run)
        return run

    @staticmethod
    def _success_state(
        state: State,
        fetched: FetchResult,
        normalized_hash: str,
        snapshot_ref: str,
    ) -> State:
        return State(
            target_id=state.target_id,
            last_checked_at=fetched.fetched_at,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            validated_url=fetched.final_url,
            normalized_hash=normalized_hash,
            snapshot_ref=snapshot_ref,
            consecutive_failures=0,
        )

    @staticmethod
    def _run_record_id(run_id: str, target: Target) -> str:
        return f"{run_id}:{target.target_id}"

    @staticmethod
    def _validate_run_id(run_id: object, targets: Sequence[Target]) -> str:
        if (
            not isinstance(run_id, str)
            or not run_id
            or any(not char.isprintable() for char in run_id)
        ):
            msg = "invalid_record"
            raise MonitorError(
                msg,
                "run_id must be a non-empty string without control characters",
            )
        longest_target_id = max(
            (len(target.target_id) for target in targets), default=-1
        )
        max_length = 200 if longest_target_id < 0 else 199 - longest_target_id
        if len(run_id) > max_length:
            msg = "invalid_record"
            raise MonitorError(
                msg,
                "run_id is too long for the enabled target IDs",
            )
        return run_id

    def _run_record(
        self,
        run_id: str,
        target: Target,
        result: str,
        score: int,
        summary: str,
        error_code: str,
        started_at: str,
        attempts: Sequence[Attempt],
    ) -> RunRecord:
        return RunRecord(
            run_id=self._run_record_id(run_id, target),
            target_id=target.target_id,
            result=result,
            change_score=score,
            summary=summary,
            error_code=error_code,
            started_at=started_at,
            finished_at=utc_now(),
            attempts=tuple(attempts),
        )

    @staticmethod
    def _renumber(attempts: Sequence[Attempt], offset: int = 0) -> tuple[Attempt, ...]:
        return tuple(
            Attempt(index + offset, attempt.result, attempt.error_code)
            for index, attempt in enumerate(attempts, start=1)
        )

    def _persist_success(self, state: State, run: RunRecord) -> RunRecord:
        with self._store_lock:
            self.store.append_run(run)
            try:
                self.store.replace_state(state)
            except Exception as exc:
                msg = (
                    f"State commit failed after Run {run.run_id} was already persisted"
                )
                raise _PartialCommitError(msg) from exc
        return run

    def _send_slack_message(self, target: Target, message: str) -> str:
        """Send ``message`` for ``target`` via the configured Slack connector.

        Returns:
            The delivery reference.

        Raises:
            MonitorError: If no Slack connector is configured.
            AmbiguousDeliveryFailure: If ``send_message`` returns no
                delivery reference.
        """
        with self._slack_lock:
            if self.slack is None:
                msg = "connector_configuration_missing"
                raise MonitorError(
                    msg,
                    "Slack connector is unavailable",
                )
            reference = self.slack.send_message(target.notification_group, message)
        if not reference:
            msg = "notification_send_failed"
            raise AmbiguousDeliveryFailure(
                msg,
                "failure alert returned no delivery reference",
            )
        return reference

    @staticmethod
    def _failure_episode_id(state: State) -> str:
        """Return the stable identity for the current consecutive-failure episode."""
        return state.last_checked_at or "initial"

    def _failure_alert(
        self,
        target: Target,
        state: State,
        error_code: str,
        run_id: str,
    ) -> None:
        threshold = self.config.failure_alert_threshold
        if state.consecutive_failures < threshold:
            return
        event_id = failure_event_id(
            target.target_id,
            self._failure_episode_id(state),
            threshold,
        )
        message = (
            "*Web監視エラー*\n"
            f"{escape_slack_text(target.name)} ({target.target_id}) の監視が "
            f"{state.consecutive_failures} 回連続で失敗しました。\n"
            f"エラーコード: {error_code}"
        )
        if self.config.delivery_mode == "outbox":
            outcome = self._queue_outbox_message(event_id, target, message)
            self._audit(
                "failure_alert",
                target_id=target.target_id,
                outcome=(
                    "succeeded"
                    if outcome.status in {"queued", "suppressed"}
                    else "failed"
                ),
                run_id=run_id,
                metadata={"error_code": outcome.error_code or error_code},
            )
            return
        with self._store_lock:
            existing = self.store.get_notification(event_id)
            if existing and existing.status in {"sent", "pending", "suppressed"}:
                return
            if state.consecutive_failures == threshold:
                crossing_event_id = failure_event_id(
                    target.target_id,
                    run_id,
                    threshold,
                )
                if crossing_event_id != event_id:
                    crossing = self.store.get_notification(crossing_event_id)
                    if crossing and crossing.status in {"sent", "pending", "suppressed"}:
                        return
            self.store.upsert_notification(
                NotificationRecord(
                    event_id,
                    target.target_id,
                    "pending",
                    kind="failure",
                )
            )
        try:
            self._send_slack_message(target, message)
        except ConfirmedDeliveryFailure:
            with self._store_lock:
                self.store.upsert_notification(
                    NotificationRecord(
                        event_id,
                        target.target_id,
                        "failed",
                        kind="failure",
                        last_error="notification_send_failed",
                    )
                )
            self._audit(
                "failure_alert",
                target_id=target.target_id,
                outcome="failed",
                run_id=run_id,
                metadata={"error_code": error_code},
            )
            return
        except Exception:  # ruff: ignore[blind-except] -- any delivery failure here is ambiguous by design
            self._audit(
                "failure_alert",
                target_id=target.target_id,
                outcome="failed",
                run_id=run_id,
                metadata={"error_code": "delivery_ambiguous"},
            )
            return
        with self._store_lock:
            self.store.upsert_notification(
                NotificationRecord(
                    event_id,
                    target.target_id,
                    "sent",
                    notified_at=utc_now(),
                    kind="failure",
                )
            )
        self._audit(
            "failure_alert",
            target_id=target.target_id,
            outcome="succeeded",
            run_id=run_id,
            metadata={"error_code": error_code},
        )

    def _queue_outbox_message(
        self,
        event_id: str,
        target: Target,
        message: str,
    ) -> DeliveryOutcome:
        if self.outbox_store is None:
            return DeliveryOutcome(
                event_id, "failed", error_code="connector_configuration_missing"
            )
        with self._store_lock:
            existing = self.outbox_store.get_outbox(event_id)
            if existing:
                if existing.status == "sent":
                    return DeliveryOutcome(event_id, "suppressed")
                if existing.status in {"pending", "sending", "retry"}:
                    return DeliveryOutcome(event_id, "queued")
                return DeliveryOutcome(event_id, "failed", error_code="outbox_poison")
            self.outbox_store.upsert_outbox(
                enqueue_record(
                    event_id,
                    target.target_id,
                    target.notification_group,
                    message,
                )
            )
        return DeliveryOutcome(event_id, "queued")

    def _queue_material(self, pending: _PendingMaterial) -> DeliveryOutcome:
        event = pending.event
        message = (
            f"*{escape_slack_text(event.target.name)}*\n{event.notification_text_ja}"
        )
        if event.recommended_action_ja:
            message += f"\n推奨対応: {event.recommended_action_ja}"
        return self._queue_outbox_message(event.event_id, event.target, message)

    def _finish_failure(
        self,
        run_id: str,
        target: Target,
        previous_state: State,
        error: MonitorError,
        started_at: str,
        attempts: Sequence[Attempt],
        *,
        state_loaded: bool = True,
    ) -> RunRecord:
        if not state_loaded:
            try:
                previous_state = self._state(target)
                state_loaded = True
            except Exception:  # ruff: ignore[try-except-pass, blind-except] - the second load is best-effort
                pass
        failed_state = replace(
            previous_state,
            consecutive_failures=previous_state.consecutive_failures + 1,
        )
        error_attempts = (
            cast("list[dict[str, Any]]", error.details.get("attempts", []))
            if error.details
            else []
        )
        combined = list(attempts)
        if error_attempts:
            parsed_attempts = tuple(
                Attempt(
                    int(item["number"]),
                    str(item["result"]),
                    str(item.get("error_code", "")),
                )
                for item in error_attempts
            )
            combined.extend(
                self._renumber(parsed_attempts, offset=len(combined))
                if combined
                else parsed_attempts
            )
        run = self._run_record(
            run_id,
            target,
            "failed",
            0,
            "",
            error.code,
            started_at,
            combined,
        )
        failure_state_committed = False
        try:
            if state_loaded:
                self._persist_success(failed_state, run)
                failure_state_committed = True
            else:
                self._append_run(run)
        finally:
            if failure_state_committed:
                self._failure_alert(target, failed_state, error.code, run_id)
            self._audit(
                "target_execution",
                target_id=target.target_id,
                outcome="failed",
                run_id=run_id,
                metadata={"error_code": error.code},
            )
        return run

    def _handle_unchanged_fetch(
        self,
        run_id: str,
        target: Target,
        ctx: _TargetRunContext,
        fetched: FetchResult,
        started_at: str,
    ) -> RunRecord:
        """Persist the run for a fetch that itself reported no change (e.g. HTTP 304).

        Returns:
            The persisted run record.
        """
        next_state = replace(
            ctx.previous_state,
            last_checked_at=fetched.fetched_at,
            etag=fetched.etag or ctx.previous_state.etag,
            last_modified=fetched.last_modified or ctx.previous_state.last_modified,
            validated_url=fetched.final_url or ctx.previous_state.validated_url,
            consecutive_failures=0,
        )
        run = self._run_record(
            run_id, target, "unchanged", 0, "", "", started_at, ctx.attempts
        )
        return self._persist_success(next_state, run)

    def _handle_baseline_created(
        self,
        run_id: str,
        target: Target,
        ctx: _TargetRunContext,
        fetched: FetchResult,
        normalized: NormalizedContent,
        started_at: str,
    ) -> RunRecord:
        """Persist the run for the first-ever snapshot of a target.

        Returns:
            The persisted run record.
        """
        with self._snapshot_lock:
            reference = self.snapshots.save(target.target_id, normalized)
        next_state = self._success_state(
            ctx.previous_state, fetched, normalized.normalized_hash, reference
        )
        run = self._run_record(
            run_id, target, "baseline_created", 0, "", "", started_at, ctx.attempts
        )
        return self._persist_success(next_state, run)

    def _handle_unchanged_hash(
        self,
        run_id: str,
        target: Target,
        ctx: _TargetRunContext,
        fetched: FetchResult,
        started_at: str,
    ) -> RunRecord:
        """Persist the run for content that normalized to the same hash as before.

        Returns:
            The persisted run record.
        """
        next_state = self._success_state(
            ctx.previous_state,
            fetched,
            ctx.previous_state.normalized_hash,
            ctx.previous_state.snapshot_ref,
        )
        run = self._run_record(
            run_id, target, "unchanged", 0, "", "", started_at, ctx.attempts
        )
        return self._persist_success(next_state, run)

    def _handle_minor_change(
        self,
        run_id: str,
        target: Target,
        ctx: _TargetRunContext,
        fetched: FetchResult,
        normalized: NormalizedContent,
        diff: DiffResult,
        started_at: str,
    ) -> RunRecord:
        """Persist the run for a change too minor to warrant summarization.

        Returns:
            The persisted run record.
        """
        with self._snapshot_lock:
            reference = self.snapshots.save(
                target.target_id,
                normalized,
                diff,
                previous_hash=ctx.previous_state.normalized_hash,
            )
        next_state = self._success_state(
            ctx.previous_state, fetched, normalized.normalized_hash, reference
        )
        run = self._run_record(
            run_id,
            target,
            "minor",
            diff.change_score,
            "",
            "",
            started_at,
            ctx.attempts,
        )
        return self._persist_success(next_state, run)

    def _summarize_and_build_outcome(
        self,
        run_id: str,
        target: Target,
        ctx: _TargetRunContext,
        fetched: FetchResult,
        normalized: NormalizedContent,
        diff: DiffResult,
        started_at: str,
        retry_kwargs: dict[str, Any],
    ) -> RunRecord | _PendingMaterial:
        """Summarize a material-candidate diff and build its run outcome.

        Returns:
            The persisted run record for a non-material verdict, or pending
            change material (for the caller to deliver) for a material one.

        Raises:
            MonitorError: If the diff was truncated and the model judged it
                non-material anyway -- the evidence was too incomplete to
                trust that verdict.
        """
        request = build_summary_request(target, diff)
        with self._summary_lock:
            summary_retry = run_with_retry(
                lambda: self.summary_client.summarize(request),
                **retry_kwargs,
            )
        summary_attempts = self._renumber(
            summary_retry.attempts, offset=len(ctx.attempts)
        )
        ctx.attempts = (*ctx.attempts, *summary_attempts)
        validated = validate_summary(
            summary_retry.value,
            changed_sections=request["changed_sections"],
            source_url=target.url,
            max_notification_chars=self.config.max_notification_chars,
        )
        if not validated["material"] and diff.truncated:
            msg = "truncated_diff_non_material"
            raise MonitorError(
                msg,
                "diff truncation dropped evidence the model never saw; "
                "a non-material verdict over incomplete evidence needs "
                "manual review before the baseline can advance",
            )
        with self._snapshot_lock:
            reference = self.snapshots.save(
                target.target_id,
                normalized,
                diff,
                previous_hash=ctx.previous_state.normalized_hash,
            )
        next_state = self._success_state(
            ctx.previous_state, fetched, normalized.normalized_hash, reference
        )
        if not validated["material"]:
            run = self._run_record(
                run_id,
                target,
                "non_material",
                diff.change_score,
                validated["summary_ja"],
                "",
                started_at,
                ctx.attempts,
            )
            return self._persist_success(next_state, run)
        event = build_change_event(target, normalized.normalized_hash, validated)
        return _PendingMaterial(
            target,
            ctx.previous_state,
            next_state,
            event,
            started_at,
            diff.change_score,
            validated["summary_ja"],
            ctx.attempts,
        )

    def _run_target_pipeline(
        self,
        run_id: str,
        target: Target,
        started_at: str,
        ctx: _TargetRunContext,
        workspace: Path,
    ) -> RunRecord | _PendingMaterial:
        """Fetch, normalize, diff, and (if material) summarize one target.

        Returns:
            The finished run record, or pending change material awaiting
            delivery.

        Raises:
            MonitorError: If the diff exceeds its configured line budget
                (via :func:`compare_content`), or (via
                :func:`_summarize_and_build_outcome`) a truncated diff was
                judged non-material.
        """
        retry_kwargs: dict[str, Any] = {"config": self.config.retry}
        if self.sleeper is not None:
            retry_kwargs["sleeper"] = self.sleeper
        fetched_retry = run_with_retry(
            lambda: self.fetcher.fetch(target, ctx.previous_state, workspace),
            **retry_kwargs,
        )
        fetched = fetched_retry.value
        ctx.attempts = self._renumber(fetched_retry.attempts)
        if fetched.result == "unchanged":
            return self._handle_unchanged_fetch(
                run_id, target, ctx, fetched, started_at
            )
        normalized = normalize_content(
            fetched.body,
            content_type=fetched.content_type,
            charset=fetched.charset,
            base_url=fetched.final_url,
            include_selector=target.include_selector,
            exclude_selectors=target.exclude_selectors,
        )
        if not ctx.previous_state.normalized_hash:
            return self._handle_baseline_created(
                run_id, target, ctx, fetched, normalized, started_at
            )
        if ctx.previous_state.normalized_hash == normalized.normalized_hash:
            return self._handle_unchanged_hash(run_id, target, ctx, fetched, started_at)
        with self._snapshot_lock:
            previous_text = self.snapshots.load_normalized(
                ctx.previous_state.snapshot_ref
            )
        diff = compare_content(
            previous_text,
            normalized.text,
            previous_hash=ctx.previous_state.normalized_hash,
            current_hash=normalized.normalized_hash,
            config=self.config.diff,
            watch_focus=target.watch_focus,
        )
        if diff.budget_exceeded:
            msg = "diff_budget_exceeded"
            raise MonitorError(
                msg,
                "diff exceeded the configured line budget; the change "
                "cannot be assessed from synthetic evidence and needs "
                "manual review",
            )
        if not diff.should_summarize:
            return self._handle_minor_change(
                run_id, target, ctx, fetched, normalized, diff, started_at
            )
        return self._summarize_and_build_outcome(
            run_id, target, ctx, fetched, normalized, diff, started_at, retry_kwargs
        )

    def _claimed_run(self, run_id: str, target: Target) -> RunRecord | None:
        """Return the already-recorded run for this run/target pair, if any.

        Returns:
            The prior run record if this exact run ID already reached a
            terminal outcome for this target, otherwise None.
        """
        with self._store_lock:
            return self.store.get_run(self._run_record_id(run_id, target))

    def _run_target_pipeline_in_workspace(
        self,
        run_id: str,
        target: Target,
        started_at: str,
        ctx: _TargetRunContext,
    ) -> RunRecord | _PendingMaterial:
        """Run the target pipeline inside a scratch workspace directory.

        Returns:
            The finished run record, or pending change material awaiting
            delivery.
        """
        with tempfile.TemporaryDirectory(
            prefix=f"web-update-monitor-{target.target_id}-"
        ) as temporary:
            workspace = Path(temporary)
            return self._run_target_pipeline(run_id, target, started_at, ctx, workspace)

    def _process_target(
        self, run_id: str, target: Target
    ) -> RunRecord | _PendingMaterial:
        """Run the per-target pipeline, routing failures to :func:`_finish_failure`.

        Returns:
            The finished run record, or pending change material awaiting
            delivery.

        Raises:
            _PartialCommitError: If a prior failure's Run record landed but
                its paired state update did not.
        """
        started_at = utc_now()
        ctx = _TargetRunContext(previous_state=State(target.target_id))
        try:
            claimed = self._claimed_run(run_id, target)
            if claimed is not None:
                return claimed
            ctx.previous_state = self._state(target)
            ctx.state_loaded = True
            return self._run_target_pipeline_in_workspace(
                run_id, target, started_at, ctx
            )
        except _PartialCommitError:
            raise
        except MonitorError as exc:
            return self._finish_failure(
                run_id,
                target,
                ctx.previous_state,
                exc,
                started_at,
                ctx.attempts,
                state_loaded=ctx.state_loaded,
            )
        except Exception:  # ruff: ignore[blind-except] -- any target-pipeline failure not already
            # classified as a MonitorError is recorded as an opaque,
            # content-free "unexpected_error" so target execution never
            # crashes the routine.
            return self._finish_failure(
                run_id,
                target,
                ctx.previous_state,
                MonitorError(
                    "unexpected_error", "target execution failed unexpectedly"
                ),
                started_at,
                ctx.attempts,
                state_loaded=ctx.state_loaded,
            )

    def _finish_material(
        self,
        run_id: str,
        pending: _PendingMaterial,
        outcome: DeliveryOutcome,
    ) -> RunRecord:
        operator_suppressed = (
            outcome.status == "suppressed"
            and outcome.error_code == "operator_suppressed"
        )
        if operator_suppressed:
            attempts = (
                *pending.attempts,
                Attempt(len(pending.attempts) + 1, "notification_suppressed"),
            )
            run = self._run_record(
                run_id,
                pending.target,
                "suppressed",
                pending.change_score,
                pending.summary_ja,
                "",
                pending.started_at,
                attempts,
            )
            self._audit(
                "change_notification",
                target_id=pending.target.target_id,
                outcome="skipped",
                run_id=run_id,
                metadata={
                    "event_id": pending.event.event_id,
                    "delivery_state": outcome.status,
                },
            )
            return self._persist_success(pending.next_state, run)
        if outcome.status in {"sent", "suppressed", "queued"}:
            queued = outcome.status == "queued"
            attempts = (
                *pending.attempts,
                Attempt(
                    len(pending.attempts) + 1,
                    "outbox_queued" if queued else "notification_succeeded",
                ),
            )
            run = self._run_record(
                run_id,
                pending.target,
                "material" if queued else "notified",
                pending.change_score,
                pending.summary_ja,
                "",
                pending.started_at,
                attempts,
            )
            self._audit(
                "change_notification",
                target_id=pending.target.target_id,
                outcome="succeeded",
                run_id=run_id,
                metadata={
                    "event_id": pending.event.event_id,
                    "delivery_state": outcome.status,
                },
            )
            return self._persist_success(pending.next_state, run)
        error = MonitorError(
            outcome.error_code or "notification_send_failed",
            "material change notification was not confirmed",
            retryable=outcome.status == "failed",
        )
        return self._finish_failure(
            run_id,
            pending.target,
            pending.previous_state,
            error,
            pending.started_at,
            pending.attempts,
        )

    def _run_targets(
        self, active_run_id: str, targets: Sequence[Target], started_at: str
    ) -> tuple[list[RunRecord], list[_PendingMaterial]]:
        """Run every target concurrently, isolating executor-level failures.

        Returns:
            The completed run records, and any material change awaiting
            delivery.
        """
        completed: list[RunRecord] = []
        pending: list[_PendingMaterial] = []
        with ThreadPoolExecutor(max_workers=self.config.max_concurrency) as executor:
            futures = {
                executor.submit(self._process_target, active_run_id, target): target
                for target in targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    result = future.result()
                except Exception:  # ruff: ignore[blind-except] -- a connector can
                    # prevent durable state/run writes from inside
                    # _process_target. Preserve isolation by returning a
                    # content-free terminal result for this target while
                    # other targets continue.
                    result = self._run_record(
                        active_run_id,
                        target,
                        "failed",
                        0,
                        "",
                        "connector_unavailable",
                        started_at,
                        (),
                    )
                if isinstance(result, _PendingMaterial):
                    pending.append(result)
                else:
                    completed.append(result)
        return completed, pending

    def _deliver_via_outbox(
        self, pending: Sequence[_PendingMaterial]
    ) -> dict[str, DeliveryOutcome]:
        """Queue every pending material change onto the outbox delivery backend.

        Returns:
            Each event's queue-or-failure delivery outcome, by event ID.
        """
        outcomes: dict[str, DeliveryOutcome] = {}
        for item in pending:
            try:
                outcome = self._queue_material(item)
            except Exception:  # ruff: ignore[blind-except] -- the outbox store is a
                # caller-supplied boundary whose failure modes cannot be
                # enumerated; any failure here must not abort other targets.
                outcome = DeliveryOutcome(
                    item.event.event_id,
                    "failed",
                    error_code="connector_unavailable",
                )
            outcomes[item.event.event_id] = outcome
        return outcomes

    def _require_slack(self) -> SlackConnector:
        """Return the configured Slack connector.

        Returns:
            The routine's Slack connector.

        Raises:
            MonitorError: If no Slack connector is configured.
        """
        if self.slack is None:
            msg = "connector_configuration_missing"
            raise MonitorError(msg, "Slack connector is unavailable")
        return self.slack

    def _deliver_via_slack(
        self, pending: Sequence[_PendingMaterial]
    ) -> dict[str, DeliveryOutcome]:
        """Group-deliver every pending material change over Slack.

        Returns:
            Each event's delivery outcome, by event ID -- a content-free
            "pending" placeholder for every event if delivery itself failed.
        """
        try:
            slack = self._require_slack()
            return deliver_grouped(
                [item.event for item in pending], store=self.store, connector=slack
            )
        except Exception:  # ruff: ignore[blind-except] -- the Slack connector is a
            # caller-supplied boundary whose failure modes cannot be
            # enumerated; any failure here must not abort other targets.
            return {
                item.event.event_id: DeliveryOutcome(
                    item.event.event_id,
                    "pending",
                    error_code="connector_unavailable",
                )
                for item in pending
            }

    def _finish_pending(
        self,
        active_run_id: str,
        pending: Sequence[_PendingMaterial],
        outcomes: Mapping[str, DeliveryOutcome],
    ) -> list[RunRecord]:
        """Persist the terminal outcome for every pending material change.

        Returns:
            The finished run record for each pending item, in the same order.
        """
        finished: list[RunRecord] = []
        for item in pending:
            try:
                finished.append(
                    self._finish_material(
                        active_run_id, item, outcomes[item.event.event_id]
                    )
                )
            except Exception:  # ruff: ignore[blind-except] -- persisting the outcome
                # can itself hit the same caller-supplied store/connector
                # boundary; any failure here must not abort other targets.
                finished.append(
                    self._run_record(
                        active_run_id,
                        item.target,
                        "failed",
                        0,
                        "",
                        "connector_unavailable",
                        item.started_at,
                        item.attempts,
                    )
                )
        return finished

    def _deliver_pending(
        self, active_run_id: str, pending: list[_PendingMaterial]
    ) -> list[RunRecord]:
        """Deliver and persist every pending material change, sorted by target.

        Returns:
            The finished run record for each pending item.
        """
        pending.sort(key=lambda item: item.target.target_id)
        if self.config.delivery_mode == "outbox":
            outcomes = self._deliver_via_outbox(pending)
        else:
            outcomes = self._deliver_via_slack(pending)
        return self._finish_pending(active_run_id, pending, outcomes)

    def run(self, *, run_id: str | None = None) -> RoutineResult:
        """Run one full monitoring cycle across every enabled target.

        Returns:
            The completed routine result, with a run record per target and
            summary metrics.
        """
        started_at = utc_now()
        targets = self.store.load_enabled_targets()
        candidate_run_id: object = run_id
        if candidate_run_id is None:
            candidate_run_id = (
                datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + secrets.token_hex(4)
            )
        active_run_id = self._validate_run_id(candidate_run_id, targets)
        config_digest = configuration_digest({
            "max_concurrency": self.config.max_concurrency,
            "failure_alert_threshold": self.config.failure_alert_threshold,
            "retry_max_attempts": self.config.retry.max_attempts,
            "diff_minor_threshold": self.config.diff.minor_threshold,
            "delivery_mode": self.config.delivery_mode,
        })
        self._audit(
            "configuration_loaded",
            outcome="succeeded",
            run_id=active_run_id,
            metadata={
                "configuration_digest": config_digest,
                "target_count": len(targets),
            },
        )
        completed, pending = self._run_targets(active_run_id, targets, started_at)
        if pending:
            completed.extend(self._deliver_pending(active_run_id, pending))
        completed.sort(key=lambda run: run.target_id)
        finished_at = utc_now()
        metrics = calculate_metrics(completed)
        self._audit(
            "routine_execution",
            outcome="failed" if metrics.failed else "succeeded",
            run_id=active_run_id,
            metadata=metrics.as_dict(),
        )
        return RoutineResult(
            active_run_id,
            started_at,
            finished_at,
            metrics,
            tuple(completed),
        )


# Backward-compatible import for callers that used the pre-rename internal class.
WeeklyMonitorRoutine = WebUpdateMonitorRoutine
