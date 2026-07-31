from __future__ import annotations

import unittest
from pathlib import Path

import support  # noqa: F401
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
from models import Target
from retry import RetryConfig
from routine import RoutineConfig, WeeklyMonitorRoutine


def make_target(target_id: str = "one", group: str = "default") -> Target:
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
    return FixtureResponse(
        (
            "<html><body><main><h1>Product</h1>"
            f"<p>Price: ¥{price}</p></main></body></html>"
        ).encode(),
        "text/html",
    )


def many_lines_response(line_count: int) -> FixtureResponse:
    body = "".join(f"<p>Line {index}</p>" for index in range(line_count))
    return FixtureResponse(
        f"<html><body><main><h1>Product</h1>{body}</main></body></html>".encode(),
        "text/html",
    )


def paragraphs_response(paragraphs: list[str]) -> FixtureResponse:
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return FixtureResponse(
        f"<html><body><main><h1>Product</h1>{body}</main></body></html>".encode(),
        "text/html",
    )


class NonMaterialSummary:
    def summarize(self, request):
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
    def setUp(self) -> None:
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
    ):
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
        baseline, first_fetcher = self.run_cycle(response(1000), "run-1")
        self.assertEqual(1, baseline.metrics.baseline)
        self.assertEqual(0, len(self.slack.messages))
        first_hash = self.store.states["one"].normalized_hash
        self.assertTrue(first_hash)

        changed, second_fetcher = self.run_cycle(response(1200), "run-2")
        self.assertEqual(1, changed.metrics.notified)
        self.assertEqual(1, len(self.slack.messages))
        second_hash = self.store.states["one"].normalized_hash
        self.assertNotEqual(first_hash, second_hash)

        unchanged, third_fetcher = self.run_cycle(response(1200), "run-3")
        self.assertEqual(1, unchanged.metrics.unchanged)
        self.assertEqual(1, len(self.slack.messages))
        self.assertEqual(0, self.store.states["one"].consecutive_failures)
        self.assertEqual(3, len(self.store.runs))
        for fetcher in (first_fetcher, second_fetcher, third_fetcher):
            self.assertTrue(fetcher.workspaces)
            self.assertTrue(
                all(not Path(workspace).exists() for workspace in fetcher.workspaces)
            )

    def test_failed_notification_preserves_baseline_and_retries_safely(self) -> None:
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        failing_slack = MemorySlackConnector(["default"])
        failed, _ = self.run_cycle(response(1200), "run-2", slack=failing_slack)
        self.assertEqual(1, failed.metrics.failed)
        self.assertEqual(baseline_hash, self.store.states["one"].normalized_hash)
        self.assertEqual(1, self.store.states["one"].consecutive_failures)
        retried, _ = self.run_cycle(response(1200), "run-3")
        self.assertEqual(1, retried.metrics.notified)
        self.assertNotEqual(baseline_hash, self.store.states["one"].normalized_hash)

    def test_retry_failure_alert_and_recovery(self) -> None:
        transient = MonitorError("fetch_timeout", "fixture timeout", retryable=True)
        for index in range(1, 4):
            result, fetcher = self.run_cycle(
                transient,
                f"run-{index}",
                retry=RetryConfig(max_attempts=3, initial_delay_seconds=0),
            )
            self.assertEqual(1, result.metrics.failed)
            self.assertEqual(3, fetcher.calls["one"])
        self.assertEqual(3, self.store.states["one"].consecutive_failures)
        self.assertEqual(1, len(self.slack.messages))
        recovered, _ = self.run_cycle(response(1000), "run-4")
        self.assertEqual(1, recovered.metrics.baseline)
        self.assertEqual(0, self.store.states["one"].consecutive_failures)

    def test_transient_state_read_failure_does_not_wipe_existing_baseline(
        self,
    ) -> None:
        # get_state fails on the very first call (before the real previous
        # state is ever loaded) but succeeds on a later call, simulating a
        # transient read failure that a naive fix would paper over by
        # persisting the empty placeholder State and destroying the
        # existing baseline.
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        self.assertTrue(baseline_hash)

        class FlakyStateStore(MemoryOperationalStore):
            def __init__(self, targets, states) -> None:
                super().__init__(targets)
                self.states = states
                self.get_state_calls = 0

            def get_state(self, target_id):
                self.get_state_calls += 1
                if self.get_state_calls == 1:
                    raise MonitorError(
                        "state_read_failed", "simulated transient failure"
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
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual(baseline_hash, flaky_store.states["one"].normalized_hash)
        self.assertEqual(1, flaky_store.states["one"].consecutive_failures)
        self.assertEqual("failed", flaky_store.runs["run-2:one"].result)

    def test_persistent_state_read_failure_still_records_run_without_state_write(
        self,
    ) -> None:
        # get_state fails on every call, including _finish_failure's single
        # retry. The run must still complete (not raise out of
        # _process_target) and the real State row must be left completely
        # untouched rather than replaced with the empty placeholder.
        self.run_cycle(response(1000), "run-1")
        baseline_hash = self.store.states["one"].normalized_hash
        self.assertTrue(baseline_hash)

        class AlwaysFailingStateStore(MemoryOperationalStore):
            def __init__(self, targets, states) -> None:
                super().__init__(targets)
                self.states = states

            def get_state(self, target_id):
                raise MonitorError("state_read_failed", "simulated persistent failure")

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
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual(baseline_hash, broken_store.states["one"].normalized_hash)
        self.assertEqual(0, broken_store.states["one"].consecutive_failures)
        self.assertEqual("failed", broken_store.runs["run-2:one"].result)

    def test_one_target_failure_does_not_abort_other_targets(self) -> None:
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
        self.assertEqual(2, result.metrics.checked)
        self.assertEqual(1, result.metrics.baseline)
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual({"good", "bad"}, set(store.states))

    def test_same_run_id_is_idempotent_in_fixture_store(self) -> None:
        self.run_cycle(response(1000), "stable")
        self.assertEqual(1, len(self.store.runs))
        baseline_state = self.store.states["one"]
        second, second_fetcher = self.run_cycle(response(1200), "stable")
        self.assertEqual(1, len(self.store.runs))
        self.assertEqual(1, second.metrics.baseline)
        self.assertEqual(0, second_fetcher.calls["one"])
        self.assertEqual(baseline_state, self.store.states["one"])

    def test_audit_sink_failure_does_not_change_primary_result(self) -> None:
        class FailingAudit:
            def append_audit(self, record) -> None:
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
        self.assertEqual(1, result.metrics.baseline)
        self.assertIn("one", self.store.states)

    def test_outbox_is_a_mutually_exclusive_delivery_backend(self) -> None:
        def outbox_cycle(price: int, run_id: str):
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
        self.assertEqual(1, baseline.metrics.baseline)
        changed = outbox_cycle(1200, "outbox-2")
        self.assertEqual(1, changed.metrics.material)
        self.assertEqual(0, changed.metrics.notified)
        self.assertEqual(1, len(self.store.outbox))
        self.assertEqual("pending", next(iter(self.store.outbox.values())).status)
        with self.assertRaisesRegex(MonitorError, "requires only"):
            WeeklyMonitorRoutine(
                store=self.store,
                snapshots=self.snapshots,
                summary_client=EvidenceSummaryClient(),
                slack=self.slack,
                outbox_store=self.store,
                config=RoutineConfig(delivery_mode="outbox"),
            )

    def test_summary_connector_attempts_are_recorded_with_fetch_attempt(self) -> None:
        self.run_cycle(response(1000), "summary-1")

        class FailingSummary:
            def summarize(self, request):
                del request
                raise MonitorError(
                    "connector_unavailable",
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
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual(3, len(result.runs[0].attempts))
        self.assertEqual(
            ["", "connector_unavailable", "connector_unavailable"],
            [attempt.error_code for attempt in result.runs[0].attempts],
        )

    def test_oversized_diff_fails_closed_instead_of_advancing_baseline(self) -> None:
        class UnreachableSummary:
            def summarize(self, request):
                raise AssertionError(
                    "synthetic budget-exceeded evidence must never reach a "
                    "summary model"
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
        self.assertTrue(baseline_hash)

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
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual(
            "diff_budget_exceeded", self.store.runs["budget-2:one"].error_code
        )
        self.assertEqual(baseline_hash, self.store.states["one"].normalized_hash)
        self.assertEqual(1, self.store.states["one"].consecutive_failures)
        self.assertEqual(0, len(self.slack.messages))

    def test_non_material_over_ordinary_truncation_still_advances_baseline(
        self,
    ) -> None:
        # 40 changed paragraphs with one price change buried among them: this
        # truncates under a small max_sections, but the price section is
        # prioritized into the retained evidence, so a genuine non-material
        # verdict from the model is trusted and the baseline still advances.
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
        self.assertTrue(baseline_hash)

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
        self.assertEqual(1, result.metrics.minor)
        self.assertEqual(
            "non_material", self.store.runs["signal-2:one"].result
        )
        self.assertNotEqual(baseline_hash, self.store.states["one"].normalized_hash)
        self.assertEqual(0, self.store.states["one"].consecutive_failures)

    def test_truncated_signal_sections_with_non_material_verdict_fail_closed(
        self,
    ) -> None:
        # Five separate price changes but only two sections fit the budget:
        # some signal-bearing evidence is unavoidably dropped, so a
        # non-material verdict cannot be trusted to advance the baseline.
        # Anchor paragraphs between each price keep them as distinct diff
        # opcodes instead of collapsing into a single contiguous replace.
        baseline_paragraphs = []
        changed_paragraphs = []
        for index in range(5):
            baseline_paragraphs.append(f"Anchor {index}")
            changed_paragraphs.append(f"Anchor {index}")
            baseline_paragraphs.append(f"Price: ¥{100 + index}")
            changed_paragraphs.append(f"Price: ¥{200 + index}")
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
        self.assertTrue(baseline_hash)

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
        self.assertEqual(1, result.metrics.failed)
        self.assertEqual(
            "truncated_diff_non_material",
            self.store.runs["dropped-2:one"].error_code,
        )
        self.assertEqual(baseline_hash, self.store.states["one"].normalized_hash)
        self.assertEqual(1, self.store.states["one"].consecutive_failures)


if __name__ == "__main__":
    unittest.main()
