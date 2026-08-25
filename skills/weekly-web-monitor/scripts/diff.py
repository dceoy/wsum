"""Bounded deterministic diffing and material-change scoring."""

from __future__ import annotations

import difflib
import hashlib
import json
import pathlib
import re
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from errors import MonitorError

if TYPE_CHECKING:
    from collections.abc import Mapping

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

_MIN_THRESHOLD = 0
_MAX_THRESHOLD = 100
_MIN_MAX_DIFF_CHARS = 1_000
_MAX_MAX_DIFF_CHARS = 100_000
_MIN_MAX_SECTIONS = 1
_MAX_MAX_SECTIONS = 100
_MIN_CONTEXT_LINES = 0
_MAX_CONTEXT_LINES = 5
_MIN_MAX_DIFF_LINES = 1_000
_MAX_MAX_DIFF_LINES = 200_000
_MIN_MAX_DIFF_COMPLEXITY = 100_000
_MAX_MAX_DIFF_COMPLEXITY = 50_000_000
_MIN_FOCUS_TERM_LENGTH = 2
_SCORE_RATIO_BASE_CAP = 60
_LARGE_REWRITE_RATIO = 0.65
_LARGE_REWRITE_BONUS = 20
_NOISE_ONLY_SCORE_CAP = 15
_MAX_SCORE = 100
_SECTION_ID_BASE_OVERHEAD = 100


@dataclass(frozen=True, slots=True)
class DiffConfig:
    """Bounds and scoring weights for :func:`compare_content`."""

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
        """Validate that every bound falls within its allowed range.

        Raises:
            MonitorError: If any field is outside its allowed range.
        """
        if not (
            _MIN_THRESHOLD
            <= self.minor_threshold
            < self.high_threshold
            <= _MAX_THRESHOLD
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "diff thresholds are invalid")
        if not _MIN_MAX_DIFF_CHARS <= self.max_diff_chars <= _MAX_MAX_DIFF_CHARS:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "max_diff_chars must be 1000-100000"
            )
        if not _MIN_MAX_SECTIONS <= self.max_sections <= _MAX_MAX_SECTIONS:
            msg = "invalid_configuration"
            raise MonitorError(msg, "max_sections must be 1-100")
        if not _MIN_CONTEXT_LINES <= self.context_lines <= _MAX_CONTEXT_LINES:
            msg = "invalid_configuration"
            raise MonitorError(msg, "context_lines must be 0-5")
        if not _MIN_MAX_DIFF_LINES <= self.max_diff_lines <= _MAX_MAX_DIFF_LINES:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "max_diff_lines must be 1000-200000"
            )
        if not (
            _MIN_MAX_DIFF_COMPLEXITY
            <= self.max_diff_complexity
            <= _MAX_MAX_DIFF_COMPLEXITY
        ):
            msg = "invalid_configuration"
            raise MonitorError(
                msg,
                "max_diff_complexity must be 100000-50000000",
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiffConfig:
        """Build a validated config from a raw mapping (e.g. sheet JSON).

        Returns:
            The constructed, validated config.

        Raises:
            MonitorError: If ``value`` has unknown keys or non-integer
                values.
        """
        allowed = cls.__dataclass_fields__.keys()
        unknown = set(value) - set(allowed)
        if unknown:
            msg = "invalid_configuration"
            raise MonitorError(
                msg,
                f"unknown diff configuration: {', '.join(sorted(unknown))}",
            )
        try:
            return cls(**{key: int(raw) for key, raw in value.items()})
        except (TypeError, ValueError) as exc:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "diff configuration values must be integers"
            ) from exc


@dataclass(frozen=True, slots=True)
class DiffSection:
    """One changed region of a diff, with bounded before/after/context."""

    section_id: str
    anchor: str
    kind: str
    context: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this section."""
        value = asdict(self)
        value["context"] = list(self.context)
        value["before"] = list(self.before)
        value["after"] = list(self.after)
        return value


@dataclass(frozen=True, slots=True)
class DiffResult:
    """The deterministic outcome of comparing a target's previous/current content."""

    result: str
    change_score: int
    significance: str
    changed_ratio: float
    sections: tuple[DiffSection, ...]
    truncated: bool
    scoring_reasons: tuple[str, ...]

    @property
    def should_summarize(self) -> bool:
        """Whether this result is a candidate for model summarization."""
        return self.result == "candidate_material"

    @property
    def should_notify(self) -> bool:
        """Whether this deterministic result alone should trigger a notification.

        Always ``False``: notification is decided only after a validated
        model summary marks the diff material, never from the diff alone.
        """
        return False

    @property
    def budget_exceeded(self) -> bool:
        """Whether the diff exceeded its complexity/size budget and was clamped."""
        return "diff_budget_exceeded" in self.scoring_reasons

    @property
    def signal_section_truncated(self) -> bool:
        """Whether a signal-bearing section was truncated below the size limit."""
        return "material_signal_truncated" in self.scoring_reasons

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this result."""
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
        if line.startswith(("#", "ENTRY ")):
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


CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]")


def _watch_focus_terms(watch_focus: str) -> tuple[re.Pattern[str], ...]:
    """Compile ``watch_focus`` into per-term match patterns.

    Returns:
        One compiled pattern per non-empty whitespace-separated term.
    """
    patterns: list[re.Pattern[str]] = []
    for term in re.split(r"\s+", watch_focus.strip()):
        if not term:
            continue
        if CJK_RE.search(term):
            # \b is not a reliable tokenizer for CJK text (there is no
            # whitespace between words), and CJK focus terms are commonly
            # only one or two characters, so match the literal substring
            # unbounded instead of requiring a word boundary and length > 2.
            patterns.append(re.compile(re.escape(term), re.IGNORECASE))
        elif len(term) > _MIN_FOCUS_TERM_LENGTH:
            patterns.append(re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
        elif term.isupper() and term.isalnum():
            # Short Latin terms are usually accidental noise, but an
            # all-uppercase token (``AI``, ``HR``, ``EV``, ``3D``) is
            # deliberately an acronym, not a word fragment, so it is matched
            # case-sensitively rather than discarded outright.
            patterns.append(re.compile(r"\b" + re.escape(term) + r"\b"))
    return tuple(patterns)


def _watch_focus_matches_section(
    section: DiffSection, terms: tuple[re.Pattern[str], ...]
) -> bool:
    joined = "\n".join((*section.before, *section.after, section.anchor))
    return any(term.search(joined) for term in terms)


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
    base = min(_SCORE_RATIO_BASE_CAP, round(ratio * _SCORE_RATIO_BASE_CAP))
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
    if ratio >= _LARGE_REWRITE_RATIO:
        score += _LARGE_REWRITE_BONUS
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
        score = min(score, _NOISE_ONLY_SCORE_CAP)
        reasons.append("noise_only")
    return min(_MAX_SCORE, score), tuple(reasons)


def _fit_section_lines(
    section: DiffSection, remaining: int
) -> tuple[list[str], list[str], list[str], int, bool, bool]:
    """Fit as much of ``section``'s lines as possible within ``remaining`` chars.

    Only a cut inside before/after loses evidence :func:`_section_signal`
    inspects; a cut confined to context is not signal-relevant.

    Returns:
        A tuple of (kept before, kept after, kept context, chars consumed,
        whether any line was cut, whether a signal-bearing line was cut).
    """
    before: list[str] = []
    after: list[str] = []
    context: list[str] = []
    consumed = 0
    truncated = False
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
            consumed += line_size
    return before, after, context, consumed, truncated, section_partial


def _bounded_sections(
    sections: list[DiffSection], config: DiffConfig
) -> tuple[tuple[DiffSection, ...], bool, frozenset[str]]:
    """Bound ``sections`` to the configured count/character budget.

    Returns:
        A tuple of (kept sections, whether anything was truncated, the
        section IDs that were kept but had a signal-bearing line cut).
    """
    output: list[DiffSection] = []
    used = 0
    truncated = False
    partial_ids: set[str] = set()
    for section in sections:
        if len(output) >= config.max_sections:
            truncated = True
            break
        base_size = (
            len(section.anchor) + len(section.section_id) + _SECTION_ID_BASE_OVERHEAD
        )
        remaining = config.max_diff_chars - used - base_size
        if remaining <= 0:
            truncated = True
            break
        before, after, context, consumed, section_truncated, section_partial = (
            _fit_section_lines(section, remaining)
        )
        truncated = truncated or section_truncated
        used += consumed
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
    """Conservatively bound SequenceMatcher's quadratic worst case.

    Frequency-based estimates miss adversarial permutations whose lines are
    unique.  The sequence-length product is a safe upper-work proxy for both
    repeated-line and unique-line worst cases, so fail closed before invoking
    ``SequenceMatcher`` whenever that product exceeds the configured budget.

    Returns:
        Whether the line-count product exceeds ``limit``.
    """
    return len(before_lines) * len(after_lines) > limit


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
        result="candidate_material",
        change_score=100,
        significance="high",
        changed_ratio=1.0,
        sections=(section,),
        truncated=True,
        scoring_reasons=("diff_budget_exceeded",),
    )


def _build_raw_sections(
    before_lines: list[str],
    after_lines: list[str],
    matcher: difflib.SequenceMatcher[str],
    context_lines: int,
) -> list[DiffSection]:
    """Build one DiffSection per non-equal opcode from the sequence matcher.

    Returns:
        The unbounded, unordered list of changed sections.
    """
    raw_sections: list[DiffSection] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        context_before = before_lines[max(0, old_start - context_lines) : old_start]
        context_after = before_lines[
            old_end : min(len(before_lines), old_end + context_lines)
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
    return raw_sections


def _check_signal_truncation(
    raw_sections: list[DiffSection],
    sections: tuple[DiffSection, ...],
    partial_ids: frozenset[str],
    reasons: tuple[str, ...],
) -> tuple[str, ...]:
    """Append "material_signal_truncated" if a kept section lost its signal.

    Returns:
        ``reasons``, with the truncation reason appended if applicable.
    """
    signal_ids = {
        section.section_id for section in raw_sections if _section_signal(section)
    }
    # A signal-bearing section only counts as preserved if it was kept in
    # full; one retained but cut mid-section (partial_ids) may be missing
    # the very content that made it signal-bearing.
    fully_retained_ids = {section.section_id for section in sections} - partial_ids
    if signal_ids - fully_retained_ids:
        return (*reasons, "material_signal_truncated")
    return reasons


def _apply_watch_focus_override(
    result: str,
    reasons: tuple[str, ...],
    watch_focus: str,
    raw_sections: list[DiffSection],
) -> tuple[str, tuple[str, ...]]:
    """Override a "minor" result to "candidate_material" per watch_focus.

    Returns:
        The (possibly overridden) result and reasons.
    """
    if result != "minor" or not watch_focus.strip():
        return result, reasons
    if "noise_only" not in reasons:
        # The deterministic score/pattern gate has no notion of a target's
        # configured watch_focus (e.g. "executive changes"), which rarely
        # matches the fixed price/spec/terms/availability/eligibility
        # patterns above. Rather than silently discard a non-noise change
        # on a focused target, always let the summary model -- which does
        # see watch_focus -- assess it.
        return "candidate_material", (*reasons, "watch_focus_configured")
    # A change clamped as pure noise (e.g. a bare "last updated" date) must
    # not be forced to candidate_material just because *any* focus is
    # configured -- that would defeat the noise clamp entirely. But a focus
    # that names the specific value being watched (e.g.
    # watch_focus="valuation" on a standalone "# Valuation\n10" -> "20")
    # must still reach the summary model: match the focus terms against the
    # section text/anchor deterministically and only override the clamp on
    # a hit.
    terms = _watch_focus_terms(watch_focus)
    if terms and any(
        _watch_focus_matches_section(section, terms) for section in raw_sections
    ):
        return "candidate_material", (*reasons, "watch_focus_configured")
    return result, reasons


def compare_content(
    previous_text: str | None,
    current_text: str,
    *,
    previous_hash: str = "",
    current_hash: str = "",
    config: DiffConfig | None = None,
    watch_focus: str = "",
) -> DiffResult:
    """Deterministically diff and score previous vs. current content.

    Returns:
        The scored, bounded diff result.
    """
    active = config or DiffConfig()
    if previous_text is None:
        return DiffResult(
            result="baseline_created",
            change_score=0,
            significance="none",
            changed_ratio=0.0,
            sections=(),
            truncated=False,
            scoring_reasons=("first_fetch",),
        )
    if previous_hash and current_hash and previous_hash == current_hash:
        return DiffResult(
            result="unchanged",
            change_score=0,
            significance="none",
            changed_ratio=0.0,
            sections=(),
            truncated=False,
            scoring_reasons=("equal_hash",),
        )
    if previous_text == current_text:
        return DiffResult(
            result="unchanged",
            change_score=0,
            significance="none",
            changed_ratio=0.0,
            sections=(),
            truncated=False,
            scoring_reasons=("equal_text",),
        )

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
    raw_sections = _build_raw_sections(
        before_lines, after_lines, matcher, active.context_lines
    )
    ratio = _changed_char_ratio(previous_text, current_text, raw_sections)
    score, reasons = _score(ratio, raw_sections, active)
    ordered_sections = sorted(raw_sections, key=_section_priority)
    sections, truncated, partial_ids = _bounded_sections(ordered_sections, active)
    if truncated:
        reasons = _check_signal_truncation(raw_sections, sections, partial_ids, reasons)
    result = "minor" if score < active.minor_threshold else "candidate_material"
    result, reasons = _apply_watch_focus_override(
        result, reasons, watch_focus, raw_sections
    )
    significance = (
        "minor"
        if result == "minor"
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


_EXPECTED_ARGC = 3


def _main(argv: list[str]) -> int:
    """Run the CLI entry point: diff the two files named in ``argv[1:3]``.

    On success, writes the JSON-encoded :class:`DiffResult` to stdout. On a
    read failure, writes ``{"error": ...}`` JSON to stdout instead.
    Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if a file could not be read, 2 for incorrect CLI
        usage.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write("usage: diff.py PREVIOUS CURRENT\n")
        return 2
    try:
        previous = pathlib.Path(argv[1]).read_text(encoding="utf-8")
        current = pathlib.Path(argv[2]).read_text(encoding="utf-8")
        result = compare_content(previous, current)
    except OSError:
        json.dump(
            {"error": {"code": "input_read_failed", "retryable": False}}, sys.stdout
        )
        sys.stdout.write("\n")
        return 1
    json.dump(result.as_dict(), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
