"""Deterministic local workflow helpers for the web update monitor skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import monitor

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FETCH_MODES = {"static", "browser"}
_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024


class WorkflowError(RuntimeError):
    """Expected workflow input or local persistence failure."""


def _require_string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        kind = "string" if allow_empty else "non-empty string"
        raise WorkflowError(f"{field} must be a {kind}")
    return value


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{field} must be an object")
    return cast("Mapping[str, object]", value)


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowError(f"{field} must be an array")
    return cast("list[object]", value)


def _validate_target_id(value: object) -> str:
    target_id = _require_string(value, "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    return target_id


def _validate_sha256(
    value: object, field: str, *, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    digest = _require_string(value, field)
    if not _SHA256_RE.fullmatch(digest):
        raise WorkflowError(f"{field} must be a lowercase hexadecimal SHA-256")
    return digest


def _validate_url(value: object) -> str:
    url = _require_string(value, "url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise WorkflowError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WorkflowError("url must be an absolute HTTP(S) URL")
    if parsed.fragment:
        raise WorkflowError("url must not contain a fragment")
    if monitor.url_has_credentials(url):
        raise WorkflowError("url must not contain credentials")
    return url


def validate_target(value: object) -> dict[str, object]:
    """Validate and normalize one monitoring target."""
    target = _require_mapping(value, "target")
    if target.get("include_selector") or target.get("exclude_selectors"):
        raise WorkflowError("selector_migration_required")

    enabled = target.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkflowError("enabled must be a boolean")
    fetch_mode = _require_string(target.get("fetch_mode", "static"), "fetch_mode")
    if fetch_mode not in _FETCH_MODES:
        raise WorkflowError("fetch_mode must be static or browser")

    return {
        "target_id": _validate_target_id(target.get("target_id")),
        "name": _require_string(target.get("name"), "name"),
        "url": _validate_url(target.get("url")),
        "enabled": enabled,
        "action": "monitor" if enabled else "skip_disabled",
        "watch_focus": _require_string(
            target.get("watch_focus", ""), "watch_focus", allow_empty=True
        ),
        "notification_group": _require_string(
            target.get("notification_group", ""),
            "notification_group",
            allow_empty=True,
        ),
        "fetch_mode": fetch_mode,
    }


def validate_targets(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one local run's target set."""
    raw_targets = _require_list(payload.get("targets"), "targets")
    targets = [validate_target(target) for target in raw_targets]
    ids = [str(target["target_id"]) for target in targets]
    if len(ids) != len(set(ids)):
        raise WorkflowError("duplicate_target_id")
    return {"targets": targets}


def change_action(payload: Mapping[str, object]) -> dict[str, object]:
    """Choose the next action from a monitor result and materiality judgment."""
    status = _require_string(payload.get("status"), "status")
    if status == "baseline":
        return {"action": "promote_snapshot"}
    if status == "unchanged":
        return {"action": "discard_candidate"}
    if status != "changed":
        raise WorkflowError("status must be baseline, unchanged, or changed")

    diff_truncated = payload.get("diff_truncated")
    if not isinstance(diff_truncated, bool):
        raise WorkflowError("diff_truncated must be a boolean")
    materiality = payload.get("materiality")
    if materiality is None:
        return {"action": "assess_materiality"}
    if not isinstance(materiality, bool):
        raise WorkflowError("materiality must be a boolean or null")
    if diff_truncated and not materiality:
        return {"action": "manual_review"}
    return {"action": "notify" if materiality else "promote_snapshot"}


def promote_snapshot(
    runtime_dir: str | Path,
    candidate_path: str | Path,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Atomically promote a local candidate when its expected baseline matches."""
    runtime = _runtime_dir(runtime_dir)
    target_id = _validate_target_id(payload.get("target_id"))
    expected_sha256 = _validate_sha256(
        payload.get("expected_sha256"), "expected_sha256", allow_none=True
    )
    candidate_sha256 = _validate_sha256(
        payload.get("candidate_sha256"), "candidate_sha256"
    )
    candidate = _candidate_path(runtime, candidate_path)
    candidate_data = _read_text_bytes(candidate, "candidate")
    if hashlib.sha256(candidate_data).hexdigest() != candidate_sha256:
        raise WorkflowError("candidate_sha256 does not match candidate")

    snapshots_dir = runtime / "snapshots"
    snapshots_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = snapshots_dir / f"{target_id}.txt"
    current = _read_snapshot(destination)
    current_sha256 = None if current is None else hashlib.sha256(current).hexdigest()
    if current_sha256 != expected_sha256:
        return {
            "action": "snapshot_conflict",
            "applied": False,
            "current_sha256": current_sha256,
        }

    temporary = _write_temporary_snapshot(destination, candidate_data)
    try:
        os.replace(temporary, destination)
    except OSError as exc:
        raise WorkflowError("cannot promote snapshot") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)

    durable = _read_snapshot(destination)
    durable_sha256 = None if durable is None else hashlib.sha256(durable).hexdigest()
    if durable_sha256 != candidate_sha256:
        raise WorkflowError("snapshot read-back mismatch")
    return {
        "action": "snapshot_promoted",
        "applied": True,
        "path": str(destination),
        "sha256": candidate_sha256,
    }


def _runtime_dir(value: str | Path) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkflowError("runtime_dir must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkflowError("runtime_dir must be a non-symlink directory")
    return path.resolve()


def _candidate_path(runtime_dir: Path, value: str | Path) -> Path:
    candidates_dir = runtime_dir / "candidates"
    candidates_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = Path(value)
    original = Path.cwd() / candidate if not candidate.is_absolute() else candidate
    try:
        info = original.lstat()
    except OSError as exc:
        raise WorkflowError("cannot stat candidate") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkflowError("candidate must be a regular non-symlink file")
    candidate = original.resolve()
    try:
        candidate.relative_to(candidates_dir.resolve())
    except ValueError as exc:
        raise WorkflowError("candidate must be under runtime_dir/candidates") from exc
    return candidate


def _read_snapshot(path: Path) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError("cannot stat snapshot") from exc
    return _read_text_bytes(path, "snapshot")


def _read_text_bytes(path: Path, description: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkflowError(f"cannot stat {description}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkflowError(f"{description} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > _MAX_SNAPSHOT_BYTES:
        raise WorkflowError(f"{description} size is invalid")
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"cannot read {description} as UTF-8") from exc
    return data


def _write_temporary_snapshot(destination: Path, data: bytes) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise WorkflowError("cannot create temporary snapshot") from exc
    path = Path(name)
    completed = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        completed = True
    except OSError as exc:
        raise WorkflowError("cannot write temporary snapshot") from exc
    finally:
        os.close(descriptor)
        if not completed:
            with suppress(OSError):
                path.unlink(missing_ok=True)
    return path


def _read_payload() -> Mapping[str, object]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise WorkflowError("stdin must contain valid JSON") from exc
    return _require_mapping(value, "request")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-targets")
    subparsers.add_parser("change-action")
    promote = subparsers.add_parser("promote-snapshot")
    promote.add_argument("--runtime-dir", required=True)
    promote.add_argument("--candidate", required=True)
    return parser


def main() -> int:
    """Run one deterministic workflow helper command."""
    args = _parser().parse_args()
    try:
        if args.command == "validate-targets":
            result = validate_targets(_read_payload())
        elif args.command == "change-action":
            result = change_action(_read_payload())
        else:
            result = promote_snapshot(args.runtime_dir, args.candidate, _read_payload())
    except WorkflowError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
