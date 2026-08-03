"""Replay stored normalized snapshots and summaries without network access."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from diff import DiffConfig, compare_content
from errors import MonitorError
from normalize import NORMALIZATION_VERSION, hash_normalized
from validate_summary import validate_summary


def replay_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) - {
        "previous",
        "current",
        "diff_config",
        "expected",
        "summary",
        "source_url",
    }:
        raise MonitorError("replay_invalid", "replay manifest has unknown fields")
    previous = value.get("previous")
    current = value.get("current")
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        raise MonitorError(
            "replay_invalid", "previous and current snapshots are required"
        )
    for name, snapshot in (("previous", previous), ("current", current)):
        if snapshot.get("normalization_version") != NORMALIZATION_VERSION:
            raise MonitorError(
                "replay_version_mismatch",
                f"{name} normalization version is unsupported",
            )
        kind = snapshot.get("kind")
        text = snapshot.get("text")
        expected_hash = snapshot.get("normalized_hash")
        if not isinstance(kind, str) or not isinstance(text, str):
            raise MonitorError("replay_invalid", f"{name} snapshot is malformed")
        actual_hash = hash_normalized(kind, text)
        if actual_hash != expected_hash:
            raise MonitorError(
                "replay_hash_mismatch", f"{name} normalized hash does not match"
            )
    config_value = value.get("diff_config", {})
    if not isinstance(config_value, Mapping):
        raise MonitorError("replay_invalid", "diff_config must be an object")
    diff = compare_content(
        str(previous["text"]),
        str(current["text"]),
        previous_hash=str(previous["normalized_hash"]),
        current_hash=str(current["normalized_hash"]),
        config=DiffConfig.from_mapping(config_value),
    )
    expected = value.get("expected", {})
    if expected:
        if not isinstance(expected, Mapping):
            raise MonitorError("replay_invalid", "expected must be an object")
        for key in ("result", "change_score", "significance"):
            if key in expected and diff.as_dict()[key] != expected[key]:
                raise MonitorError(
                    "replay_result_mismatch", f"replayed {key} does not match"
                )
    summary_result: dict[str, Any] | None = None
    if "summary" in value:
        summary = value["summary"]
        if not isinstance(summary, Mapping):
            raise MonitorError("replay_invalid", "summary must be an object")
        source_url = value.get("source_url")
        if not isinstance(source_url, str):
            raise MonitorError(
                "replay_invalid", "independent replay source_url is missing"
            )
        summary_result = validate_summary(
            summary,
            changed_sections=[section.as_dict() for section in diff.sections],
            source_url=source_url,
        )
    return {
        "hashes_valid": True,
        "diff": diff.as_dict(),
        "summary_valid": summary_result is not None,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: replay.py MANIFEST_JSON", file=sys.stderr)
        return 2
    try:
        value = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        result = replay_manifest(value)
    except (OSError, json.JSONDecodeError, MonitorError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "replay_invalid",
                "message": "replay manifest could not be read",
                "retryable": False,
            }
        )
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
