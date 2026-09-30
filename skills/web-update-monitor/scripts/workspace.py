"""Workspace-facing orchestration for CSV-based web update monitoring."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import monitor

_TARGETS_FILE = "targets.csv"
_STATE_DIR = ".wsum"
_PENDING_RECOVERY_DIR = ".pending-recovery"
_MAX_CSV_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024
_MAX_RECOVERY_RECORD_BYTES = 3 * _MAX_SNAPSHOT_BYTES + 1024 * 1024
_REQUIRED_FIELDS = {"name", "url"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"enabled", "watch_focus"}
_PENDING_FIELDS = {
    "candidate_sha256",
    "diff_truncated",
    "expected_sha256",
    "revision",
    "run_id",
    "target_id",
}
_TARGET_ID_PART_RE = re.compile(r"[^a-z0-9]+")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_RECORD_VERSION = 1


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
    return _ensure_directory(
        workspace / _STATE_DIR,
        "workspace state directory",
        sync_parent=True,
    )


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


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise WorkspaceError("invalid_run_id")
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


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


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


def _existing_pending_paths(
    state: Path, target_id: str
) -> tuple[Path, Path, str] | None:
    """Resolve existing pending paths without creating a target directory."""
    target_id = _validate_target_id(target_id)
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    target = pending / target_id
    try:
        info = target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkspaceError("pending target directory is unavailable") from exc
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError("pending target must be a non-symlink directory")
        return target / "state.json", target / "candidate.txt", "grouped"

    legacy_state = pending / f"{target_id}.json"
    try:
        info = legacy_state.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError("cannot stat pending decision") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("pending decision must be a regular non-symlink file")

    candidates = state / "candidates"
    try:
        info = candidates.lstat()
    except FileNotFoundError:
        raise WorkspaceError("legacy candidate directory is unavailable") from None
    except OSError as exc:
        raise WorkspaceError("legacy candidate directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(
            "workspace state/candidates must be a non-symlink directory"
        )
    return legacy_state, candidates / f"{target_id}.txt", "legacy"


def _pending_paths(
    state: Path, target_id: str, *, create: bool = False
) -> tuple[Path, Path]:
    """Resolve pending metadata and candidate paths from one layout."""
    target_id = _validate_target_id(target_id)
    existing = _existing_pending_paths(state, target_id)
    if existing is not None:
        return existing[0], existing[1]
    if create:
        target = _ensure_directory(
            state / "pending" / target_id,
            "pending target directory",
            sync_parent=True,
        )
        return target / "state.json", target / "candidate.txt"
    raise WorkspaceError("no valid pending decision exists for target")


def _candidate_path(state: Path, target_id: str) -> Path:
    _, candidate = _pending_paths(state, target_id)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise WorkspaceError("cannot stat candidate") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("candidate must be a regular non-symlink file")
    return candidate


def _report_path(reports_dir: Path, run_id: str) -> Path:
    destination = reports_dir / f"{run_id}.md"
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise WorkspaceError("cannot stat report") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("report must be a regular non-symlink file")
    return destination


def _read_optional_report(path: Path) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError("cannot stat report") from exc
    return _read_text_bytes(path, "report")


def _render_run_report(
    run_id: str,
    target_id: str,
    report: str,
    existing: bytes | None,
) -> str:
    marker_prefix = "<!-- wsum:target "
    if marker_prefix in report:
        raise WorkspaceError("report contains reserved marker")
    start_marker = f"{marker_prefix}{target_id}:start -->"
    end_marker = f"{marker_prefix}{target_id}:end -->"
    block = f"{start_marker}\n{report.strip()}\n{end_marker}"

    if existing is None:
        return f"# Web Update Monitor Report\n\nRun: `{run_id}`\n\n{block}\n"

    current = existing.decode("utf-8")
    start = current.find(start_marker)
    end = current.find(end_marker)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise WorkspaceError("report contains invalid managed section")
    if start >= 0:
        if current.find(start_marker, start + len(start_marker)) >= 0:
            raise WorkspaceError("report contains duplicate managed section")
        if current.find(end_marker, end + len(end_marker)) >= 0:
            raise WorkspaceError("report contains duplicate managed section")
        end += len(end_marker)
        return current[:start] + block + current[end:]

    return current.rstrip() + f"\n\n{block}\n"


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
    *,
    target_id: str,
    expected_sha256: object,
    candidate_sha256: object,
    candidate_source: Path | None = None,
) -> dict[str, object]:
    """Atomically promote a candidate when its expected baseline matches."""
    state = _workspace(state)
    target_id = _validate_target_id(target_id)
    expected = _validate_sha256(expected_sha256, "expected_sha256", allow_none=True)
    candidate_digest = _validate_sha256(candidate_sha256, "candidate_sha256")
    candidate = (
        _candidate_path(state, target_id)
        if candidate_source is None
        else candidate_source
    )
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


def _write_report(workspace: Path, run_id: str, target_id: str, report: str) -> Path:
    """Atomically merge one target section into its run-level report."""
    workspace = _workspace(workspace)
    run_id = _validate_run_id(run_id)
    target_id = _validate_target_id(target_id)
    report_data = report.encode("utf-8")
    if not report_data or len(report_data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("report size is invalid")

    reports_dir = _ensure_directory(
        workspace / "reports", "workspace reports directory", sync_parent=True
    )
    destination = _report_path(reports_dir, run_id)
    existing = _read_optional_report(destination)
    report_data = _render_run_report(run_id, target_id, report, existing).encode(
        "utf-8"
    )
    if len(report_data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("report size is invalid")

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


def _monitor_target(
    state: Path, target: Mapping[str, object], run_id: str
) -> dict[str, object]:
    target_id = _validate_target_id(target["target_id"])
    _recover_pending(state, target_id)
    snapshots = _ensure_directory(
        state / "snapshots", "snapshot directory", sync_parent=True
    )
    previous = snapshots / f"{target_id}.txt"
    with tempfile.TemporaryDirectory(prefix=".monitor-", dir=state) as staging:
        candidate = Path(staging) / "candidate.txt"
        arguments = ["--url", str(target["url"]), "--output", str(candidate)]
        if previous.exists():
            arguments.extend(["--previous", str(previous)])
        namespace = monitor._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            arguments
        )
        result = monitor.run(namespace)
        return _handle_monitor_result(
            state, target, result, run_id, candidate_source=candidate
        )


def _optional_lstat(path: Path, description: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError(f"cannot stat {description}") from exc


def _pending_cleanup_paths(
    state: Path, pending: Path, target_id: str
) -> tuple[Path | None, Path | None, Path | None]:
    target = pending / target_id
    target_info = _optional_lstat(target, "pending target")
    if target_info is not None and (
        stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode)
    ):
        raise WorkspaceError("pending target must be a non-symlink directory")

    legacy_state = pending / f"{target_id}.json"
    state_info = _optional_lstat(legacy_state, "pending decision")
    if state_info is not None and (
        stat.S_ISLNK(state_info.st_mode) or not stat.S_ISREG(state_info.st_mode)
    ):
        raise WorkspaceError("pending decision must be a regular non-symlink file")

    candidates = state / "candidates"
    candidates_info = _optional_lstat(candidates, "legacy candidate directory")
    if candidates_info is not None and (
        stat.S_ISLNK(candidates_info.st_mode)
        or not stat.S_ISDIR(candidates_info.st_mode)
    ):
        raise WorkspaceError(
            "workspace state/candidates must be a non-symlink directory"
        )
    legacy_candidate = candidates / f"{target_id}.txt"
    candidate_info = _optional_lstat(legacy_candidate, "candidate")
    if candidate_info is not None and (
        stat.S_ISLNK(candidate_info.st_mode) or not stat.S_ISREG(candidate_info.st_mode)
    ):
        raise WorkspaceError("candidate must be a regular non-symlink file")

    return (
        target if target_info is not None else None,
        legacy_candidate if candidate_info is not None else None,
        legacy_state if state_info is not None else None,
    )


def _remove_pending(state: Path, target_id: str) -> None:
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    target_id = _validate_target_id(target_id)
    grouped_target, legacy_candidate, legacy_state = _pending_cleanup_paths(
        state, pending, target_id
    )

    try:
        if legacy_candidate is not None:
            legacy_candidate.unlink()
        if legacy_state is not None:
            legacy_state.unlink()
    except OSError as exc:
        raise WorkspaceError("cannot remove pending transaction") from exc

    try:
        if grouped_target is not None:
            shutil.rmtree(grouped_target)
    except OSError as exc:
        raise WorkspaceError("cannot remove pending transaction") from exc

    try:
        candidates = state / "candidates"
        candidates_info = _optional_lstat(candidates, "legacy candidate directory")
        if candidates_info is not None:
            if stat.S_ISLNK(candidates_info.st_mode) or not stat.S_ISDIR(
                candidates_info.st_mode
            ):
                raise WorkspaceError(
                    "workspace state/candidates must be a non-symlink directory"
                )
            _fsync_directory(candidates)
        _fsync_directory(pending)
    except OSError as exc:
        raise WorkspaceError("cannot fsync pending transaction directories") from exc


def _write_pending_file(destination: Path, payload: Mapping[str, object]) -> None:
    data = (json.dumps(payload, sort_keys=True) + "\n").encode()
    temporary = _write_temporary_file(destination, data, "pending state")
    try:
        temporary.replace(destination)
    except OSError as exc:
        raise WorkspaceError("cannot persist pending decision state") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise WorkspaceError("cannot fsync pending directory") from exc


def _recovery_directory(state: Path, *, create: bool) -> Path | None:
    path = state / _PENDING_RECOVERY_DIR
    if create:
        directory = _ensure_directory(
            path,
            "pending recovery directory",
            sync_parent=True,
        )
    else:
        info = _optional_lstat(path, "pending recovery directory")
        if info is None:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError(
                "pending recovery directory must be a non-symlink directory"
            )
        directory = path
    try:
        mode = stat.S_IMODE(directory.lstat().st_mode)
    except OSError as exc:
        raise WorkspaceError("cannot stat pending recovery directory") from exc
    if mode & 0o077:
        raise WorkspaceError("pending recovery directory must be private")
    return directory


def _decode_recovery_backup(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkspaceError("pending recovery record is invalid")
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise WorkspaceError("pending recovery record is invalid") from exc
    if len(data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("pending recovery record is too large")
    return data


def _validate_recovery_record(value: object, target_id: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkspaceError("pending recovery record is invalid")
    record = cast("dict[str, object]", value)
    if (
        type(record.get("version")) is not int
        or record["version"] != _RECOVERY_RECORD_VERSION
        or record.get("target_id") != target_id
    ):
        raise WorkspaceError("pending recovery record is invalid")
    kind = record.get("kind")
    if kind == "replace":
        if set(record) != {
            "group_dir_existed",
            "kind",
            "layout",
            "old_candidate",
            "old_state",
            "target_id",
            "version",
        }:
            raise WorkspaceError("pending recovery record is invalid")
        layout = record.get("layout")
        group_dir_existed = record.get("group_dir_existed")
        if (
            not isinstance(layout, str)
            or layout not in {"none", "grouped", "legacy"}
            or not isinstance(group_dir_existed, bool)
            or group_dir_existed != (layout == "grouped")
        ):
            raise WorkspaceError("pending recovery record is invalid")
        _decode_recovery_backup(record.get("old_state"))
        _decode_recovery_backup(record.get("old_candidate"))
    elif kind == "commit":
        if set(record) != {"kind", "revision", "target_id", "version"}:
            raise WorkspaceError("pending recovery record is invalid")
        if not isinstance(record.get("revision"), str) or not _REVISION_RE.fullmatch(
            cast("str", record.get("revision"))
        ):
            raise WorkspaceError("pending recovery record is invalid")
    elif kind == "cleanup":
        if set(record) != {
            "kind",
            "material",
            "purpose",
            "report_sha256",
            "revision",
            "run_id",
            "target_id",
            "version",
        }:
            raise WorkspaceError("pending recovery record is invalid")
        purpose = record.get("purpose")
        if purpose == "discard":
            if any(
                record.get(field) is not None
                for field in ("material", "report_sha256", "revision", "run_id")
            ):
                raise WorkspaceError("pending recovery record is invalid")
        elif purpose == "finalize":
            revision = record.get("revision")
            material = record.get("material")
            report_sha256 = record.get("report_sha256")
            run_id = record.get("run_id")
            if (
                not isinstance(revision, str)
                or not _REVISION_RE.fullmatch(revision)
                or not isinstance(material, bool)
                or not isinstance(run_id, str)
                or not _RUN_ID_RE.fullmatch(run_id)
            ):
                raise WorkspaceError("pending recovery record is invalid")
            if material:
                if not isinstance(report_sha256, str) or not _SHA256_RE.fullmatch(
                    report_sha256
                ):
                    raise WorkspaceError("pending recovery record is invalid")
            elif report_sha256 is not None:
                raise WorkspaceError("pending recovery record is invalid")
        else:
            raise WorkspaceError("pending recovery record is invalid")
    else:
        raise WorkspaceError("pending recovery record is invalid")
    return record


def _read_recovery_record(state: Path, target_id: str) -> dict[str, object] | None:
    target_id = _validate_target_id(target_id)
    directory = _recovery_directory(state, create=False)
    if directory is None:
        return None
    path = directory / f"{target_id}.json"
    info = _optional_lstat(path, "pending recovery record")
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(
            "pending recovery record must be a regular non-symlink file"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise WorkspaceError("pending recovery record must be private")
    if info.st_size <= 0 or info.st_size > _MAX_RECOVERY_RECORD_BYTES:
        raise WorkspaceError("pending recovery record size is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("pending recovery record is invalid") from exc
    return _validate_recovery_record(value, target_id)


def _write_recovery_record(state: Path, record: Mapping[str, object]) -> None:
    target_id = _validate_target_id(record.get("target_id"))
    normalized = _validate_recovery_record(dict(record), target_id)
    data = (json.dumps(normalized, sort_keys=True) + "\n").encode()
    if not data or len(data) > _MAX_RECOVERY_RECORD_BYTES:
        raise WorkspaceError("pending recovery record size is invalid")
    directory = _recovery_directory(state, create=True)
    if directory is None:
        raise WorkspaceError("pending recovery directory is unavailable")
    path = directory / f"{target_id}.json"
    current = _optional_lstat(path, "pending recovery record")
    if current is not None and (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) & 0o077
    ):
        raise WorkspaceError("pending recovery record is unsafe")
    temporary = _write_temporary_file(path, data, "pending recovery record")
    try:
        temporary.replace(path)
    except OSError as exc:
        raise WorkspaceError("cannot persist pending recovery record") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(directory)
    except OSError as exc:
        raise WorkspaceError("cannot fsync pending recovery directory") from exc


def _retire_recovery_record(
    state: Path, target_id: str, *, ignore_errors: bool = False
) -> None:
    directory = _recovery_directory(state, create=False)
    if directory is None:
        return
    path = directory / f"{_validate_target_id(target_id)}.json"
    info = _optional_lstat(path, "pending recovery record")
    if info is None:
        return
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise WorkspaceError("pending recovery record is unsafe")
    try:
        path.unlink()
        _fsync_directory(directory)
    except OSError as exc:
        if not ignore_errors:
            raise WorkspaceError("cannot retire pending recovery record") from exc


def _read_transaction_backup(path: Path, description: str) -> bytes | None:
    info = _optional_lstat(path, description)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"{description} must be a regular non-symlink file")
    return _read_text_bytes(path, description)


def _capture_pending_replacement(state: Path, target_id: str) -> dict[str, object]:
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    existing = _existing_pending_paths(state, target_id)
    layout = "none" if existing is None else existing[2]
    if existing is None:
        old_state = old_candidate = None
    else:
        old_state = _read_transaction_backup(existing[0], "pending decision")
        old_candidate = _read_transaction_backup(existing[1], "candidate")
    group_dir = pending / target_id
    group_info = _optional_lstat(group_dir, "pending target")
    group_dir_existed = group_info is not None
    if group_dir_existed and (
        stat.S_ISLNK(group_info.st_mode) or not stat.S_ISDIR(group_info.st_mode)
    ):
        raise WorkspaceError("pending target must be a non-symlink directory")
    return {
        "group_dir_existed": group_dir_existed,
        "kind": "replace",
        "layout": layout,
        "old_candidate": (
            None if old_candidate is None else base64.b64encode(old_candidate).decode()
        ),
        "old_state": None
        if old_state is None
        else base64.b64encode(old_state).decode(),
        "target_id": target_id,
        "version": _RECOVERY_RECORD_VERSION,
    }


def _write_pending_candidate(destination: Path, data: bytes) -> None:
    temporary = _write_temporary_file(destination, data, "pending candidate")
    try:
        temporary.replace(destination)
    except OSError as exc:
        raise WorkspaceError("cannot persist pending candidate") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise WorkspaceError("cannot fsync pending candidate directory") from exc
    if _read_text_bytes(destination, "candidate") != data:
        raise WorkspaceError("pending candidate read-back mismatch")


def _restore_transaction_file(path: Path, data: bytes | None, description: str) -> None:
    info = _optional_lstat(path, description)
    if data is None:
        if info is None:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError(f"{description} must be a regular non-symlink file")
        try:
            path.unlink()
        except OSError as exc:
            raise WorkspaceError(f"cannot restore {description}") from exc
        return
    _ensure_directory(path.parent, f"{description} directory", sync_parent=True)
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
    ):
        raise WorkspaceError(f"{description} must be a regular non-symlink file")
    temporary = _write_temporary_file(path, data, description)
    try:
        temporary.replace(path)
    except OSError as exc:
        raise WorkspaceError(f"cannot restore {description}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        raise WorkspaceError(f"cannot fsync {description} directory") from exc
    if _read_text_bytes(path, description) != data:
        raise WorkspaceError(f"{description} read-back mismatch")


def _remove_pending_write_temporaries(directory: Path) -> None:
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise WorkspaceError("cannot inspect pending target directory") from exc
    prefixes = (".candidate.txt.", ".state.json.")
    for path in entries:
        if not path.name.endswith(".tmp") or not path.name.startswith(prefixes):
            continue
        info = _optional_lstat(path, "pending temporary file")
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("pending temporary file must be regular")
        try:
            path.unlink()
        except OSError as exc:
            raise WorkspaceError("cannot remove pending temporary file") from exc


def _restore_pending_replacement(state: Path, record: Mapping[str, object]) -> None:
    target_id = _validate_target_id(record.get("target_id"))
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    grouped = pending / target_id
    grouped_info = _optional_lstat(grouped, "pending target")
    if grouped_info is not None and (
        stat.S_ISLNK(grouped_info.st_mode) or not stat.S_ISDIR(grouped_info.st_mode)
    ):
        raise WorkspaceError("pending target must be a non-symlink directory")

    layout = record.get("layout")
    old_state = _decode_recovery_backup(record.get("old_state"))
    old_candidate = _decode_recovery_backup(record.get("old_candidate"))
    if layout == "grouped":
        grouped = _ensure_directory(
            grouped, "pending target directory", sync_parent=True
        )
        _remove_pending_write_temporaries(grouped)
        _restore_transaction_file(grouped / "candidate.txt", old_candidate, "candidate")
        _restore_transaction_file(grouped / "state.json", old_state, "pending decision")
    else:
        if grouped_info is not None:
            _remove_pending_write_temporaries(grouped)
            _restore_transaction_file(grouped / "candidate.txt", None, "candidate")
            _restore_transaction_file(grouped / "state.json", None, "pending decision")
            try:
                grouped.rmdir()
            except OSError as exc:
                raise WorkspaceError("cannot restore pending target directory") from exc
        if layout == "legacy":
            _restore_transaction_file(
                state / "candidates" / f"{target_id}.txt",
                old_candidate,
                "candidate",
            )
            _restore_transaction_file(
                pending / f"{target_id}.json",
                old_state,
                "pending decision",
            )

    candidates = state / "candidates"
    candidates_info = _optional_lstat(candidates, "legacy candidate directory")
    try:
        if candidates_info is not None:
            if stat.S_ISLNK(candidates_info.st_mode) or not stat.S_ISDIR(
                candidates_info.st_mode
            ):
                raise WorkspaceError(
                    "workspace state/candidates must be a non-symlink directory"
                )
            _fsync_directory(candidates)
        if layout == "grouped" and grouped_info is not None:
            _fsync_directory(grouped)
        _fsync_directory(pending)
    except OSError as exc:
        raise WorkspaceError("cannot fsync restored pending transaction") from exc


def _finalize_cleanup_record(
    target_id: str,
    revision: str,
    material: bool,
    report: str | None,
    run_id: str,
) -> dict[str, object]:
    return {
        "kind": "cleanup",
        "material": material,
        "purpose": "finalize",
        "report_sha256": (
            None
            if report is None
            else hashlib.sha256(report.encode("utf-8")).hexdigest()
        ),
        "revision": revision,
        "run_id": _validate_run_id(run_id),
        "target_id": _validate_target_id(target_id),
        "version": _RECOVERY_RECORD_VERSION,
    }


def _complete_cleanup_record(state: Path, record: Mapping[str, object]) -> None:
    target_id = _validate_target_id(record.get("target_id"))
    _remove_pending(state, target_id)
    _retire_recovery_record(state, target_id, ignore_errors=True)


def _recover_pending(state: Path, target_id: str) -> dict[str, object] | None:
    target_id = _validate_target_id(target_id)
    record = _read_recovery_record(state, target_id)
    if record is None:
        return None
    kind = record["kind"]
    if kind == "replace":
        _restore_pending_replacement(state, record)
        _retire_recovery_record(state, target_id)
        return None
    if kind == "commit":
        _retire_recovery_record(state, target_id, ignore_errors=True)
        return None
    _complete_cleanup_record(state, record)
    return record


def _install_pending_replacement(
    state: Path,
    payload: Mapping[str, object],
    candidate_data: bytes,
    undo: Mapping[str, object],
) -> None:
    target_id = _validate_target_id(payload.get("target_id"))
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    target = _ensure_directory(
        pending / target_id,
        "pending target directory",
        sync_parent=True,
    )
    _write_pending_candidate(target / "candidate.txt", candidate_data)
    _write_pending_file(target / "state.json", payload)

    if undo["layout"] == "legacy":
        old_legacy_candidate = state / "candidates" / f"{target_id}.txt"
        old_legacy_state = pending / f"{target_id}.json"
        for path, description in (
            (old_legacy_candidate, "candidate"),
            (old_legacy_state, "pending decision"),
        ):
            info = _optional_lstat(path, description)
            if info is not None:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError(
                        f"{description} must be a regular non-symlink file"
                    )
                path.unlink()
        _fsync_directory(state / "candidates")
        _fsync_directory(pending)

    durable_candidate = _read_text_bytes(target / "candidate.txt", "candidate")
    if durable_candidate != candidate_data:
        raise WorkspaceError("pending candidate read-back mismatch")
    durable_state = _read_pending(state, target_id)
    if durable_state != dict(payload):
        raise WorkspaceError("pending decision read-back mismatch")

    _write_recovery_record(
        state,
        {
            "kind": "commit",
            "revision": payload["revision"],
            "target_id": target_id,
            "version": _RECOVERY_RECORD_VERSION,
        },
    )


def _write_pending_transaction(
    state: Path, payload: Mapping[str, object], candidate_data: bytes
) -> None:
    target_id = _validate_target_id(payload.get("target_id"))
    _recover_pending(state, target_id)
    undo = _capture_pending_replacement(state, target_id)
    _write_recovery_record(state, undo)
    try:
        _install_pending_replacement(state, payload, candidate_data, undo)
    except (OSError, WorkspaceError):
        try:
            # The commit marker may have been renamed even if its directory fsync
            # failed, so put the durable undo record back before restoring files.
            _write_recovery_record(state, undo)
            _restore_pending_replacement(state, undo)
            _retire_recovery_record(state, target_id)
        except (OSError, WorkspaceError) as recovery_exc:
            raise WorkspaceError(
                "pending update failed and rollback could not be completed"
            ) from recovery_exc
        raise
    _retire_recovery_record(state, target_id, ignore_errors=True)


def _discard_pending(state: Path, target_id: str) -> None:
    target_id = _validate_target_id(target_id)
    _recover_pending(state, target_id)
    record = {
        "kind": "cleanup",
        "material": None,
        "purpose": "discard",
        "report_sha256": None,
        "revision": None,
        "run_id": None,
        "target_id": target_id,
        "version": _RECOVERY_RECORD_VERSION,
    }
    _write_recovery_record(state, record)
    _complete_cleanup_record(state, record)


def _handle_monitor_result(
    state: Path,
    target: Mapping[str, object],
    result: Mapping[str, object],
    run_id: str,
    *,
    candidate_source: Path | None = None,
) -> dict[str, object]:
    target_id = str(target["target_id"])
    status = result.get("status")
    if status == "unchanged":
        _discard_pending(state, target_id)
        return {"action": "unchanged", "target_id": target_id, "name": target["name"]}
    if status == "baseline":
        promoted = _promote_snapshot(
            state,
            target_id=target_id,
            expected_sha256=None,
            candidate_sha256=result.get("sha256"),
            candidate_source=candidate_source,
        )
        if promoted.get("action") != "snapshot_promoted":
            return {"action": "snapshot_conflict", "target_id": target_id}
        _discard_pending(state, target_id)
        return {
            "action": "baseline_created",
            "target_id": target_id,
            "name": target["name"],
        }
    if status != "changed":
        raise WorkspaceError("monitor returned an unsupported status")

    pending = {
        "target_id": target_id,
        "run_id": run_id,
        "revision": secrets.token_hex(16),
        "expected_sha256": result.get("previous_sha256"),
        "candidate_sha256": result.get("sha256"),
        "diff_truncated": result.get("diff_truncated") is True,
    }
    candidate = (
        _candidate_path(state, target_id)
        if candidate_source is None
        else candidate_source
    )
    candidate_data = _read_text_bytes(candidate, "candidate")
    candidate_digest = _validate_sha256(pending["candidate_sha256"], "candidate_sha256")
    if hashlib.sha256(candidate_data).hexdigest() != candidate_digest:
        raise WorkspaceError("candidate_sha256 does not match candidate")
    _write_pending_transaction(state, pending, candidate_data)
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
    run_id = _new_run_id()
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
            outcomes.append(_monitor_target(state, target, run_id))
        except (monitor.MonitorError, OSError, WorkspaceError) as exc:
            outcomes.append({
                "action": "error",
                "target_id": target["target_id"],
                "name": target["name"],
                "error": str(exc),
            })
    return {"run_id": run_id, "targets": outcomes}


def _read_pending(state: Path, target_id: str) -> dict[str, object]:
    path, _ = _pending_paths(state, target_id)
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
    if set(pending) == _PENDING_FIELDS - {"run_id"}:
        pending["run_id"] = _new_run_id()
        _write_pending_file(path, pending)
    elif set(pending) != _PENDING_FIELDS:
        raise WorkspaceError("pending decision is invalid")
    run_id = pending.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
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


def _finalize_record_matches(
    record: Mapping[str, object],
    revision: str,
    material: bool,
    report: str | None,
) -> bool:
    report_sha256 = (
        None if report is None else hashlib.sha256(report.encode("utf-8")).hexdigest()
    )
    return (
        record.get("kind") == "cleanup"
        and record.get("purpose") == "finalize"
        and record.get("revision") == revision
        and record.get("material") is material
        and record.get("report_sha256") == report_sha256
    )


def _finalized_result(root: Path, record: Mapping[str, object]) -> dict[str, object]:
    target_id = _validate_target_id(record.get("target_id"))
    material = record.get("material")
    result: dict[str, object] = {
        "action": "finalized",
        "target_id": target_id,
        "material": material,
    }
    if material is True:
        run_id = _validate_run_id(record.get("run_id"))
        result["report_path"] = str(root / "reports" / f"{run_id}.md")
    return result


def finalize(workspace: str | Path, payload: Mapping[str, object]) -> dict[str, object]:
    """Apply one semantic decision and safely advance its baseline."""
    target_id, revision, material, report = _validate_decision(payload)

    root = _workspace(workspace)
    state = _state_dir(root)
    recovery = _read_recovery_record(state, target_id)
    if recovery is not None and recovery.get("kind") == "cleanup":
        if recovery.get("purpose") == "finalize":
            if not _finalize_record_matches(recovery, revision, material, report):
                raise WorkspaceError("decision does not match pending cleanup")
            _complete_cleanup_record(state, recovery)
            return _finalized_result(root, recovery)
        _recover_pending(state, target_id)
    elif recovery is not None:
        _recover_pending(state, target_id)

    pending = _read_pending(state, target_id)
    if pending["target_id"] != target_id:
        raise WorkspaceError("pending decision target does not match")
    if pending["revision"] != revision:
        raise WorkspaceError("decision revision does not match pending review")
    if pending["diff_truncated"] is True and not material:
        return {"action": "manual_review_required", "target_id": target_id}

    promoted = _promote_snapshot(
        state,
        target_id=target_id,
        expected_sha256=pending["expected_sha256"],
        candidate_sha256=pending["candidate_sha256"],
    )
    if promoted.get("action") == "snapshot_conflict":
        return {"action": "snapshot_conflict", "target_id": target_id}

    report_path: str | None = None
    if report is not None:
        report_path = str(
            _write_report(root, str(pending["run_id"]), target_id, report)
        )
    cleanup = _finalize_cleanup_record(
        target_id,
        revision,
        material,
        report,
        str(pending["run_id"]),
    )
    _write_recovery_record(state, cleanup)
    _complete_cleanup_record(state, cleanup)
    result = _finalized_result(root, cleanup)
    if report_path is not None and result.get("report_path") != report_path:
        raise WorkspaceError("finalized report path mismatch")
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
