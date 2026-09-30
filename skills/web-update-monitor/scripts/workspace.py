"""Workspace-facing orchestration for CSV-based web update monitoring."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import monitor

_TARGETS_FILE = "targets.csv"
_STATE_DIR = ".wsum"
_MAX_CSV_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024
_REQUIRED_FIELDS = {"name", "url"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"enabled", "watch_focus"}
_PENDING_FIELDS = {
    "candidate_sha256",
    "diff_truncated",
    "expected_sha256",
    "revision",
    "target_id",
}
_TARGET_ID_PART_RE = re.compile(r"[^a-z0-9]+")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceError(RuntimeError):
    """Expected workspace, target, state, or decision error."""


def _workspace(value: str | Path) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError("workspace must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("workspace must be a non-symlink directory")
    return path.resolve()


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_directory(
    path: Path, description: str, *, sync_parent: bool = False
) -> Path:
    created = False
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceError(f"{description} is unavailable") from exc
        else:
            created = True
        try:
            info = path.lstat()
        except OSError as exc:
            raise WorkspaceError(f"{description} is unavailable") from exc
    except OSError as exc:
        raise WorkspaceError(f"{description} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"{description} must be a non-symlink directory")
    if created and sync_parent:
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot fsync parent directory for {description}"
            ) from exc
    return path


def _state_dir(workspace: Path) -> Path:
    return _ensure_directory(workspace / _STATE_DIR, "workspace state directory")


def _read_csv(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError(f"{_TARGETS_FILE} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"{_TARGETS_FILE} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > _MAX_CSV_BYTES:
        raise WorkspaceError(f"{_TARGETS_FILE} size is invalid")
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkspaceError(f"{_TARGETS_FILE} must be UTF-8 CSV") from exc


def _parse_enabled(value: str, row_number: int) -> bool:
    normalized = value.strip().lower()
    if not normalized or normalized == "true":
        return True
    if normalized == "false":
        return False
    raise WorkspaceError(f"row {row_number}: enabled must be true or false")


def _target_id(url: str) -> str:
    try:
        host = urlsplit(url).hostname or "target"
    except ValueError:
        host = "target"
    prefix = _TARGET_ID_PART_RE.sub("-", host.lower()).strip("-") or "target"
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"{prefix[:48]}-{digest}"


def _validate_target_id(value: object) -> str:
    if not isinstance(value, str) or not _TARGET_ID_RE.fullmatch(value):
        raise WorkspaceError("invalid_target_id")
    return value


def _validate_sha256(
    value: object, field: str, *, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorkspaceError(f"{field} must be a lowercase hexadecimal SHA-256")
    return value


def _validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError as exc:
        raise WorkspaceError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WorkspaceError("url must be an absolute HTTP(S) URL")
    if parsed.fragment:
        raise WorkspaceError("url must not contain a fragment")
    if monitor.url_has_credentials(value):
        raise WorkspaceError("url must not contain credentials")
    return value


def load_targets(workspace: str | Path) -> list[dict[str, object]]:
    """Load, validate, and normalize all targets from ``targets.csv``."""
    root = _workspace(workspace)
    reader = csv.DictReader(io.StringIO(_read_csv(root / _TARGETS_FILE)))
    fieldnames = reader.fieldnames
    if fieldnames is None or len(fieldnames) != len(set(fieldnames)):
        raise WorkspaceError(f"{_TARGETS_FILE} must have a unique header row")
    fields = set(fieldnames)
    if not _REQUIRED_FIELDS.issubset(fields):
        raise WorkspaceError(f"{_TARGETS_FILE} requires name and url columns")
    if fields - _ALLOWED_FIELDS:
        raise WorkspaceError(f"{_TARGETS_FILE} contains unsupported columns")

    targets: list[dict[str, object]] = []
    target_ids: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise WorkspaceError(f"row {row_number}: too many columns")
        values: dict[str, str] = {}
        for key in fieldnames:
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise WorkspaceError(f"row {row_number}: invalid CSV value")
            values[key] = (value or "").strip()
        if not any(values.values()):
            continue

        name = values.get("name", "")
        if not name:
            raise WorkspaceError(f"row {row_number}: name must be non-empty")
        url = _validate_url(values.get("url", ""))
        target_id = _target_id(url)
        if target_id in target_ids:
            raise WorkspaceError("duplicate_target_id")
        target_ids.add(target_id)
        enabled = _parse_enabled(values.get("enabled", ""), row_number)
        targets.append({
            "target_id": target_id,
            "name": name,
            "url": url,
            "enabled": enabled,
            "action": "monitor" if enabled else "skip_disabled",
            "watch_focus": values.get("watch_focus", ""),
        })

    if not targets:
        raise WorkspaceError(f"{_TARGETS_FILE} contains no targets")
    return targets


def _candidate_path(state: Path, value: str | Path) -> Path:
    candidates_dir = _ensure_directory(
        state / "candidates", "workspace state/candidates"
    )
    candidate = Path(value)
    original = Path.cwd() / candidate if not candidate.is_absolute() else candidate
    try:
        info = original.lstat()
    except OSError as exc:
        raise WorkspaceError("cannot stat candidate") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("candidate must be a regular non-symlink file")
    candidate = original.resolve()
    try:
        candidate.relative_to(candidates_dir.resolve())
    except ValueError as exc:
        raise WorkspaceError(
            "candidate must be under workspace state/candidates"
        ) from exc
    return candidate


def _report_path(reports_dir: Path, target_id: str) -> Path:
    destination = reports_dir / f"{target_id}.md"
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise WorkspaceError("cannot stat report") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("report must be a regular non-symlink file")
    return destination


def _read_text_bytes(path: Path, description: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError(f"cannot stat {description}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"{description} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError(f"{description} size is invalid")
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkspaceError(f"cannot read {description} as UTF-8") from exc
    return data


def _read_snapshot(path: Path) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError("cannot stat snapshot") from exc
    return _read_text_bytes(path, "snapshot")


def _write_file_data(descriptor: int, data: bytes) -> None:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=False) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _write_temporary_file(destination: Path, data: bytes, description: str) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise WorkspaceError(f"cannot create temporary {description}") from exc
    path = Path(name)
    completed = False
    try:
        _write_file_data(descriptor, data)
        completed = True
    except OSError as exc:
        raise WorkspaceError(f"cannot write temporary {description}") from exc
    finally:
        os.close(descriptor)
        if not completed:
            with suppress(OSError):
                path.unlink(missing_ok=True)
    return path


def _fsync_snapshot_directory(path: Path) -> None:
    try:
        _fsync_directory(path)
    except OSError as exc:
        raise WorkspaceError("cannot fsync snapshot directory") from exc


def _promote_snapshot(
    state: Path,
    candidate_path: str | Path,
    *,
    target_id: str,
    expected_sha256: object,
    candidate_sha256: object,
) -> dict[str, object]:
    """Atomically promote a candidate when its expected baseline matches."""
    state = _workspace(state)
    target_id = _validate_target_id(target_id)
    expected = _validate_sha256(expected_sha256, "expected_sha256", allow_none=True)
    candidate_digest = _validate_sha256(candidate_sha256, "candidate_sha256")
    candidate = _candidate_path(state, candidate_path)
    candidate_data = _read_text_bytes(candidate, "candidate")
    if hashlib.sha256(candidate_data).hexdigest() != candidate_digest:
        raise WorkspaceError("candidate_sha256 does not match candidate")

    snapshots_dir = _ensure_directory(
        state / "snapshots",
        "workspace state/snapshots",
        sync_parent=True,
    )
    destination = snapshots_dir / f"{target_id}.txt"
    current = _read_snapshot(destination)
    current_sha256 = None if current is None else hashlib.sha256(current).hexdigest()
    if current_sha256 == candidate_digest:
        _fsync_snapshot_directory(snapshots_dir)
        return {
            "action": "snapshot_promoted",
            "applied": True,
            "already": True,
            "path": str(destination),
            "sha256": candidate_digest,
        }
    if current_sha256 != expected:
        return {
            "action": "snapshot_conflict",
            "applied": False,
            "current_sha256": current_sha256,
        }

    temporary = _write_temporary_file(destination, candidate_data, "snapshot")
    try:
        temporary.replace(destination)
    except OSError as exc:
        raise WorkspaceError("cannot promote snapshot") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)

    _fsync_snapshot_directory(snapshots_dir)
    durable = _read_snapshot(destination)
    durable_sha256 = None if durable is None else hashlib.sha256(durable).hexdigest()
    if durable_sha256 != candidate_digest:
        raise WorkspaceError("snapshot read-back mismatch")
    return {
        "action": "snapshot_promoted",
        "applied": True,
        "path": str(destination),
        "sha256": candidate_digest,
    }


def _write_report(workspace: Path, target_id: str, report: str) -> Path:
    """Atomically write one Markdown report under the workspace."""
    workspace = _workspace(workspace)
    target_id = _validate_target_id(target_id)
    report_data = report.encode("utf-8")
    if not report_data or len(report_data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("report size is invalid")

    reports_dir = _ensure_directory(
        workspace / "reports", "workspace reports directory", sync_parent=True
    )
    destination = _report_path(reports_dir, target_id)
    temporary = _write_temporary_file(destination, report_data, "report")
    try:
        temporary.replace(destination)
    except OSError as exc:
        raise WorkspaceError("cannot write report") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)

    try:
        _fsync_directory(reports_dir)
    except OSError as exc:
        raise WorkspaceError("cannot fsync report directory") from exc
    durable = _read_text_bytes(destination, "report")
    if durable != report_data:
        raise WorkspaceError("report read-back mismatch")
    return destination


def _monitor_target(state: Path, target: Mapping[str, object]) -> dict[str, object]:
    target_id = str(target["target_id"])
    candidates = _ensure_directory(state / "candidates", "candidate directory")
    snapshots = _ensure_directory(state / "snapshots", "snapshot directory")
    candidate = candidates / f"{target_id}.txt"
    previous = snapshots / f"{target_id}.txt"
    arguments = ["--url", str(target["url"]), "--output", str(candidate)]
    if previous.exists():
        arguments.extend(["--previous", str(previous)])
    namespace = monitor._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        arguments
    )
    result = monitor.run(namespace)
    return _handle_monitor_result(state, target, candidate, result)


def _remove_pending(state: Path, target_id: str) -> None:
    with suppress(OSError):
        (state / "pending" / f"{target_id}.json").unlink(missing_ok=True)


def _cleanup_candidate(candidate: Path) -> None:
    try:
        candidate.unlink(missing_ok=True)
    except OSError as exc:
        raise WorkspaceError("cannot remove candidate snapshot") from exc


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
        raise WorkspaceError("cannot persist pending decision state") from exc
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
        promoted = _promote_snapshot(
            state,
            candidate,
            target_id=target_id,
            expected_sha256=None,
            candidate_sha256=result.get("sha256"),
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
        raise WorkspaceError("monitor returned an unsupported status")

    pending = {
        "target_id": target_id,
        "revision": secrets.token_hex(16),
        "expected_sha256": result.get("previous_sha256"),
        "candidate_sha256": result.get("sha256"),
        "diff_truncated": result.get("diff_truncated") is True,
    }
    _write_pending(state, pending)
    return {
        "action": "review",
        "target_id": target_id,
        "revision": pending["revision"],
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
            outcomes.append({
                "action": "skipped",
                "target_id": target["target_id"],
                "name": target["name"],
            })
            continue
        try:
            outcomes.append(_monitor_target(state, target))
        except (monitor.MonitorError, OSError, WorkspaceError) as exc:
            outcomes.append({
                "action": "error",
                "target_id": target["target_id"],
                "name": target["name"],
                "error": str(exc),
            })
    return {"targets": outcomes}


def _read_pending(state: Path, target_id: str) -> dict[str, object]:
    path = state / "pending" / f"{target_id}.json"
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError("no valid pending decision exists for target") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("pending decision must be a regular non-symlink file")
    try:
        data = path.read_text(encoding="utf-8")
        value = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("no valid pending decision exists for target") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("pending decision is invalid")
    pending = cast("dict[str, object]", value)
    if set(pending) != _PENDING_FIELDS:
        raise WorkspaceError("pending decision is invalid")
    return pending


def _validate_decision(
    payload: Mapping[str, object],
) -> tuple[str, str, bool, str | None]:
    """Validate a decision and return its target, revision, materiality, and report."""
    unsupported = set(payload) - {"material", "report", "revision", "target_id"}
    if unsupported:
        raise WorkspaceError("decision contains unsupported fields")
    target_id = payload.get("target_id")
    revision = payload.get("revision")
    material = payload.get("material")
    if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkspaceError("target_id is invalid")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise WorkspaceError("revision is invalid")
    if not isinstance(material, bool):
        raise WorkspaceError("material must be a boolean")

    report: str | None = None
    if material:
        report_value = payload.get("report")
        if not isinstance(report_value, str) or not report_value:
            raise WorkspaceError("material decisions require a non-empty report")
        report = report_value
    return target_id, revision, material, report


def finalize(workspace: str | Path, payload: Mapping[str, object]) -> dict[str, object]:
    """Apply one semantic decision and safely advance its baseline."""
    target_id, revision, material, report = _validate_decision(payload)

    root = _workspace(workspace)
    state = _state_dir(root)
    pending = _read_pending(state, target_id)
    if pending["target_id"] != target_id:
        raise WorkspaceError("pending decision target does not match")
    if pending["revision"] != revision:
        raise WorkspaceError("decision revision does not match pending review")
    if pending["diff_truncated"] is True and not material:
        return {"action": "manual_review_required", "target_id": target_id}

    candidate = state / "candidates" / f"{target_id}.txt"
    promoted = _promote_snapshot(
        state,
        candidate,
        target_id=target_id,
        expected_sha256=pending["expected_sha256"],
        candidate_sha256=pending["candidate_sha256"],
    )
    if promoted.get("action") == "snapshot_conflict":
        return {"action": "snapshot_conflict", "target_id": target_id}

    report_path: str | None = None
    if report is not None:
        report_path = str(_write_report(root, target_id, report))
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
        raise WorkspaceError("stdin must contain a valid decision object") from exc
    if not isinstance(value, Mapping):
        raise WorkspaceError("decision must be an object")
    return cast("Mapping[str, object]", value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("finalize")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the workspace-facing checker or finalizer."""
    args = _parser().parse_args(argv)
    try:
        result = (
            check(args.workspace)
            if args.command == "check"
            else finalize(args.workspace, _read_decision())
        )
    except WorkspaceError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
