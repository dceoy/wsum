"""Bounded deterministic diffing and material-change scoring."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from errors import MonitorError

PRICE_RE = re.compile(
    r"(?:[$€£¥￥]\s?\d[\d,.]*|\d[\d,.]*\s?(?:円|usd|eur|gbp|jpy)|"
    r"\b(?:price|cost|fee|fare|charge)\b|"
    r"価格|料金|費用|金額)",
    re.IGNORECASE,
)
SPEC_RE = re.compile(
    r"(?:\b(?:specification|specs?|capacity|dimension|weight|version|model)\b|"
    r"性能|仕様|容量|寸法|重量|バージョン|モデル)",
    re.IGNORECASE,
)
TERMS_RE = re.compile(
    r"(?:\b(?:terms?|contract|agreement|policy|privacy|warranty|liability)\b|"
    r"規約|契約|条件|方針|保証|責任)",
    re.IGNORECASE,
)
AVAILABILITY_RE = re.compile(
    r"(?:\b(?:available|unavailable|in stock|out of stock|discontinued)\b|"
    r"受付中|受付終了|在庫|提供終了|販売終了)",
    re.IGNORECASE,
)
ELIGIBILITY_RE = re.compile(
    r"(?:\b(?:eligible|eligibility|requirement|applicant|deadline)\b|"
    r"対象|資格|要件|申請|締切)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"^(?:\W*\d+\W*|(?:updated|modified|published|閲覧|アクセス|更新)"
    r".{0,80}\d{1,4}[-/:年月日].*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DiffConfig:
    minor_threshold: int = 35
    high_threshold: int = 70
    max_diff_chars: int = 12_000
    max_sections: int = 30
    context_lines: int = 1
    max_diff_lines: int = 20_000
    max_diff_complexity: int = 2_000_000
    price_weight: int = 30
    specification_weight: int = 20
    terms_weight: int = 30
    availability_weight: int = 25
    eligibility_weight: int = 25

    def __post_init__(self) -> None:
        if not 0 <= self.minor_threshold < self.high_threshold <= 100:
            raise MonitorError("invalid_configuration", "diff thresholds are invalid")
        if not 1_000 <= self.max_diff_chars <= 100_000:
            raise MonitorError(
                "invalid_configuration", "max_diff_chars must be 1000-100000"
            )
        if not 1 <= self.max_sections <= 100:
            raise MonitorError("invalid_configuration", "max_sections must be 1-100")
        if not 0 <= self.context_lines <= 5:
            raise MonitorError("invalid_configuration", "context_lines must be 0-5")
        if not 1_000 <= self.max_diff_lines <= 200_000:
            raise MonitorError(
                "invalid_configuration", "max_diff_lines must be 1000-200000"
            )
        if not 100_000 <= self.max_diff_complexity <= 50_000_000:
            raise MonitorError(
                "invalid_configuration",
                "max_diff_complexity must be 100000-50000000",
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiffConfig:
        allowed = cls.__dataclass_fields__.keys()
        unknown = set(value) - set(allowed)
        if unknown:
            raise MonitorError(
                "invalid_configuration",
                f"unknown diff configuration: {', '.join(sorted(unknown))}",
            )
        try:
            return cls(**{key: int(raw) for key, raw in value.items()})
        except (TypeError, ValueError) as exc:
            raise MonitorError(
                "invalid_configuration", "diff configuration values must be integers"
            ) from exc


@dataclass(frozen=True, slots=True)
class DiffSection:
    section_id: str
    anchor: str
    kind: str
    context: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["context"] = list(self.context)
        value["before"] = list(self.before)
        value["after"] = list(self.after)
        return value


@dataclass(frozen=True, slots=True)
class DiffResult:
    result: str
    change_score: int
    significance: str
    changed_ratio: float
    sections: tuple[DiffSection, ...]
    truncated: bool
    scoring_reasons: tuple[str, ...]

    @property
    def should_summarize(self) -> bool:
        return self.result == "candidate_material"

    @property
    def should_notify(self) -> bool:
        return False

    @property
    def budget_exceeded(self) -> bool:
        return "diff_budget_exceeded" in self.scoring_reasons

    @property
    def signal_section_truncated(self) -> bool:
        return "material_signal_truncated" in self.scoring_reasons

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["sections"] = [section.as_dict() for section in self.sections]
        value["scoring_reasons"] = list(self.scoring_reasons)
        value["should_summarize"] = self.should_summarize
        value["should_notify"] = self.should_notify
        value["budget_exceeded"] = self.budget_exceeded
        return value


def _anchor(lines: list[str], position: int) -> str:
    for index in range(min(position, len(lines) - 1), -1, -1):
        line = lines[index].strip()
        if line.startswith("#") or line.startswith("ENTRY "):
            return line[:300]
    if 0 <= position < len(lines):
        return lines[position][:300]
    return "document"


def _section_id(
    anchor: str,
    tag: str,
    old_start: int,
    new_start: int,
    before: list[str],
    after: list[str],
) -> str:
    material = json.dumps(
        [anchor, tag, old_start, new_start, before, after],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _changed_char_ratio(
    previous_text: str, current_text: str, sections: list[DiffSection]
) -> float:
    changed = sum(
        max(
            sum(len(line) for line in section.before),
            sum(len(line) for line in section.after),
        )
        for section in sections
    )
    denominator = max(len(previous_text), len(current_text), 1)
    return min(1.0, changed / denominator)


def _section_signal(section: DiffSection) -> bool:
    # The anchor (e.g. a "# Price" heading) is included alongside the
    # changed lines themselves: a label/value layout where only the value
    # changes (`# Price\n10` -> `# Price\n20`) would otherwise lose the
    # label that makes the change material, since the anchor line itself
    # is never truncated, it is always available here.
    joined = "\n".join((*section.before, *section.after, section.anchor))
    return any(
        pattern.search(joined)
        for pattern in (PRICE_RE, SPEC_RE, TERMS_RE, AVAILABILITY_RE, ELIGIBILITY_RE)
    )


def _section_priority(section: DiffSection) -> tuple[bool, int]:
    size = sum(len(line) for line in (*section.before, *section.after))
    return (not _section_signal(section), -size)


def _score(
    ratio: float,
    sections: list[DiffSection],
    config: DiffConfig,
) -> tuple[int, tuple[str, ...]]:
    changed_lines = [
        line
        for section in sections
        for line in (*section.before, *section.after)
        if line.strip()
    ]
    # Include each section's anchor (e.g. a "# Price" heading) alongside the
    # changed lines: a label/value layout where only the value changes
    # (`# Price\n10` -> `# Price\n20`) would otherwise never expose the
    # label that makes the pattern match, since the anchor itself never
    # changes and so is never among before/after.
    anchors = [section.anchor for section in sections if section.anchor]
    joined = "\n".join((*changed_lines, *anchors))
    base = min(60, round(ratio * 60))
    reasons = [f"changed_ratio:{ratio:.4f}"]
    score = base
    patterns = (
        ("price", PRICE_RE, config.price_weight),
        ("specification", SPEC_RE, config.specification_weight),
        ("terms", TERMS_RE, config.terms_weight),
        ("availability", AVAILABILITY_RE, config.availability_weight),
        ("eligibility", ELIGIBILITY_RE, config.eligibility_weight),
    )
    signal_matched = False
    for name, pattern, weight in patterns:
        if pattern.search(joined):
            score += weight
            reasons.append(name)
            signal_matched = True
    if ratio >= 0.65:
        score += 20
        reasons.append("large_rewrite")
    nonempty = [line for line in changed_lines if line.strip()]
    # A matched label/value/terms/etc. pattern means the change is material
    # even if the changed lines alone are noise-shaped (e.g. a bare "10" ->
    # "20"); only clamp when no such signal was found.
    if (
        not signal_matched
        and nonempty
        and all(NOISE_RE.fullmatch(line.strip()) for line in nonempty)
    ):
        score = min(score, 15)
        reasons.append("noise_only")
    return min(100, score), tuple(reasons)


def _bounded_sections(
    sections: list[DiffSection], config: DiffConfig
) -> tuple[tuple[DiffSection, ...], bool, frozenset[str]]:
    output: list[DiffSection] = []
    used = 0
    truncated = False
    partial_ids: set[str] = set()
    for section in sections:
        if len(output) >= config.max_sections:
            truncated = True
            break
        base_size = len(section.anchor) + len(section.section_id) + 100
        remaining = config.max_diff_chars - used - base_size
        if remaining <= 0:
            truncated = True
            break
        before: list[str] = []
        after: list[str] = []
        context: list[str] = []
        # Only a cut inside before/after loses evidence _section_signal()
        # inspects; a cut confined to context is not signal-relevant.
        section_partial = False
        for source, destination, is_signal_bearing in (
            (section.before, before, True),
            (section.after, after, True),
            (section.context, context, False),
        ):
            for line in source:
                line_size = len(line) + 1
                if line_size > remaining:
                    truncated = True
                    if is_signal_bearing:
                        section_partial = True
                    break
                destination.append(line)
                remaining -= line_size
                used += line_size
        if before or after:
            output.append(
                DiffSection(
                    section.section_id,
                    section.anchor,
                    section.kind,
                    tuple(context),
                    tuple(before),
                    tuple(after),
                )
            )
            used += base_size
            if section_partial:
                partial_ids.add(section.section_id)
        if truncated:
            break
    return tuple(output), truncated, frozenset(partial_ids)


def _complexity_budget_exceeded(
    before_lines: list[str], after_lines: list[str], limit: int
) -> bool:
    """Estimate SequenceMatcher's worst-case matching cost before running it.

    ``difflib.SequenceMatcher`` with ``autojunk=False`` does O(count_before(v) *
    count_after(v)) work for each line value ``v`` shared by both sequences.
    A document dominated by a handful of heavily repeated lines (rather than
    simply many lines) can hit this cost well under ``max_diff_lines``, so it
    must be bounded independently of the line-count cap.
    """
    before_counts = Counter(before_lines)
    after_counts = Counter(after_lines)
    smaller, larger = (
        (before_counts, after_counts)
        if len(before_counts) <= len(after_counts)
        else (after_counts, before_counts)
    )
    total = 0
    for value, count in smaller.items():
        other = larger.get(value)
        if not other:
            continue
        total += count * other
        if total > limit:
            return True
    return False


def _budget_exceeded_result(
    before_lines: list[str], after_lines: list[str]
) -> DiffResult:
    section = DiffSection(
        _section_id("document", "replace", 0, 0, [], []),
        "document",
        "modified",
        (),
        (f"{len(before_lines)} lines (diff budget exceeded)",),
        (f"{len(after_lines)} lines (diff budget exceeded)",),
    )
    return DiffResult(
        "candidate_material",
        100,
        "high",
        1.0,
        (section,),
        True,
        ("diff_budget_exceeded",),
    )


def compare_content(
    previous_text: str | None,
    current_text: str,
    *,
    previous_hash: str = "",
    current_hash: str = "",
    config: DiffConfig | None = None,
) -> DiffResult:
    active = config or DiffConfig()
    if previous_text is None:
        return DiffResult(
            "baseline_created", 0, "none", 0.0, (), False, ("first_fetch",)
        )
    if previous_hash and current_hash and previous_hash == current_hash:
        return DiffResult("unchanged", 0, "none", 0.0, (), False, ("equal_hash",))
    if previous_text == current_text:
        return DiffResult("unchanged", 0, "none", 0.0, (), False, ("equal_text",))

    before_lines = previous_text.splitlines()
    after_lines = current_text.splitlines()
    if (
        len(before_lines) > active.max_diff_lines
        or len(after_lines) > active.max_diff_lines
        or _complexity_budget_exceeded(
            before_lines, after_lines, active.max_diff_complexity
        )
    ):
        return _budget_exceeded_result(before_lines, after_lines)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    raw_sections: list[DiffSection] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        context_before = before_lines[
            max(0, old_start - active.context_lines) : old_start
        ]
        context_after = before_lines[
            old_end : min(len(before_lines), old_end + active.context_lines)
        ]
        context = [*context_before, *context_after]
        before = before_lines[old_start:old_end]
        after = after_lines[new_start:new_end]
        anchor = (
            _anchor(before_lines, old_start)
            if tag == "delete"
            else _anchor(after_lines, new_start)
        )
        anchor = anchor or _anchor(before_lines, old_start)
        kind = {"delete": "removed", "insert": "added", "replace": "modified"}[tag]
        raw_sections.append(
            DiffSection(
                _section_id(anchor, tag, old_start, new_start, before, after),
                anchor,
                kind,
                tuple(context),
                tuple(before),
                tuple(after),
            )
        )
    ratio = _changed_char_ratio(previous_text, current_text, raw_sections)
    score, reasons = _score(ratio, raw_sections, active)
    ordered_sections = sorted(raw_sections, key=_section_priority)
    sections, truncated, partial_ids = _bounded_sections(ordered_sections, active)
    if truncated:
        signal_ids = {
            section.section_id for section in raw_sections if _section_signal(section)
        }
        # A signal-bearing section only counts as preserved if it was kept in
        # full; one retained but cut mid-section (partial_ids) may be missing
        # the very content that made it signal-bearing.
        fully_retained_ids = {
            section.section_id for section in sections
        } - partial_ids
        if signal_ids - fully_retained_ids:
            reasons = (*reasons, "material_signal_truncated")
    result = "minor" if score < active.minor_threshold else "candidate_material"
    significance = (
        "minor"
        if score < active.minor_threshold
        else "high"
        if score >= active.high_threshold
        else "moderate"
    )
    return DiffResult(
        result,
        score,
        significance,
        round(ratio, 6),
        sections,
        truncated,
        reasons,
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: diff.py PREVIOUS CURRENT", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as previous_stream:
            previous = previous_stream.read()
        with open(argv[2], encoding="utf-8") as current_stream:
            current = current_stream.read()
        result = compare_content(previous, current)
    except OSError:
        print(json.dumps({"error": {"code": "input_read_failed", "retryable": False}}))
        return 1
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
