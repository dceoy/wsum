"""Fail-closed validation for model-generated change summaries."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any
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


def _require_string(
    value: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        msg = "summary_invalid"
        raise MonitorError(msg, f"{key} must be a string")
    if len(raw) > maximum or (not allow_empty and not raw.strip()):
        msg = "summary_invalid"
        raise MonitorError(
            msg, f"{key} is empty or exceeds {maximum} characters"
        )
    if any(ord(char) < 32 and char not in "\n\t" for char in raw):
        msg = "summary_invalid"
        raise MonitorError(msg, f"{key} contains control characters")
    return raw.strip()


def _validate_url(value: str, expected: str) -> None:
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
    value = section.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        msg = "summary_invalid"
        raise MonitorError(msg, "diff evidence must contain line arrays")
    if not all(isinstance(item, str) for item in value):
        msg = "summary_invalid"
        raise MonitorError(msg, "diff evidence lines must be strings")
    return [" ".join(item.split()) for item in value]


def validate_summary(
    summary: Mapping[str, Any],
    *,
    changed_sections: Sequence[Mapping[str, Any]],
    source_url: str,
    max_notification_chars: int = 1_500,
) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
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
    material = summary.get("material")
    if not isinstance(material, bool):
        msg = "summary_invalid"
        raise MonitorError(msg, "material must be a boolean")
    significance = summary.get("significance")
    if significance not in {"minor", "moderate", "high"}:
        msg = "summary_invalid"
        raise MonitorError(msg, "significance is invalid")
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
    delivery_text = f"{notification}\n{action}"
    if SLACK_CONTROL_RE.search(delivery_text):
        msg = "summary_invalid"
        raise MonitorError(
            msg, "delivery text contains a forbidden Slack control token"
        )
    delivery_urls = URL_RE.findall(delivery_text)
    if any(
        value != source_url and value.rstrip(".,;:!?。、，；：！？") != source_url
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

    section_map: dict[str, Mapping[str, Any]] = {}
    for section in changed_sections:
        section_id = section.get("section_id")
        if not isinstance(section_id, str) or section_id in section_map:
            msg = "summary_invalid"
            raise MonitorError(msg, "diff section ids are invalid")
        section_map[section_id] = section
    evidence = summary.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        msg = "summary_invalid"
        raise MonitorError(msg, "evidence must be an array")
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        msg = "summary_invalid"
        raise MonitorError(msg, "evidence exceeds the item limit")
    if material and not evidence:
        msg = "summary_unsupported"
        raise MonitorError(msg, "material summary has no evidence")

    supported_text_parts: list[str] = []
    validated_evidence: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, Mapping) or set(item) != EVIDENCE_KEYS:
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
            raise MonitorError(
                msg, "evidence must quote before or after text"
            )
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
        supported_text_parts.extend((before, after))
        validated_evidence.append(
            {
                "section_id": section_id,
                "claim_ja": claim,
                "before": before,
                "after": after,
            }
        )
    supported_text = "\n".join(supported_text_parts)
    notification_without_url = notification.replace(source_url, "")
    claimed_facts = set(
        FACT_TOKEN_RE.findall(f"{summary_ja}\n{notification_without_url}")
    )
    evidence_facts = set(FACT_TOKEN_RE.findall(supported_text))
    unsupported_facts = claimed_facts - evidence_facts
    if unsupported_facts:
        msg = "summary_unsupported"
        raise MonitorError(
            msg,
            "summary contains numeric facts absent from cited evidence",
        )
    return {
        "material": material,
        "significance": significance,
        "summary_ja": summary_ja,
        "evidence": validated_evidence,
        "recommended_action_ja": action,
        "notification_text_ja": notification,
        "source_url": returned_url,
    }


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    try:
        with pathlib.Path(argv[1]).open(encoding="utf-8") as summary_stream:
            summary = json.load(summary_stream)
        with pathlib.Path(argv[2]).open(encoding="utf-8") as request_stream:
            request = json.load(request_stream)
        validate_summary(
            summary,
            changed_sections=request["changed_sections"],
            source_url=request["target"]["source_url"],
        )
    except (OSError, KeyError, json.JSONDecodeError, MonitorError) as exc:
        (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "summary_invalid",
                "message": "summary or request JSON is malformed",
                "retryable": False,
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
