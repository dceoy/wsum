# ruff: noqa: DOC201, EM101, EM102, INP001, T201, TRY003
"""Cowork-facing orchestration for CSV-based web update monitoring."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import monitor
import workflow

_TARGETS_FILE = "targets.csv"
_STATE_DIR = ".wsum"
_MAX_CSV_BYTES = 1024 * 1024
_REQUIRED_FIELDS = {"name", "url"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"enabled", "watch_focus"}
_PENDING_FIELDS = {
    "candidate_sha256",
    "diff_truncated",
    "expected_sha256",
    "target_id",
}
_TARGET_ID_PART_RE = re.compile(r"[^a-z0-9]+")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CoworkError(RuntimeError):
    """Expected Cowork workspace or decision error."""


def _workspace(value: str | Path) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CoworkError("workspace must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CoworkError("workspace must be a non-symlink directory")
    return path.resolve()


def _ensure_directory(path: Path, description: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
            info = path.lstat()
        except OSError as exc:
            raise CoworkError(f"{description} is unavailable") from exc
    except OSError as exc:
        raise CoworkError(f"{description} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CoworkError(f"{description} must be a non-symlink directory")
    return path


def _state_dir(workspace: Path) -> Path:
    return _ensure_directory(workspace / _STATE_DIR, "workspace state directory")


def _read_csv(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CoworkError(f"{_TARGETS_FILE} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CoworkError(f"{_TARGETS_FILE} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > _MAX_CSV_BYTES:
        raise CoworkError(f"{_TARGETS_FILE} size is invalid")
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CoworkError(f"{_TARGETS_FILE} must be UTF-8 CSV") from exc


def _parse_enabled(value: str, row_number: int) -> bool:
    normalized = value.strip().lower()
    if not normalized or normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CoworkError(f"row {row_number}: enabled must be true or false")


def _target_id(url: str) -> str:
    try:
        host = urlsplit(url).hostname or "target"
    except ValueError:
        host = "target"
    prefix = _TARGET_ID_PART_RE.sub("-", host.lower()).strip("-") or "target"
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"{prefix[:48]}-{digest}"


def load_targets(workspace: str | Path) -> list[dict[str, object]]:
    """Load, validate, and normalize all targets from ``targets.csv``."""
    root = _workspace(workspace)
    reader = csv.DictReader(io.StringIO(_read_csv(root / _TARGETS_FILE)))
    fieldnames = reader.fieldnames
    if fieldnames is None or len(fieldnames) != len(set(fieldnames)):
        raise CoworkError(f"{_TARGETS_FILE} must have a unique header row")
    fields = set(fieldnames)
    if not _REQUIRED_FIELDS <= fields:
        raise CoworkError(f"{_TARGETS_FILE} requires name and url columns")
    if fields - _ALLOWED_FIELDS:
        raise CoworkError(f"{_TARGETS_FILE} contains unsupported columns")

    raw_targets: list[dict[str, object]] = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise CoworkError(f"row {row_number}: too many columns")
        values: dict[str, str] = {}
        for key in fieldnames:
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise CoworkError(f"row {row_number}: invalid CSV value")
            values[key] = (value or "").strip()
        if not any(values.values()):
            continue
        url = values.get("url", "")
        raw_targets.append(
            {
                "target_id": _target_id(url),
                "name": values.get("name", ""),
                "url": url,
                "enabled": _parse_enabled(values.get("enabled", ""), row_number),
                "watch_focus": values.get("watch_focus", ""),
                "fetch_mode": "static",
            }
        )
    if not raw_targets:
        raise CoworkError(f"{_TARGETS_FILE} contains no targets")

    normalized = workflow.validate_targets({"targets": raw_targets})["targets"]
    return cast("list[dict[str, object]]", normalized)


def _monitor_target(state: Path, target: Mapping[str, object]) -> dict[str, object]:
    target_id = str(target["target_id"])
    candidates = _ensure_directory(state / "candidates", "candidate directory")
    snapshots = _ensure_directory(state / "snapshots", "snapshot directory")
    candidate = candidates / f"{target_id}.txt"
    previous = snapshots / f"{target_id}.txt"
    arguments = ["--url", str(target["url"]), "--output", str(candidate)]
    if previous.exists():
        arguments.extend(["--previous", str(previous)])
    namespace = monitor._parser().parse_args(arguments)  # pyright: ignore[reportPrivateUsage]
    result = monitor.run(namespace)
    return _handle_monitor_result(state, target, candidate, result)


def _remove_pending(state: Path, target_id: str) -> None:
    with suppress(OSError):
        (state / "pending" / f"{target_id}.json").unlink(missing_ok=True)


def _cleanup_candidate(candidate: Path) -> None:
    try:
        candidate.unlink(missing_ok=True)
    except OSError as exc:
        raise CoworkError("cannot remove candidate snapshot") from exc


def _write_pending(state: Path, payload: Mapping[str, object]) -> None:
    pending = _ensure_directory(state / "pending", "pending directory")
    target_id = str(payload["target_id"])
    destination = pending / f"{target_id}.json"
    data = (json.dumps(payload, sort_keys=True) + "\n").encode()
    temporary_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=pending, prefix=f".{target_id}.", suffix=".tmp"
        )
        temporary_path = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(destination)
        temporary_path = None
    except OSError as exc:
        raise CoworkError("cannot persist pending decision state") from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _handle_monitor_result(
    state: Path,
    target: Mapping[str, object],
    candidate: Path,
    result: Mapping[str, object],
) -> dict[str, object]:
    target_id = str(target["target_id"])
    status = result.get("status")
    if status == "unchanged":
        _cleanup_candidate(candidate)
        _remove_pending(state, target_id)
        return {"action": "unchanged", "target_id": target_id, "name": target["name"]}
    if status == "baseline":
        promoted = workflow.promote_snapshot(
            state,
            candidate,
            {
                "target_id": target_id,
                "expected_sha256": None,
                "candidate_sha256": result.get("sha256"),
            },
        )
        if promoted.get("action") != "snapshot_promoted":
            return {"action": "snapshot_conflict", "target_id": target_id}
        _cleanup_candidate(candidate)
        _remove_pending(state, target_id)
        return {
            "action": "baseline_created",
            "target_id": target_id,
            "name": target["name"],
        }
    if status != "changed":
        raise CoworkError("monitor returned an unsupported status")

    pending = {
        "target_id": target_id,
        "expected_sha256": result.get("previous_sha256"),
        "candidate_sha256": result.get("sha256"),
        "diff_truncated": result.get("diff_truncated") is True,
    }
    _write_pending(state, pending)
    return {
        "action": "review",
        "target_id": target_id,
        "name": target["name"],
        "url": target["url"],
        "watch_focus": target["watch_focus"],
        "diff": result.get("diff", ""),
        "diff_truncated": pending["diff_truncated"],
    }


def check(workspace: str | Path) -> dict[str, object]:
    """Check every enabled CSV target and return only agent-relevant outcomes."""
    root = _workspace(workspace)
    targets = load_targets(root)
    state = _state_dir(root)
    outcomes: list[dict[str, object]] = []
    for target in targets:
        if target["action"] == "skip_disabled":
            outcomes.append(
                {
                    "action": "skipped",
                    "target_id": target["target_id"],
                    "name": target["name"],
                }
            )
            continue
        try:
            outcomes.append(_monitor_target(state, target))
        except (monitor.MonitorError, OSError, CoworkError, workflow.WorkflowError) as exc:
            outcomes.append(
                {
                    "action": "error",
                    "target_id": target["target_id"],
                    "name": target["name"],
                    "error": str(exc),
                }
            )
    return {"targets": outcomes}


def _read_pending(state: Path, target_id: str) -> dict[str, object]:
    path = state / "pending" / f"{target_id}.json"
    try:
        info = path.lstat()
    except OSError as exc:
        raise CoworkError("no valid pending decision exists for target") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CoworkError("pending decision must be a regular non-symlink file")
    try:
        data = path.read_text(encoding="utf-8")
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoworkError("no valid pending decision exists for target") from exc
    if not isinstance(value, dict) or set(value) != _PENDING_FIELDS:
        raise CoworkError("pending decision is invalid")
    return cast("dict[str, object]", value)


def finalize(workspace: str | Path, payload: Mapping[str, object]) -> dict[str, object]:
    """Apply one semantic decision and safely advance its baseline."""
    unsupported = set(payload) - {"material", "report", "target_id"}
    if unsupported:
        raise CoworkError("decision contains unsupported fields")
    target_id = payload.get("target_id")
    material = payload.get("material")
    if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
        raise CoworkError("target_id is invalid")
    if not isinstance(material, bool):
        raise CoworkError("material must be a boolean")

    root = _workspace(workspace)
    state = _state_dir(root)
    pending = _read_pending(state, target_id)
    if pending["target_id"] != target_id:
        raise CoworkError("pending decision target does not match")
    if pending["diff_truncated"] is True and not material:
        return {"action": "manual_review_required", "target_id": target_id}

    report_path: str | None = None
    if material:
        report = payload.get("report")
        if not isinstance(report, str) or not report:
            raise CoworkError("material decisions require a non-empty report")
        report_result = workflow.write_report(
            root, {"target_id": target_id, "report": report}
        )
        report_path = str(report_result["path"])

    candidate = state / "candidates" / f"{target_id}.txt"
    promoted = workflow.promote_snapshot(
        state,
        candidate,
        {
            "target_id": target_id,
            "expected_sha256": pending["expected_sha256"],
            "candidate_sha256": pending["candidate_sha256"],
        },
    )
    if promoted.get("action") == "snapshot_conflict":
        return {"action": "snapshot_conflict", "target_id": target_id}
    _cleanup_candidate(candidate)
    _remove_pending(state, target_id)
    result: dict[str, object] = {
        "action": "finalized",
        "target_id": target_id,
        "material": material,
    }
    if report_path is not None:
        result["report_path"] = report_path
    return result


def _read_decision() -> Mapping[str, object]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise CoworkError("stdin must contain a valid decision object") from exc
    if not isinstance(value, Mapping):
        raise CoworkError("decision must be an object")
    return cast("Mapping[str, object]", value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("finalize")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Cowork-facing checker or finalizer."""
    args = _parser().parse_args(argv)
    try:
        result = (
            check(args.workspace)
            if args.command == "check"
            else finalize(args.workspace, _read_decision())
        )
    except (CoworkError, workflow.WorkflowError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
