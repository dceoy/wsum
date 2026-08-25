"""Run-level counters that never include fetched or model-generated content."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from models import RunRecord


@dataclass(frozen=True, slots=True)
class RunMetrics:
    checked: int = 0
    unchanged: int = 0
    baseline: int = 0
    minor: int = 0
    material: int = 0
    notified: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "unchanged": self.unchanged,
            "baseline": self.baseline,
            "minor": self.minor,
            "material": self.material,
            "notified": self.notified,
            "failed": self.failed,
        }


def calculate_metrics(runs: Iterable[RunRecord]) -> RunMetrics:
    records = list(runs)
    return RunMetrics(
        checked=len(records),
        unchanged=sum(run.result == "unchanged" for run in records),
        baseline=sum(run.result == "baseline_created" for run in records),
        minor=sum(run.result in {"minor", "non_material"} for run in records),
        material=sum(run.result in {"material", "notified"} for run in records),
        notified=sum(run.result == "notified" for run in records),
        failed=sum(run.result == "failed" for run in records),
    )
