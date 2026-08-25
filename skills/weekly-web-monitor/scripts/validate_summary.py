"""Fail-closed validation for model-generated change summaries."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from errors import MonitorError

JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
FACT_TOKEN_RE = re.compile(
    r"(?:[$€£¥￥]\s?\d[\d,.%]*|\d[\d,.]*\s?(?:円|usd|eur|gbp|jpy|%)|"
    r"\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
INJECTION_RE = re.compile(
    r"(?:ignore (?:all |the )?(?:previous|prior) instructions|"
    r"system prompt|developer message|reveal (?:a )?(?:secret|credential)|"
    r"run (?:this )?(?:command|tool)|curl\s+https?://|webhook(?:_url)?|"
    r"以前の指示を無視|システムプロンプト|ツールを実行|秘密.{0,20}表示)",
    re.IGNORECASE,
)
SLACK_CONTROL_RE = re.compile(
    r"(?:<!|<@|<#|@channel\b|@here\b|@everyone\b)", re.IGNORECASE
)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
ALLOWED_KEYS = frozenset(
    {
        "material",
        "significance",
        "summary_ja",
        "evidence",
        "recommended_action_ja",
        "notification_text_ja",
        "source_url",
    }
)
EVIDENCE_KEYS = frozenset({"section_id", "claim_ja", "before", "after"})
MAX_EVIDENCE_ITEMS = 30

_MIN_PRINTABLE_CODEPOINT = 32


def _require_string(
    value: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    """Read and validate a required (or optionally empty) string field.

    Returns:
        The stripped string value.

    Raises:
        MonitorError: If the field is not a string, is empty when
            required, exceeds ``maximum`` characters, or contains control
            characters other than newline/tab.
    """
    raw = value.get(key)
    if not isinstance(raw, str):
        msg = "summary_invalid"
        raise MonitorError(msg, f"{key} must be a string")
    if len(raw) > maximum or (not allow_empty and not raw.strip()):
        msg = "summary_invalid"
        raise MonitorError(
            msg, f"{key} is empty or exceeds {maximum} characters"
        )
    if any(
        ord(char) < _MIN_PRINTABLE_CODEPOINT and char not in "\n\t" for char in raw
    ):
        msg = "summary_invalid"
        raise MonitorError(msg, f"{key} contains control characters")
    return raw.strip()


def _validate_url(value: str, expected: str) -> None:
    """Validate that ``value`` is a safe HTTP(S) URL matching ``expected``.

    Raises:
        MonitorError: If ``value`` is malformed, not HTTP(S), embeds
            credentials, or does not equal ``expected``.
    """
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        msg = "summary_invalid_url"
        raise MonitorError(msg, "source_url is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or value != expected
    ):
        msg = "summary_invalid_url"
        raise MonitorError(
            msg, "source_url does not match the monitored source"
        )


def _normalized_evidence_lines(section: Mapping[str, Any], key: str) -> list[str]:
    """Return ``section[key]``'s lines with internal whitespace collapsed.

    Returns:
        The whitespace-normalized lines.

    Raises:
        MonitorError: If ``section[key]`` is not an array of strings.
    """
    value = section.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        msg = "summary_invalid"
        raise MonitorError(msg, "diff evidence must contain line arrays")
    value = cast("Sequence[object]", value)
    if not all(isinstance(item, str) for item in value):
        msg = "summary_invalid"
        raise MonitorError(msg, "diff evidence lines must be strings")
    str_lines = cast("Sequence[str]", value)
    return [" ".join(item.split()) for item in str_lines]


def _validate_schema_keys(summary: Mapping[str, Any]) -> None:
    """Validate that ``summary`` has exactly the allowed top-level keys.

    Raises:
        MonitorError: If ``summary`` is not a mapping, or has unknown or
            missing keys.
    """
    if not isinstance(
        summary, Mapping
    ):  # pyright: ignore[reportUnnecessaryIsInstance]
        # summary ultimately originates from untrusted, dynamically-typed
        # model/JSON output; callers may pass a non-mapping value at
        # runtime despite the declared type, so this check stays
        # load-bearing.
        msg = "summary_invalid"
        raise MonitorError(msg, "summary must be an object")
    unknown = set(summary) - ALLOWED_KEYS
    missing = ALLOWED_KEYS - set(summary)
    if unknown or missing:
        msg = "summary_invalid"
        raise MonitorError(
            msg,
            "summary fields do not match the required schema",
            details={"missing": sorted(missing), "unknown": sorted(unknown)},
        )


def _validate_material_and_significance(
    summary: Mapping[str, Any],
) -> tuple[bool, str]:
    """Validate and return ``summary``'s material flag and significance.

    Returns:
        A (material, significance) tuple.

    Raises:
        MonitorError: If either field is missing or invalid.
    """
    material = summary.get("material")
    if not isinstance(material, bool):
        msg = "summary_invalid"
        raise MonitorError(msg, "material must be a boolean")
    significance = summary.get("significance")
    if significance not in {"minor", "moderate", "high"}:
        msg = "summary_invalid"
        raise MonitorError(msg, "significance is invalid")
    return material, significance


def _validate_text_fields(
    summary: Mapping[str, Any],
    *,
    material: bool,
    source_url: str,
    max_notification_chars: int,
) -> tuple[str, str, str, str]:
    """Validate the summary's Japanese text fields and source_url.

    Returns:
        A (summary_ja, recommended_action_ja, notification_text_ja,
        source_url) tuple.

    Raises:
        MonitorError: If any field is malformed, reproduces an
            instruction-like payload, or lacks required Japanese text.
    """
    summary_ja = _require_string(summary, "summary_ja", maximum=500)
    action = _require_string(
        summary, "recommended_action_ja", maximum=300, allow_empty=True
    )
    notification = _require_string(
        summary,
        "notification_text_ja",
        maximum=max_notification_chars,
        allow_empty=not material,
    )
    returned_url = _require_string(summary, "source_url", maximum=4_096)
    _validate_url(returned_url, source_url)

    for field_name, value in (
        ("summary_ja", summary_ja),
        ("recommended_action_ja", action),
        ("notification_text_ja", notification),
    ):
        if value and INJECTION_RE.search(value):
            msg = "summary_prompt_injection"
            raise MonitorError(
                msg,
                f"{field_name} reproduces an instruction-like payload",
            )
    if not JAPANESE_RE.search(summary_ja):
        msg = "summary_invalid"
        raise MonitorError(msg, "summary_ja must contain Japanese text")
    if action and not JAPANESE_RE.search(action):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "recommended_action_ja must contain Japanese text"
        )
    if material and (
        not JAPANESE_RE.search(notification) or source_url not in notification
    ):
        msg = "summary_invalid"
        raise MonitorError(
            msg,
            "material notification must be Japanese and include the source URL",
        )
    return summary_ja, action, notification, returned_url


def _validate_delivery_text(
    notification: str, action: str, source_url: str, *, material: bool
) -> None:
    """Validate the Slack-delivered text for control tokens and stray URLs.

    Raises:
        MonitorError: If the delivery text contains a Slack control token,
            an unsupported external URL, or non-material notification
            text.
    """
    delivery_text = f"{notification}\n{action}"
    if SLACK_CONTROL_RE.search(delivery_text):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "delivery text contains a forbidden Slack control token"
        )
    delivery_urls = URL_RE.findall(delivery_text)
    # Trailing Japanese full-width punctuation (the full-width forms of
    # . , ; : ! ?, plus the ideographic full stop and comma) is
    # deliberately stripped here, not mistaken for its ASCII look-alike:
    # Japanese prose commonly follows a URL directly with sentence-final
    # punctuation with no separating space (e.g.
    # "https://example.com<ideographic full stop>"), and that punctuation
    # is not part of the URL.
    if any(
        value != source_url
        and value.rstrip(".,;:!?。、，；：！？") != source_url  # ruff: ignore[ambiguous-unicode-character-string]
        for value in delivery_urls
    ):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "delivery text contains an unsupported external URL"
        )
    if not material and notification:
        msg = "summary_invalid"
        raise MonitorError(
            msg,
            "non-material summaries must not provide notification text",
        )


def _build_section_map(
    changed_sections: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Index ``changed_sections`` by their (unique) section_id.

    Returns:
        A mapping of section_id to section.

    Raises:
        MonitorError: If any section_id is missing, not a string, or
            duplicated.
    """
    section_map: dict[str, Mapping[str, Any]] = {}
    for section in changed_sections:
        section_id = section.get("section_id")
        if not isinstance(section_id, str) or section_id in section_map:
            msg = "summary_invalid"
            raise MonitorError(msg, "diff section ids are invalid")
        section_map[section_id] = section
    return section_map


def _validate_evidence_shape(
    summary: Mapping[str, Any], *, material: bool
) -> Sequence[object]:
    """Validate the shape of ``summary``'s evidence array.

    Returns:
        The (still per-item unvalidated) evidence sequence.

    Raises:
        MonitorError: If evidence is not an array, exceeds the item
            limit, or is empty for a material summary.
    """
    evidence = summary.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        msg = "summary_invalid"
        raise MonitorError(msg, "evidence must be an array")
    evidence = cast("Sequence[object]", evidence)
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        msg = "summary_invalid"
        raise MonitorError(msg, "evidence exceeds the item limit")
    if material and not evidence:
        msg = "summary_unsupported"
        raise MonitorError(msg, "material summary has no evidence")
    return evidence


def _validate_one_evidence_item(
    item: object, section_map: Mapping[str, Mapping[str, Any]]
) -> tuple[str, str, str, str]:
    """Validate one evidence item against its referenced diff section.

    Returns:
        A (section_id, claim_ja, before, after) tuple of its validated
        fields.

    Raises:
        MonitorError: If the item's shape, references, or quoted text are
            invalid.
    """
    if not isinstance(item, Mapping):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "evidence fields do not match the required schema"
        )
    item = cast("Mapping[str, Any]", item)
    if frozenset(item) != EVIDENCE_KEYS:
        msg = "summary_invalid"
        raise MonitorError(
            msg, "evidence fields do not match the required schema"
        )
    section_id = _require_string(item, "section_id", maximum=64)
    claim = _require_string(item, "claim_ja", maximum=300)
    before = _require_string(item, "before", maximum=500, allow_empty=True)
    after = _require_string(item, "after", maximum=500, allow_empty=True)
    section = section_map.get(section_id)
    if section is None:
        msg = "summary_unsupported"
        raise MonitorError(
            msg, "evidence references an unknown diff section"
        )
    if not JAPANESE_RE.search(claim):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "evidence claim_ja must contain Japanese text"
        )
    allowed_before = _normalized_evidence_lines(section, "before")
    allowed_after = _normalized_evidence_lines(section, "after")
    normalized_before = " ".join(before.split())
    normalized_after = " ".join(after.split())
    if not normalized_before and not normalized_after:
        msg = "summary_unsupported"
        raise MonitorError(msg, "evidence must quote before or after text")
    if (
        normalized_before
        and normalized_after
        and normalized_before == normalized_after
    ):
        msg = "summary_unsupported"
        raise MonitorError(
            msg, "before and after evidence must show a change"
        )
    if normalized_before and not any(
        normalized_before in line for line in allowed_before
    ):
        msg = "summary_unsupported"
        raise MonitorError(
            msg, "before evidence is absent from its diff section"
        )
    if normalized_after and not any(
        normalized_after in line for line in allowed_after
    ):
        msg = "summary_unsupported"
        raise MonitorError(
            msg, "after evidence is absent from its diff section"
        )
    return section_id, claim, before, after


def _validate_evidence_items(
    evidence: Sequence[object], section_map: Mapping[str, Mapping[str, Any]]
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate every evidence item and collect its quoted text.

    Returns:
        A (supported text parts, validated evidence dicts) tuple.
    """
    supported_text_parts: list[str] = []
    validated_evidence: list[dict[str, str]] = []
    for item in evidence:
        section_id, claim, before, after = _validate_one_evidence_item(
            item, section_map
        )
        supported_text_parts.extend((before, after))
        validated_evidence.append(
            {
                "section_id": section_id,
                "claim_ja": claim,
                "before": before,
                "after": after,
            }
        )
    return supported_text_parts, validated_evidence


def _check_unsupported_facts(
    summary_ja: str, notification: str, source_url: str, supported_text: str
) -> None:
    """Deny numeric/currency facts claimed but not present in cited evidence.

    Raises:
        MonitorError: If the summary claims a numeric fact absent from the
            evidence's quoted before/after text.
    """
    notification_without_url = notification.replace(source_url, "")
    claimed_facts = set(
        FACT_TOKEN_RE.findall(f"{summary_ja}\n{notification_without_url}")
    )
    evidence_facts = set(FACT_TOKEN_RE.findall(supported_text))
    if claimed_facts - evidence_facts:
        msg = "summary_unsupported"
        raise MonitorError(
            msg,
            "summary contains numeric facts absent from cited evidence",
        )


def validate_summary(
    summary: Mapping[str, Any],
    *,
    changed_sections: Sequence[Mapping[str, Any]],
    source_url: str,
    max_notification_chars: int = 1_500,
) -> dict[str, Any]:
    """Fail-closed validate a model-generated change summary.

    Denies (raising ``MonitorError`` from the called validation helpers)
    if any part of the summary or its evidence fails validation.

    Returns:
        The validated summary, with every field re-derived from validated
        inputs rather than passed through verbatim.
    """
    _validate_schema_keys(summary)
    material, significance = _validate_material_and_significance(summary)
    summary_ja, action, notification, returned_url = _validate_text_fields(
        summary,
        material=material,
        source_url=source_url,
        max_notification_chars=max_notification_chars,
    )
    _validate_delivery_text(notification, action, source_url, material=material)

    section_map = _build_section_map(changed_sections)
    evidence = _validate_evidence_shape(summary, material=material)
    supported_text_parts, validated_evidence = _validate_evidence_items(
        evidence, section_map
    )
    supported_text = "\n".join(supported_text_parts)
    _check_unsupported_facts(summary_ja, notification, source_url, supported_text)
    return {
        "material": material,
        "significance": significance,
        "summary_ja": summary_ja,
        "evidence": validated_evidence,
        "recommended_action_ja": action,
        "notification_text_ja": notification,
        "source_url": returned_url,
    }


_EXPECTED_ARGC = 3


def _main(argv: list[str]) -> int:
    """Run the CLI entry point: validate the summary named in ``argv[1]``.

    On success, writes the JSON-encoded validated summary to stdout. On a
    handled failure, writes ``{"error": ...}`` JSON to stdout instead.
    Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if the input files or summary are invalid, 2 for
        incorrect CLI usage.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write(
            "usage: validate_summary.py SUMMARY_JSON REQUEST_JSON\n"
        )
        return 2
    try:
        with pathlib.Path(argv[1]).open(encoding="utf-8") as summary_stream:
            summary = json.load(summary_stream)
        with pathlib.Path(argv[2]).open(encoding="utf-8") as request_stream:
            request = json.load(request_stream)
        validated = validate_summary(
            summary,
            changed_sections=request["changed_sections"],
            source_url=request["target"]["source_url"],
        )
    except (OSError, KeyError, json.JSONDecodeError, MonitorError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "summary_invalid",
                "message": "summary or request JSON is malformed",
                "retryable": False,
            }
        )
        json.dump({"error": error}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    json.dump(validated, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
