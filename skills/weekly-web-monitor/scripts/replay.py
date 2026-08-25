"""Replay stored normalized snapshots and summaries without network access."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from diff import DiffConfig, DiffResult, compare_content
from errors import MonitorError
from normalize import NORMALIZATION_VERSION, hash_normalized
from validate_summary import validate_summary

_EXPECTED_ARGC = 2


def _validate_snapshot(name: str, snapshot: Mapping[str, Any]) -> None:
    """Validate one manifest snapshot's normalization version and hash.

    Raises:
        MonitorError: If the snapshot's version, shape, or hash is invalid.
    """
    if snapshot.get("normalization_version") != NORMALIZATION_VERSION:
        msg = "replay_version_mismatch"
        raise MonitorError(
            msg,
            f"{name} normalization version is unsupported",
        )
    kind = snapshot.get("kind")
    text = snapshot.get("text")
    expected_hash = snapshot.get("normalized_hash")
    if not isinstance(kind, str) or not isinstance(text, str):
        msg = "replay_invalid"
        raise MonitorError(msg, f"{name} snapshot is malformed")
    actual_hash = hash_normalized(kind, text)
    if actual_hash != expected_hash:
        msg = "replay_hash_mismatch"
        raise MonitorError(
            msg, f"{name} normalized hash does not match"
        )


def _check_expected(expected: object, diff: DiffResult) -> None:
    """Check a replayed diff against an optional ``expected`` manifest field.

    Raises:
        MonitorError: If ``expected`` is malformed or a replayed field
            diverges from its expected value.
    """
    if not expected:
        return
    if not isinstance(expected, Mapping):
        msg = "replay_invalid"
        raise MonitorError(msg, "expected must be an object")
    for key in ("result", "change_score", "significance"):
        if key in expected and diff.as_dict()[key] != expected[key]:
            msg = "replay_result_mismatch"
            raise MonitorError(
                msg, f"replayed {key} does not match"
            )


def replay_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a prior run's diff and summary validity without network access.

    Returns:
        A summary of the replay containing ``hashes_valid``, the recomputed
        ``diff``, and whether the stored ``summary`` (if any) is valid.

    Raises:
        MonitorError: If ``value`` is malformed, its snapshots' normalization
            version or hash does not match, or the replayed result diverges
            from an ``expected`` outcome embedded in ``value``.
    """
    if set(value) - {
        "previous",
        "current",
        "diff_config",
        "expected",
        "summary",
        "source_url",
    }:
        msg = "replay_invalid"
        raise MonitorError(msg, "replay manifest has unknown fields")
    previous = value.get("previous")
    current = value.get("current")
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        msg = "replay_invalid"
        raise MonitorError(
            msg, "previous and current snapshots are required"
        )
    previous = cast("Mapping[str, Any]", previous)
    current = cast("Mapping[str, Any]", current)
    for name, snapshot in (("previous", previous), ("current", current)):
        _validate_snapshot(name, snapshot)
    config_value = value.get("diff_config", {})
    if not isinstance(config_value, Mapping):
        msg = "replay_invalid"
        raise MonitorError(msg, "diff_config must be an object")
    config_value = cast("Mapping[str, Any]", config_value)
    diff = compare_content(
        str(previous["text"]),
        str(current["text"]),
        previous_hash=str(previous["normalized_hash"]),
        current_hash=str(current["normalized_hash"]),
        config=DiffConfig.from_mapping(config_value),
    )
    _check_expected(value.get("expected", {}), diff)
    summary_result: dict[str, Any] | None = None
    if "summary" in value:
        summary = value["summary"]
        if not isinstance(summary, Mapping):
            msg = "replay_invalid"
            raise MonitorError(msg, "summary must be an object")
        summary = cast("Mapping[str, Any]", summary)
        source_url = value.get("source_url")
        if not isinstance(source_url, str):
            msg = "replay_invalid"
            raise MonitorError(
                msg, "independent replay source_url is missing"
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
    """Run the CLI entry point: replay the manifest named in ``argv[1]``.

    On success, writes the JSON-encoded :func:`replay_manifest` result to
    stdout. On a handled failure, writes ``{"error": ...}`` JSON to stdout
    instead. Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if the manifest is invalid or the replay fails, 2
        for incorrect CLI usage.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write("usage: replay.py MANIFEST_JSON\n")
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
        json.dump({"error": error}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
