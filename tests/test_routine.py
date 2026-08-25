"""Tests for the routine module."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from diff import DiffConfig
from drive import SnapshotStore
from errors import MonitorError
from memory_adapters import (
    EvidenceSummaryClient,
    FixtureFetcher,
    FixtureResponse,
    MemoryDriveConnector,
    MemoryOperationalStore,
    MemorySlackConnector,
)
from models import NotificationRecord, RunRecord, State, Target
from normalize import normalize_content
from notifications import change_event_id, failure_event_id
from retry import RetryConfig
from routine import RoutineConfig, RoutineResult, WeeklyMonitorRoutine

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from audit import AuditRecord


def make_target(target_id: str = "one", group: str = "default") -> Target:
    """Build a static-fetch target for the given ID and notification group.

    Returns:
        The constructed target.
    """
    return Target.from_mapping(
        {
            "target_id": target_id,
            "enabled": True,
            "name": target_id.title(),
            "url": f"https://example.com/{target_id}",
            "fetch_mode": "static",
            "include_selector": "main",
            "exclude_selectors": "",
            "watch_focus": "price and terms",
            "notification_group": group,
        }
    )


def response(price: int) -> FixtureResponse:
    """Build a fixture HTML response showing the given price.

    Returns:
        The constructed fixture response.
    """
    return FixtureResponse(
        (
            "<html><body><main><h1>Product</h1>"
            f"<p>Price: ¥{price}</p></main></body></html>"
        ).encode(),
        "text/html",
    )


def many_lines_response(line_count: int) -> FixtureResponse:
    """Build a fixture HTML response with `line_count` paragraph lines.

    Returns:
        The constructed fixture response.
    """
    body = "".join(f"<p>Line {index}</p>" for index in range(line_count))
    return FixtureResponse(
        f"<html><body><main><h1>Product</h1>{body}</main></body></html>".encode(),
        "text/html",
    )


def paragraphs_response(paragraphs: list[str]) -> FixtureResponse:
    """Build a fixture HTML response with one paragraph per item.

    Returns:
        The constructed fixture response.
    """
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return FixtureResponse(
        f"<html><body><main><h1>Product</h1>{body}</main></body></html>".encode(),
        "text/html",
    )


class NonMaterialSummary:
    """A summary client stub that always returns a non-material result."""

    def summarize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a fixed non-material summary derived from `request`.

        Returns:
            The stub summary response.
        """
        return {
            "material": False,
            "significance": request["deterministic_assessment"]["significance"],
            "summary_ja": "重要な変更は確認されませんでした。",
            "evidence": [],
            "recommended_action_ja": "",
            "notification_text_ja": "",
            "source_url": request["target"]["source_url"],
        }


class RoutineTests(unittest.TestCase):
    """Tests for RoutineTests."""

    def setUp(self) -> None:
        """Build a fresh target, store, snapshots, and Slack connector."""
        self.target = make_target()
        self.store = MemoryOperationalStore([self.target])
        self.drive_connector = MemoryDriveConnector()
        self.snapshots = SnapshotStore(self.drive_connector)
        self.slack = MemorySlackConnector()

    def run_cycle(
        self,
        fixture: FixtureResponse | BaseException,
        run_id: str,
        *,
        slack: MemorySlackConnector | None = None,
        retry: RetryConfig | None = None,
    ) -> tuple[RoutineResult, FixtureFetcher]:
        """Run one routine cycle against a single fixture response.

        Returns:
            The routine's result, and the fetcher used to produce it.
        """
        fetcher = FixtureFetcher({"one": fixture})
        routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=slack or self.slack,
            fetcher=fetcher,
            audit_sink=self.store,
            config=RoutineConfig(
                max_concurrency=2,
                retry=retry or RetryConfig(),
                failure_alert_threshold=3,
            ),
            sleeper=lambda _: None,
        )
        return routine.run(run_id=run_id), fetcher

    def test_baseline_material_notification_then_unchanged_and_cleanup(self) -> None:
        """Test that baseline material notification then unchanged and cleanup."""
        baseline, first_fetcher = self.run_cycle(response(1000), "run-1")
        assert baseline.metrics.baseline == 1
        assert len(self.slack.messages) == 0
        first_hash = self.store.states["one"].normalized_hash
        assert first_hash

        changed, second_fetcher = self.run_cycle(response(1200), "run-2")
        assert changed.metrics.notified == 1
        assert len(self.slack.messages) == 1
        second_hash = self.store.states["one"].normalized_hash
        assert first_hash != second_hash

        unchanged, third_fetcher = self.run_cycle(response(1200), "run-3")
        assert unchanged.metrics.unchanged == 1
        assert len(self.slack.messages) == 1
        assert self.store.states["one"].consecutive_failures == 0
        assert len(self.store.runs) == 3
        for fetcher in (first_fetcher, second_fetcher, third_fetcher):
            assert fetcher.workspaces
            assert all(not Path(workspace).exists() for workspace in fetcher.workspaces)

    def test_operator_suppressed_change_advances_baseline_without_notified_result(
        self,
    ) -> None:
        # An operator-suppressed NotificationRecord means this event was
        # never sent and never will be, unlike the "sent" dedup case that
        # legitimately reuses the same "suppressed" delivery outcome status.
        # Conflating the two used to report a material change as "notified"
        # even though deliver_grouped never called Slack for it.
        """Test that operator suppressed change advances baseline without notified result."""
        self.run_cycle(response(1000), "run-1")
        fixture = response(1200)
        normalized = normalize_content(
            fixture.body,
            content_type=fixture.content_type,
            base_url=self.target.url,
            include_selector=self.target.include_selector,
            exclude_selectors=self.target.exclude_selectors,
        )
        event_id = change_event_id(self.target.target_id, normalized.normalized_hash)
        self.store.upsert_notification(
            NotificationRecord(
                event_id,
                self.target.target_id,
                "suppressed",
                kind="change",
                last_error="operator_suppressed",
            )
        )

        result, _ = self.run_cycle(fixture, "run-2")

        assert self.slack.messages == []
        assert result.metrics.notified == 0
        assert result.metrics.failed == 0
        assert self.store.runs["run-2:one"].result == "suppressed"
        assert normalized.normalized_hash == self.store.states["one"].normalized_hash

    def test_failed_notification_preserves_baseline_and_retries_safely(self) -> None:
        """Test that failed notification preserves baseline and retries safely."""
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        failing_slack = MemorySlackConnector(["default"])
        failed, _ = self.run_cycle(response(1200), "run-2", slack=failing_slack)
        assert failed.metrics.failed == 1
        assert baseline_hash == self.store.states["one"].normalized_hash
        assert self.store.states["one"].consecutive_failures == 1
        retried, _ = self.run_cycle(response(1200), "run-3")
        assert retried.metrics.notified == 1
        assert baseline_hash != self.store.states["one"].normalized_hash

    def test_retry_failure_alert_and_recovery(self) -> None:
        """Test that retry failure alert and recovery."""
        transient = MonitorError("fetch_timeout", "fixture timeout", retryable=True)
        for index in range(1, 4):
            result, fetcher = self.run_cycle(
                transient,
                f"run-{index}",
                retry=RetryConfig(max_attempts=3, initial_delay_seconds=0),
            )
            assert result.metrics.failed == 1
            assert fetcher.calls["one"] == 3
        assert self.store.states["one"].consecutive_failures == 3
        assert len(self.slack.messages) == 1
        recovered, _ = self.run_cycle(response(1000), "run-4")
        assert recovered.metrics.baseline == 1
        assert self.store.states["one"].consecutive_failures == 0

    def test_suppressed_failure_alert_is_preserved_and_not_retried(self) -> None:
        """Test that suppressed failure alert is preserved and not retried."""
        transient = MonitorError("fetch_timeout", "fixture timeout", retryable=True)
        for index in range(1, 3):
            self.run_cycle(
                transient,
                f"run-{index}",
                retry=RetryConfig(max_attempts=1),
            )
        now = datetime.now(UTC).isocalendar()
        event_id = failure_event_id("one", f"{now.year}-W{now.week:02d}", threshold=3)
        suppressed = NotificationRecord(
            event_id,
            "one",
            "suppressed",
            kind="failure",
            last_error="operator_suppressed",
        )
        self.store.upsert_notification(suppressed)

        result, _ = self.run_cycle(
            transient,
            "run-3",
            retry=RetryConfig(max_attempts=1),
        )

        assert result.metrics.failed == 1
        assert self.slack.messages == []
        assert suppressed == self.store.notifications[event_id]

    def test_transient_state_read_failure_does_not_wipe_existing_baseline(
        self,
    ) -> None:
        # get_state fails on the very first call (before the real previous
        # state is ever loaded) but succeeds on a later call, simulating a
        # transient read failure that a naive fix would paper over by
        # persisting the empty placeholder State and destroying the
        # existing baseline.
        """Test that transient state read failure does not wipe existing baseline."""
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        class FlakyStateStore(MemoryOperationalStore):
            """A state store stub that fails a configurable number of times."""

            def __init__(
                self, targets: Sequence[Target], states: dict[str, State]
            ) -> None:
                """Seed the store with `targets` and a pre-populated `states` map."""
                super().__init__(targets)
                self.states = states
                self.get_state_calls = 0

            def get_state(self, target_id: str) -> State | None:
                """Fail the first call, then delegate to the real lookup.

                Returns:
                    The target's state, once no longer simulating a failure.

                Raises:
                    MonitorError: On the first call only.
                """
                self.get_state_calls += 1
                if self.get_state_calls == 1:
                    msg = "state_read_failed"
                    raise MonitorError(
                        msg, "simulated transient failure"
                    )
                return super().get_state(target_id)

        flaky_store = FlakyStateStore([self.target], dict(self.store.states))
        routine = WeeklyMonitorRoutine(
            store=flaky_store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1200)}),
            config=RoutineConfig(
                max_concurrency=2,
                retry=RetryConfig(),
                failure_alert_threshold=3,
            ),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="run-2")
        assert result.metrics.failed == 1
        assert baseline_hash == flaky_store.states["one"].normalized_hash
        assert flaky_store.states["one"].consecutive_failures == 1
        assert flaky_store.runs["run-2:one"].result == "failed"

    def test_persistent_state_read_failure_still_records_run_without_state_write(
        self,
    ) -> None:
        # get_state fails on every call, including _finish_failure's single
        # retry. The run must still complete (not raise out of
        # _process_target) and the real State row must be left completely
        # untouched rather than replaced with the empty placeholder.
        """Test that persistent state read failure still records run without state write."""
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        class AlwaysFailingStateStore(MemoryOperationalStore):
            """A state store stub whose writes always fail."""

            def __init__(
                self, targets: Sequence[Target], states: dict[str, State]
            ) -> None:
                """Seed the store with `targets` and a pre-populated `states` map."""
                super().__init__(targets)
                self.states = states

            def get_state(self, target_id: str) -> State | None:
                """Always fail, simulating a persistently unavailable store.

                Raises:
                    MonitorError: Always, to simulate the persistent failure.
                """
                del target_id
                msg = "state_read_failed"
                raise MonitorError(msg, "simulated persistent failure")

        broken_store = AlwaysFailingStateStore([self.target], dict(self.store.states))
        routine = WeeklyMonitorRoutine(
            store=broken_store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1200)}),
            config=RoutineConfig(
                max_concurrency=2,
                retry=RetryConfig(),
                failure_alert_threshold=3,
            ),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="run-2")
        assert result.metrics.failed == 1
        assert baseline_hash == broken_store.states["one"].normalized_hash
        assert broken_store.states["one"].consecutive_failures == 0
        assert broken_store.runs["run-2:one"].result == "failed"

    def test_run_is_written_before_state_on_success(self) -> None:
        # If the process fails between the two independent connector writes
        # in _persist_success, only the write ordering determines whether a
        # retry with the same run_id finds a terminal Run to replay (safe)
        # or an advanced State with no matching Run (silently drops the
        # outcome the State change was based on). Run must land first.
        """Test that run is written before state on success."""
        calls: list[str] = []

        class RecordingStore(MemoryOperationalStore):
            """A state store stub that records every write it receives."""

            def append_run(self, run: RunRecord) -> None:
                """Record the call, then delegate to the real append."""
                calls.append("append_run")
                super().append_run(run)

            def replace_state(self, state: State) -> None:
                """Record the call, then delegate to the real replace."""
                calls.append("replace_state")
                super().replace_state(state)

        store = RecordingStore([self.target])
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1000)}),
            config=RoutineConfig(max_concurrency=2, retry=RetryConfig()),
            sleeper=lambda _: None,
        )
        routine.run(run_id="run-1")
        assert calls == ["append_run", "replace_state"]

    def test_partial_commit_does_not_revert_state_behind_a_committed_run(
        self,
    ) -> None:
        # append_run for a success outcome lands, then the paired
        # replace_state raises. append_run dedups by run_id, so a naive
        # routing through _finish_failure would append_run a no-op (the
        # success row survives) but still replace_state with a reverted,
        # failure-incremented baseline behind a Run that already says
        # success. State must be left exactly as it was instead.
        """Test that partial commit does not revert state behind a committed run."""
        self.run_cycle(response(1000), "run-1")
        baseline_state = self.store.states["one"]

        class PartialCommitStore(MemoryOperationalStore):
            """A state store stub that fails partway through a batch commit."""

            def __init__(
                self,
                targets: Sequence[Target],
                states: dict[str, State],
                runs: dict[str, RunRecord],
            ) -> None:
                """Seed the store with pre-populated `states` and `runs` maps."""
                super().__init__(targets)
                self.states = states
                self.runs = runs
                self.replace_state_calls = 0

            def replace_state(self, state: State) -> None:
                """Fail the first call, then delegate to the real replace.

                Raises:
                    MonitorError: On the first call only.
                """
                self.replace_state_calls += 1
                if self.replace_state_calls == 1:
                    msg = "state_write_failed"
                    raise MonitorError(
                        msg, "simulated transient failure"
                    )
                super().replace_state(state)

        store = PartialCommitStore(
            [self.target], dict(self.store.states), dict(self.store.runs)
        )
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1000)}),
            config=RoutineConfig(max_concurrency=2, retry=RetryConfig()),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="run-2")
        assert result.metrics.failed == 1
        assert store.runs["run-2:one"].result == "unchanged"
        assert baseline_state == store.states["one"]

    def test_failure_alert_waits_for_durable_failure_state(self) -> None:
        """Test that failure alert waits for durable failure state."""
        transient = MonitorError("fetch_timeout", "fixture timeout", retryable=True)
        for index in range(1, 3):
            self.run_cycle(
                transient,
                f"run-{index}",
                retry=RetryConfig(max_attempts=1),
            )
        assert self.store.states["one"].consecutive_failures == 2

        class FailingStateStore(MemoryOperationalStore):
            """A state store stub whose reads always fail."""

            def replace_state(self, state: State) -> None:
                """Always fail, simulating a persistently unavailable store.

                Raises:
                    MonitorError: Always, to simulate the write failure.
                """
                del state
                msg = "state_write_failed"
                raise MonitorError(msg, "simulated write failure")

        store = FailingStateStore([self.target])
        store.states = dict(self.store.states)
        store.runs = dict(self.store.runs)
        slack = MemorySlackConnector()
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=slack,
            fetcher=FixtureFetcher({"one": transient}),
            config=RoutineConfig(
                max_concurrency=2,
                retry=RetryConfig(max_attempts=1),
                failure_alert_threshold=3,
            ),
            sleeper=lambda _: None,
        )

        result = routine.run(run_id="run-3")

        assert result.metrics.failed == 1
        assert store.states["one"].consecutive_failures == 2
        assert slack.messages == []
        assert store.notifications == {}

    def test_one_target_failure_does_not_abort_other_targets(self) -> None:
        """Test that one target failure does not abort other targets."""
        targets = [make_target("good"), make_target("bad")]
        store = MemoryOperationalStore(targets)
        fetcher = FixtureFetcher(
            {
                "good": response(1000),
                "bad": MonitorError("selector_no_match", "fixture"),
            }
        )
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=SnapshotStore(MemoryDriveConnector()),
            summary_client=EvidenceSummaryClient(),
            slack=MemorySlackConnector(),
            fetcher=fetcher,
            config=RoutineConfig(max_concurrency=2),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="isolated")
        assert result.metrics.checked == 2
        assert result.metrics.baseline == 1
        assert result.metrics.failed == 1
        assert {"good", "bad"} == set(store.states)

    def test_same_run_id_is_idempotent_in_fixture_store(self) -> None:
        """Test that same run id is idempotent in fixture store."""
        self.run_cycle(response(1000), "stable")
        assert len(self.store.runs) == 1
        baseline_state = self.store.states["one"]
        second, second_fetcher = self.run_cycle(response(1200), "stable")
        assert len(self.store.runs) == 1
        assert second.metrics.baseline == 1
        assert second_fetcher.calls["one"] == 0
        assert baseline_state == self.store.states["one"]

    def test_invalid_caller_run_id_fails_before_target_side_effects(self) -> None:
        """Test that invalid caller run id fails before target side effects."""
        longest = make_target("x" * 128)
        store = MemoryOperationalStore([self.target, longest])
        fetcher = FixtureFetcher(
            {"one": response(1000), longest.target_id: response(1000)}
        )
        slack = MemorySlackConnector()
        routine = WeeklyMonitorRoutine(
            store=store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=slack,
            fetcher=fetcher,
            audit_sink=store,
            sleeper=lambda _: None,
        )

        invalid_run_ids = ("", "line\nbreak", "r" * 72, 123)
        for invalid_run_id in invalid_run_ids:
            with (
                self.subTest(run_id=invalid_run_id),
                pytest.raises(MonitorError, match="run_id"),
            ):
                routine.run(run_id=invalid_run_id)  # type: ignore[arg-type]

        assert dict(fetcher.calls) == {}
        assert slack.messages == []
        assert store.states == {}
        assert store.runs == {}
        assert store.notifications == {}
        assert store.audit == []

    def test_audit_sink_failure_does_not_change_primary_result(self) -> None:
        """Test that audit sink failure does not change primary result."""
        class FailingAudit:
            """An audit sink stub whose writes always fail."""

            def append_audit(self, record: AuditRecord) -> None:
                """Always fail, simulating a persistently unavailable sink.

                Raises:
                    RuntimeError: Always, to simulate the write failure.
                """
                del record
                raise RuntimeError

        routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=EvidenceSummaryClient(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1000)}),
            audit_sink=FailingAudit(),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="audit-failure")
        assert result.metrics.baseline == 1
        assert "one" in self.store.states

    def test_outbox_is_a_mutually_exclusive_delivery_backend(self) -> None:
        """Test that outbox is a mutually exclusive delivery backend."""
        def outbox_cycle(price: int, run_id: str) -> RoutineResult:
            """Run one outbox-delivery-mode cycle for the given price.

            Returns:
                The routine's result.
            """
            routine = WeeklyMonitorRoutine(
                store=self.store,
                snapshots=self.snapshots,
                summary_client=EvidenceSummaryClient(),
                outbox_store=self.store,
                fetcher=FixtureFetcher({"one": response(price)}),
                config=RoutineConfig(delivery_mode="outbox"),
                sleeper=lambda _: None,
            )
            return routine.run(run_id=run_id)

        baseline = outbox_cycle(1000, "outbox-1")
        assert baseline.metrics.baseline == 1
        changed = outbox_cycle(1200, "outbox-2")
        assert changed.metrics.material == 1
        assert changed.metrics.notified == 0
        assert len(self.store.outbox) == 1
        assert next(iter(self.store.outbox.values())).status == "pending"
        with pytest.raises(MonitorError, match="requires only"):
            WeeklyMonitorRoutine(
                store=self.store,
                snapshots=self.snapshots,
                summary_client=EvidenceSummaryClient(),
                slack=self.slack,
                outbox_store=self.store,
                config=RoutineConfig(delivery_mode="outbox"),
            )

    def test_summary_connector_attempts_are_recorded_with_fetch_attempt(self) -> None:
        """Test that summary connector attempts are recorded with fetch attempt."""
        self.run_cycle(response(1000), "summary-1")

        class FailingSummary:
            """A summary client stub whose calls always fail."""

            def summarize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
                """Always fail, simulating a persistently unavailable connector.

                Raises:
                    MonitorError: Always, to simulate the connector failure.
                """
                del request
                msg = "connector_unavailable"
                raise MonitorError(
                    msg,
                    "fixture summary connector failure",
                    retryable=True,
                )

        routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=FailingSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1200)}),
            config=RoutineConfig(
                retry=RetryConfig(max_attempts=2, initial_delay_seconds=0)
            ),
            sleeper=lambda _: None,
        )
        result = routine.run(run_id="summary-2")
        assert result.metrics.failed == 1
        assert len(result.runs[0].attempts) == 3
        assert [attempt.error_code for attempt in result.runs[0].attempts] == ["", "connector_unavailable", "connector_unavailable"]

    def test_oversized_diff_fails_closed_instead_of_advancing_baseline(self) -> None:
        """Test that oversized diff fails closed instead of advancing baseline."""
        class UnreachableSummary:
            """A summary client stub that fails the test if invoked."""

            def summarize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
                """Fail the test, since this must never be invoked.

                Raises:
                    AssertionError: Always; being called at all is the failure.
                """
                del request
                msg = (
                    "synthetic budget-exceeded evidence must never reach a "
                    "summary model"
                )
                raise AssertionError(
                    msg
                )

        routine_config = RoutineConfig(
            max_concurrency=2,
            retry=RetryConfig(),
            failure_alert_threshold=3,
            diff=DiffConfig(max_diff_lines=1_000),
        )
        baseline_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=UnreachableSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": response(1000)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        baseline_routine.run(run_id="budget-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        oversized_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=UnreachableSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": many_lines_response(1_500)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        result = oversized_routine.run(run_id="budget-2")
        assert result.metrics.failed == 1
        assert self.store.runs["budget-2:one"].error_code == "diff_budget_exceeded"
        assert baseline_hash == self.store.states["one"].normalized_hash
        assert self.store.states["one"].consecutive_failures == 1
        assert len(self.slack.messages) == 0

    def test_non_material_over_ordinary_truncation_still_advances_baseline(
        self,
    ) -> None:
        # 40 changed paragraphs with one price change buried among them: this
        # truncates under a small max_sections, but the price section is
        # prioritized into the retained evidence, so a genuine non-material
        # verdict from the model is trusted and the baseline still advances.
        """Test that non material over ordinary truncation still advances baseline."""
        baseline_paragraphs = [
            "Price: ¥10" if index == 35 else f"Note {index} original"
            for index in range(40)
        ]
        changed_paragraphs = [
            "Price: ¥20" if index == 35 else f"Note {index} changed"
            for index in range(40)
        ]
        routine_config = RoutineConfig(
            max_concurrency=2,
            retry=RetryConfig(),
            failure_alert_threshold=3,
            diff=DiffConfig(max_sections=30),
        )
        baseline_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(baseline_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        baseline_routine.run(run_id="signal-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        changed_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(changed_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        result = changed_routine.run(run_id="signal-2")
        assert result.metrics.minor == 1
        assert self.store.runs["signal-2:one"].result == "non_material"
        assert baseline_hash != self.store.states["one"].normalized_hash
        assert self.store.states["one"].consecutive_failures == 0

    def test_truncated_non_signal_sections_with_non_material_verdict_fail_closed(
        self,
    ) -> None:
        # None of the changed sections match a recognized price/spec/terms/
        # availability/eligibility pattern, so signal_section_truncated is
        # False; but the diff is candidate_material on changed ratio alone,
        # and truncation still drops sections the model never saw. A
        # non-material verdict over that incomplete evidence must not be
        # trusted just because no *recognized* pattern was cut.
        """Test that truncated non signal sections with non material verdict fail closed."""
        baseline_paragraphs: list[str] = []
        changed_paragraphs: list[str] = []
        for index in range(5):
            baseline_paragraphs.extend(
                [f"Anchor {index}", f"Note {index} original text here"]
            )
            changed_paragraphs.extend(
                [
                    f"Anchor {index}",
                    f"Note {index} completely different content now present",
                ]
            )
        routine_config = RoutineConfig(
            max_concurrency=2,
            retry=RetryConfig(),
            failure_alert_threshold=3,
            diff=DiffConfig(max_sections=3),
        )
        baseline_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(baseline_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        baseline_routine.run(run_id="nonsignal-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        changed_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(changed_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        result = changed_routine.run(run_id="nonsignal-2")
        assert result.metrics.failed == 1
        assert self.store.runs["nonsignal-2:one"].error_code == "truncated_diff_non_material"
        assert baseline_hash == self.store.states["one"].normalized_hash
        assert self.store.states["one"].consecutive_failures == 1

    def test_truncated_signal_sections_with_non_material_verdict_fail_closed(
        self,
    ) -> None:
        # Five separate price changes but only two sections fit the budget:
        # some signal-bearing evidence is unavoidably dropped, so a
        # non-material verdict cannot be trusted to advance the baseline.
        # Anchor paragraphs between each price keep them as distinct diff
        # opcodes instead of collapsing into a single contiguous replace.
        """Test that truncated signal sections with non material verdict fail closed."""
        baseline_paragraphs: list[str] = []
        changed_paragraphs: list[str] = []
        for index in range(5):
            baseline_paragraphs.extend(
                [f"Anchor {index}", f"Price: ¥{100 + index}"]
            )
            changed_paragraphs.extend(
                [f"Anchor {index}", f"Price: ¥{200 + index}"]
            )
        routine_config = RoutineConfig(
            max_concurrency=2,
            retry=RetryConfig(),
            failure_alert_threshold=3,
            diff=DiffConfig(max_sections=2),
        )
        baseline_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(baseline_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        baseline_routine.run(run_id="dropped-1")
        baseline_hash = self.store.states["one"].normalized_hash
        assert baseline_hash

        changed_routine = WeeklyMonitorRoutine(
            store=self.store,
            snapshots=self.snapshots,
            summary_client=NonMaterialSummary(),
            slack=self.slack,
            fetcher=FixtureFetcher({"one": paragraphs_response(changed_paragraphs)}),
            audit_sink=self.store,
            config=routine_config,
            sleeper=lambda _: None,
        )
        result = changed_routine.run(run_id="dropped-2")
        assert result.metrics.failed == 1
        assert self.store.runs["dropped-2:one"].error_code == "truncated_diff_non_material"
        assert baseline_hash == self.store.states["one"].normalized_hash
        assert self.store.states["one"].consecutive_failures == 1


if __name__ == "__main__":
    unittest.main()
