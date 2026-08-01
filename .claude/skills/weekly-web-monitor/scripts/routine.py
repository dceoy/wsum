"""Weekly end-to-end monitor orchestration with per-target isolation."""

from __future__ import annotations

import secrets
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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


class OperationalStore(NotificationStore, Protocol):
    def load_enabled_targets(self) -> list[Target]: ...

    def get_state(self, target_id: str) -> State | None: ...

    def replace_state(self, state: State) -> None: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def append_run(self, run: RunRecord) -> None: ...


class SnapshotStorage(Protocol):
    def save(
        self,
        target_id: str,
        content: NormalizedContent,
        diff: DiffResult | None = None,
        previous_hash: str = "",
    ) -> str: ...

    def load_normalized(self, snapshot_ref: str) -> str: ...


class SummaryClient(Protocol):
    def summarize(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class TargetFetcher(Protocol):
    def fetch(self, target: Target, state: State, workspace: Path) -> FetchResult: ...


@dataclass(frozen=True, slots=True)
class RoutineConfig:
    max_concurrency: int = 2
    failure_alert_threshold: int = 3
    retry: RetryConfig = RetryConfig()
    fetch: FetchConfig = FetchConfig()
    browser: BrowserFetchConfig = BrowserFetchConfig()
    diff: DiffConfig = DiffConfig()
    max_notification_chars: int = 1_500
    delivery_mode: str = "direct"

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 4:
            raise MonitorError(
                "invalid_configuration", "max_concurrency must be between 1 and 4"
            )
        if not 1 <= self.failure_alert_threshold <= 100:
            raise MonitorError(
                "invalid_configuration", "failure alert threshold is invalid"
            )
        if self.delivery_mode not in {"direct", "outbox"}:
            raise MonitorError(
                "invalid_configuration", "delivery_mode must be direct or outbox"
            )
        if not 100 <= self.max_notification_chars <= 3_500:
            raise MonitorError(
                "invalid_configuration", "notification length limit is invalid"
            )


@dataclass(frozen=True, slots=True)
class RoutineResult:
    run_id: str
    started_at: str
    finished_at: str
    metrics: RunMetrics
    runs: tuple[RunRecord, ...]

    def as_dict(self) -> dict[str, Any]:
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


@dataclass(frozen=True, slots=True)
class _PendingMaterial:
    target: Target
    previous_state: State
    next_state: State
    event: NotificationEvent
    started_at: str
    change_score: int
    summary_ja: str
    attempts: tuple[Attempt, ...]


class DefaultFetcher:
    def __init__(
        self,
        static_config: FetchConfig,
        browser_config: BrowserFetchConfig,
    ) -> None:
        self._static = static_config
        self._browser = browser_config

    def fetch(self, target: Target, state: State, workspace: Path) -> FetchResult:
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


class WeeklyMonitorRoutine:
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
        self.config = config or RoutineConfig()
        self.store = store
        self.snapshots = snapshots
        self.summary_client = summary_client
        if self.config.delivery_mode == "direct":
            if slack is None or outbox_store is not None:
                raise MonitorError(
                    "invalid_configuration",
                    "direct delivery requires only a Slack connector",
                )
        elif outbox_store is None or slack is not None:
            raise MonitorError(
                "invalid_configuration",
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
        except Exception:
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

    def _success_state(
        self,
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
            # Run is the durable idempotency checkpoint _process_target's
            # claimed-run replay depends on (the get_run check near the top
            # of _process_target). Writing it before State means that if the
            # process fails between these two independent connector writes,
            # a retry with the same run_id finds the terminal Run and
            # replays it instead of re-fetching or re-notifying. The reverse
            # order can advance State while the Run write never lands,
            # silently dropping the result the State change was based on.
            self.store.append_run(run)
            try:
                self.store.replace_state(state)
            except Exception as exc:
                # append_run already landed and dedups by run_id, so it can
                # no longer be made to record a different outcome for this
                # run_id. Raise rather than let a caller retry the pair and
                # silently no-op the append while replace_state runs again.
                raise _PartialCommitError(
                    f"State commit failed after Run {run.run_id} was "
                    "already persisted"
                ) from exc
        return run

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
        now = datetime.now(UTC)
        year_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        event_id = failure_event_id(target.target_id, year_week, threshold)
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
            if existing and existing.status in {"sent", "pending"}:
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
            with self._slack_lock:
                if self.slack is None:
                    raise MonitorError(
                        "connector_configuration_missing",
                        "Slack connector is unavailable",
                    )
                reference = self.slack.send_message(target.notification_group, message)
            if not reference:
                raise AmbiguousDeliveryFailure(
                    "notification_send_failed",
                    "failure alert returned no delivery reference",
                )
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
        except Exception:
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
            # The real previous state was never loaded before this failure
            # (get_run or _state itself raised), so previous_state is still
            # the empty placeholder. Try once more here: if it succeeds we
            # get a correct consecutive_failures count and can safely
            # replace State; if it fails again, skip replace_state entirely
            # rather than overwrite a real baseline with blank data.
            try:
                previous_state = self._state(target)
                state_loaded = True
            except Exception:
                pass
        failed_state = replace(
            previous_state,
            last_checked_at=utc_now(),
            consecutive_failures=previous_state.consecutive_failures + 1,
        )
        error_attempts = error.details.get("attempts", []) if error.details else []
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
        try:
            if state_loaded:
                self._persist_success(failed_state, run)
            else:
                self._append_run(run)
        finally:
            self._failure_alert(target, failed_state, error.code, run_id)
            self._audit(
                "target_execution",
                target_id=target.target_id,
                outcome="failed",
                run_id=run_id,
                metadata={"error_code": error.code},
            )
        return run

    def _process_target(
        self, run_id: str, target: Target
    ) -> RunRecord | _PendingMaterial:
        started_at = utc_now()
        previous_state = State(target.target_id)
        state_loaded = False
        attempts: tuple[Attempt, ...] = ()
        try:
            with self._store_lock:
                claimed = self.store.get_run(self._run_record_id(run_id, target))
            if claimed is not None:
                # A prior attempt for this exact run ID already reached a
                # terminal outcome for this target; replay it instead of
                # refetching, re-writing state, or re-notifying.
                return claimed
            previous_state = self._state(target)
            state_loaded = True
            with tempfile.TemporaryDirectory(
                prefix=f"weekly-web-monitor-{target.target_id}-"
            ) as temporary:
                workspace = Path(temporary)
                retry_kwargs: dict[str, Any] = {"config": self.config.retry}
                if self.sleeper is not None:
                    retry_kwargs["sleeper"] = self.sleeper
                fetched_retry = run_with_retry(
                    lambda: self.fetcher.fetch(target, previous_state, workspace),
                    **retry_kwargs,
                )
                fetched = fetched_retry.value
                attempts = self._renumber(fetched_retry.attempts)
                if fetched.result == "unchanged":
                    next_state = replace(
                        previous_state,
                        last_checked_at=fetched.fetched_at,
                        etag=fetched.etag or previous_state.etag,
                        last_modified=fetched.last_modified
                        or previous_state.last_modified,
                        validated_url=fetched.final_url
                        or previous_state.validated_url,
                        consecutive_failures=0,
                    )
                    run = self._run_record(
                        run_id,
                        target,
                        "unchanged",
                        0,
                        "",
                        "",
                        started_at,
                        attempts,
                    )
                    return self._persist_success(next_state, run)
                raw_path = workspace / "response.bin"
                raw_path.write_bytes(fetched.body)
                normalized = normalize_content(
                    raw_path.read_bytes(),
                    content_type=fetched.content_type,
                    charset=fetched.charset,
                    base_url=fetched.final_url,
                    include_selector=target.include_selector,
                    exclude_selectors=target.exclude_selectors,
                )
                if not previous_state.normalized_hash:
                    with self._snapshot_lock:
                        reference = self.snapshots.save(target.target_id, normalized)
                    next_state = self._success_state(
                        previous_state,
                        fetched,
                        normalized.normalized_hash,
                        reference,
                    )
                    run = self._run_record(
                        run_id,
                        target,
                        "baseline_created",
                        0,
                        "",
                        "",
                        started_at,
                        attempts,
                    )
                    return self._persist_success(next_state, run)
                if previous_state.normalized_hash == normalized.normalized_hash:
                    next_state = self._success_state(
                        previous_state,
                        fetched,
                        previous_state.normalized_hash,
                        previous_state.snapshot_ref,
                    )
                    run = self._run_record(
                        run_id,
                        target,
                        "unchanged",
                        0,
                        "",
                        "",
                        started_at,
                        attempts,
                    )
                    return self._persist_success(next_state, run)
                with self._snapshot_lock:
                    previous_text = self.snapshots.load_normalized(
                        previous_state.snapshot_ref
                    )
                diff = compare_content(
                    previous_text,
                    normalized.text,
                    previous_hash=previous_state.normalized_hash,
                    current_hash=normalized.normalized_hash,
                    config=self.config.diff,
                    watch_focus=target.watch_focus,
                )
                if diff.budget_exceeded:
                    raise MonitorError(
                        "diff_budget_exceeded",
                        "diff exceeded the configured line budget; the change "
                        "cannot be assessed from synthetic evidence and needs "
                        "manual review",
                    )
                if not diff.should_summarize:
                    with self._snapshot_lock:
                        reference = self.snapshots.save(
                            target.target_id,
                            normalized,
                            diff,
                            previous_hash=previous_state.normalized_hash,
                        )
                    next_state = self._success_state(
                        previous_state,
                        fetched,
                        normalized.normalized_hash,
                        reference,
                    )
                    run = self._run_record(
                        run_id,
                        target,
                        "minor",
                        diff.change_score,
                        "",
                        "",
                        started_at,
                        attempts,
                    )
                    return self._persist_success(next_state, run)
                request = build_summary_request(target, diff)
                with self._summary_lock:
                    summary_retry = run_with_retry(
                        lambda: self.summary_client.summarize(request),
                        **retry_kwargs,
                    )
                summary_attempts = self._renumber(
                    summary_retry.attempts, offset=len(attempts)
                )
                attempts = (*attempts, *summary_attempts)
                validated = validate_summary(
                    summary_retry.value,
                    changed_sections=request["changed_sections"],
                    source_url=target.url,
                    max_notification_chars=self.config.max_notification_chars,
                )
                if not validated["material"] and diff.truncated:
                    # A diff can be candidate_material for reasons outside the
                    # five recognized price/spec/terms/availability/
                    # eligibility patterns (e.g. changed ratio alone). Any
                    # truncation means the model judged materiality from an
                    # incomplete view, so a non-material verdict cannot be
                    # trusted regardless of which section was cut.
                    raise MonitorError(
                        "truncated_diff_non_material",
                        "diff truncation dropped evidence the model never saw; "
                        "a non-material verdict over incomplete evidence needs "
                        "manual review before the baseline can advance",
                    )
                with self._snapshot_lock:
                    reference = self.snapshots.save(
                        target.target_id,
                        normalized,
                        diff,
                        previous_hash=previous_state.normalized_hash,
                    )
                next_state = self._success_state(
                    previous_state,
                    fetched,
                    normalized.normalized_hash,
                    reference,
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
                        attempts,
                    )
                    return self._persist_success(next_state, run)
                event = build_change_event(
                    target, normalized.normalized_hash, validated
                )
                return _PendingMaterial(
                    target,
                    previous_state,
                    next_state,
                    event,
                    started_at,
                    diff.change_score,
                    validated["summary_ja"],
                    attempts,
                )
        except _PartialCommitError:
            # The Run for this run_id already landed durably; do not let
            # the generic Exception handler below route this through
            # _finish_failure, which would append_run a no-op (fine) but
            # still replace_state with a reverted, failure-incremented
            # baseline behind a Run that already says success. Propagate
            # so the caller records a content-free failure without any
            # further store write.
            raise
        except MonitorError as exc:
            return self._finish_failure(
                run_id,
                target,
                previous_state,
                exc,
                started_at,
                attempts,
                state_loaded=state_loaded,
            )
        except Exception:
            return self._finish_failure(
                run_id,
                target,
                previous_state,
                MonitorError(
                    "unexpected_error", "target execution failed unexpectedly"
                ),
                started_at,
                attempts,
                state_loaded=state_loaded,
            )

    def _finish_material(
        self,
        run_id: str,
        pending: _PendingMaterial,
        outcome: DeliveryOutcome,
    ) -> RunRecord:
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

    def run(self, *, run_id: str | None = None) -> RoutineResult:
        started_at = utc_now()
        active_run_id = run_id or (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        )
        targets = self.store.load_enabled_targets()
        config_digest = configuration_digest(
            {
                "max_concurrency": self.config.max_concurrency,
                "failure_alert_threshold": self.config.failure_alert_threshold,
                "retry_max_attempts": self.config.retry.max_attempts,
                "diff_minor_threshold": self.config.diff.minor_threshold,
                "delivery_mode": self.config.delivery_mode,
            }
        )
        self._audit(
            "configuration_loaded",
            outcome="succeeded",
            run_id=active_run_id,
            metadata={
                "configuration_digest": config_digest,
                "target_count": len(targets),
            },
        )
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
                except Exception:
                    # A connector can prevent durable state/run writes. Preserve
                    # isolation by returning a content-free terminal result for
                    # this target while other targets continue.
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
        if pending:
            pending.sort(key=lambda item: item.target.target_id)
            if self.config.delivery_mode == "outbox":
                outcomes = {}
                for item in pending:
                    try:
                        outcome = self._queue_material(item)
                    except Exception:
                        outcome = DeliveryOutcome(
                            item.event.event_id,
                            "failed",
                            error_code="connector_unavailable",
                        )
                    outcomes[item.event.event_id] = outcome
            else:
                try:
                    if self.slack is None:
                        raise MonitorError(
                            "connector_configuration_missing",
                            "Slack connector is unavailable",
                        )
                    outcomes = deliver_grouped(
                        [item.event for item in pending],
                        store=self.store,
                        connector=self.slack,
                    )
                except Exception:
                    outcomes = {
                        item.event.event_id: DeliveryOutcome(
                            item.event.event_id,
                            "pending",
                            error_code="connector_unavailable",
                        )
                        for item in pending
                    }
            for item in pending:
                try:
                    completed.append(
                        self._finish_material(
                            active_run_id, item, outcomes[item.event.event_id]
                        )
                    )
                except Exception:
                    completed.append(
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
