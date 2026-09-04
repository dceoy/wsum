"""Validate local monitor inputs and promote snapshots safely."""

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
from urllib.parse import urlsplit

import monitor

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FETCH_MODES = {"static", "browser"}
_TARGET_FIELDS = {
    "enabled",
    "fetch_mode",
    "name",
    "target_id",
    "url",
    "watch_focus",
}
_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024


class WorkflowError(RuntimeError):
    """Expected workflow input or local persistence failure."""


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{field} must be an object")
    return value


def _string(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        kind = "string" if empty else "non-empty string"
        raise WorkflowError(f"{field} must be a {kind}")
    return value


def _target_id(value: object) -> str:
    target_id = _string(value, "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    return target_id


def _digest(value: object, field: str) -> str:
    digest = _string(value, field)
    if not _SHA256_RE.fullmatch(digest):
        raise WorkflowError(f"{field} must be a lowercase hexadecimal SHA-256")
    return digest


def _url(value: object) -> str:
    url = _string(value, "url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise WorkflowError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WorkflowError("url must be an absolute HTTP(S) URL")
    if parsed.fragment or monitor.url_has_credentials(url):
        raise WorkflowError("url contains unsupported credentials or fragment")
    return url


def validate_target(value: object) -> dict[str, object]:
    """Validate and normalize one monitoring target."""
    target = _mapping(value, "target")
    if set(target) - _TARGET_FIELDS:
        raise WorkflowError("target contains unsupported fields")

    enabled = target.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkflowError("enabled must be a boolean")
    fetch_mode = _string(target.get("fetch_mode", "static"), "fetch_mode")
    if fetch_mode not in _FETCH_MODES:
        raise WorkflowError("fetch_mode must be static or browser")

    return {
        "target_id": _target_id(target.get("target_id")),
        "name": _string(target.get("name"), "name"),
        "url": _url(target.get("url")),
        "enabled": enabled,
        "action": "monitor" if enabled else "skip_disabled",
        "watch_focus": _string(
            target.get("watch_focus", ""), "watch_focus", empty=True
        ),
        "fetch_mode": fetch_mode,
    }


def validate_targets(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one local run's target set."""
    if set(payload) != {"targets"}:
        raise WorkflowError("request must contain only targets")
    raw_targets = payload["targets"]
    if not isinstance(raw_targets, list):
        raise WorkflowError("targets must be an array")
    targets = [validate_target(target) for target in raw_targets]
    ids = [target["target_id"] for target in targets]
    if len(ids) != len(set(ids)):
        raise WorkflowError("duplicate_target_id")
    return {"targets": targets}


def _directory(path: Path, description: str, *, create: bool = False) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise WorkflowError(
                f"{description} must be an existing directory"
            ) from None
        try:
            path.mkdir(mode=0o700)
            info = path.lstat()
        except OSError as exc:
            raise WorkflowError(f"{description} is unavailable") from exc
    except OSError as exc:
        raise WorkflowError(f"{description} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkflowError(f"{description} must be a non-symlink directory")
    return path.resolve()


def _text_bytes(path: Path, description: str) -> bytes:
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


def _current_snapshot(path: Path) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError("cannot stat snapshot") from exc
    return _text_bytes(path, "snapshot")


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_snapshot(destination: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise WorkflowError("cannot promote snapshot") from exc


def promote_snapshot(
    runtime_dir: str | Path,
    candidate_path: str | Path,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Atomically promote a local candidate when its expected baseline matches."""
    runtime = _directory(Path(runtime_dir), "runtime_dir")
    candidates = _directory(
        runtime / "candidates", "runtime_dir/candidates", create=True
    )
    candidate = Path(candidate_path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        candidate.resolve().relative_to(candidates)
    except ValueError as exc:
        raise WorkflowError("candidate must be under runtime_dir/candidates") from exc
    candidate_data = _text_bytes(candidate, "candidate")

    target_id = _target_id(payload.get("target_id"))
    expected_value = payload.get("expected_sha256")
    expected = (
        None if expected_value is None else _digest(expected_value, "expected_sha256")
    )
    candidate_sha = _digest(payload.get("candidate_sha256"), "candidate_sha256")
    if hashlib.sha256(candidate_data).hexdigest() != candidate_sha:
        raise WorkflowError("candidate_sha256 does not match candidate")

    snapshots = _directory(
        runtime / "snapshots", "runtime_dir/snapshots", create=True
    )
    destination = snapshots / f"{target_id}.txt"
    current = _current_snapshot(destination)
    current_sha = None if current is None else hashlib.sha256(current).hexdigest()
    if current_sha == candidate_sha:
        return {
            "action": "snapshot_promoted",
            "applied": True,
            "already": True,
            "path": str(destination),
            "sha256": candidate_sha,
        }
    if current_sha != expected:
        return {
            "action": "snapshot_conflict",
            "applied": False,
            "current_sha256": current_sha,
        }

    _replace_snapshot(destination, candidate_data)
    if hashlib.sha256(_text_bytes(destination, "snapshot")).hexdigest() != candidate_sha:
        raise WorkflowError("snapshot read-back mismatch")
    return {
        "action": "snapshot_promoted",
        "applied": True,
        "path": str(destination),
        "sha256": candidate_sha,
    }


def _payload() -> Mapping[str, object]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise WorkflowError("stdin must contain valid JSON") from exc
    return _mapping(value, "request")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-targets")
    promote = subparsers.add_parser("promote-snapshot")
    promote.add_argument("--runtime-dir", required=True)
    promote.add_argument("--candidate", required=True)
    return parser


def main() -> int:
    """Run one deterministic workflow helper command."""
    args = _parser().parse_args()
    try:
        if args.command == "validate-targets":
            result = validate_targets(_payload())
        else:
            result = promote_snapshot(args.runtime_dir, args.candidate, _payload())
    except WorkflowError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
