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
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit

import monitor

_INTERNAL_DIR = "internal"
_OUTPUT_DIR = "output"
_STATE_DIR = "state"
_REPORT_DIR = "report"
_PENDING_RECOVERY_DIR = "recovery"
_MAX_CSV_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024
_MAX_RECOVERY_RECORD_BYTES = 3 * _MAX_SNAPSHOT_BYTES + 1024 * 1024
_REQUIRED_FIELDS = {"name", "url"}
_INTEREST_TEXT_FIELDS = {"name", "publisher", "category", "keywords", "criteria"}
_INTEREST_FIELDS = _INTEREST_TEXT_FIELDS | {"priority", "enabled"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _INTEREST_FIELDS
_DITTO_TOKENS = {'"', "〃", "同上", "同左"}
_PENDING_BASE_FIELDS = {
    "candidate_sha256",
    "diff_truncated",
    "expected_sha256",
    "revision",
    "run_id",
    "target_id",
}
_PENDING_TEXT_FIELDS = {"diff", "name", "url"}
_DEFAULT_LINK_DEPTH = 1
_MAX_LINKS = 100
_MAX_LINK_BYTES = 2 * 1024 * 1024
_MAX_LINK_TOTAL_BYTES = 10 * 1024 * 1024
_MAX_LINK_TEXT_BYTES = 8192
_MAX_LINK_REVIEW_BYTES = 65_536
_LINK_TIMEOUT = 60.0
_NAVIGATION_HASH_RE = re.compile(
    r"^\[(?:a|area|link):(?:href|url):sha256:([a-f0-9]{64})\]$", re.MULTILINE
)
_PENDING_FIELDS = _PENDING_BASE_FIELDS | _PENDING_TEXT_FIELDS | {"interests"}
_PENDING_FIELD_SETS = (
    frozenset(_PENDING_FIELDS),
    frozenset(_PENDING_FIELDS | {"link_review"}),
)
_TARGET_ID_PART_RE = re.compile(r"[^a-z0-9]+")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_RECORD_VERSION = 1
_EVIDENCE_DIR = "evidence"
_ARCHIVE_INTENT_VERSION = 2
_EVIDENCE_SCHEMA = "wsum.evidence/1"
_LINKS_SCHEMA = "wsum.evidence.links/1"
# Per-file ceilings: parent reuses the snapshot bound; the others allow generous
# headroom over the 64 KiB diff and 64 KiB linked-review text the core retains.
_ARCHIVE_PAYLOAD_LIMITS = {
    "parent.txt": _MAX_SNAPSHOT_BYTES,
    "diff.txt": 1024 * 1024,
    "links.json": 1024 * 1024,
}
_MAX_ARCHIVE_METADATA_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_REPORT_BYTES = 1024 * 1024
_MAX_ARCHIVE_INTENT_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_RECEIPT_BYTES = 64 * 1024
_INTENT_FIELDS = frozenset({
    "archived_at",
    "candidate_sha256",
    "diff",
    "expected_sha256",
    "ingestion_id",
    "kind",
    "metadata",
    "metadata_sha256",
    "report",
    "report_sha256",
    "revision",
    "run_id",
    "target_id",
    "version",
})
_RECEIPT_FIELDS = frozenset({
    "archived_at",
    "ingestion_id",
    "manifest_sha256",
    "material",
    "report_section_sha256",
    "revision",
    "run_id",
    "schema",
    "target_id",
})


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
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceError(f"{description} is unavailable") from exc
        try:
            info = path.lstat()
        except OSError as exc:
            raise WorkspaceError(f"{description} is unavailable") from exc
    except OSError as exc:
        raise WorkspaceError(f"{description} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"{description} must be a non-symlink directory")
    if sync_parent:
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot fsync parent directory for {description}"
            ) from exc
    return path


def _internal_dir(workspace: Path) -> Path:
    return _ensure_directory(
        workspace / _INTERNAL_DIR,
        "workspace internal directory",
        sync_parent=True,
    )


def _output_dir(workspace: Path) -> Path:
    return _ensure_directory(
        workspace / _OUTPUT_DIR,
        "workspace output directory",
        sync_parent=True,
    )


def _state_dir(workspace: Path) -> Path:
    return _ensure_directory(
        _internal_dir(workspace) / _STATE_DIR,
        "workspace state directory",
        sync_parent=True,
    )


def _report_dir(workspace: Path) -> Path:
    return _ensure_directory(
        _output_dir(workspace) / _REPORT_DIR,
        "workspace report directory",
        sync_parent=True,
    )


def _read_csv(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError("targets CSV is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("targets CSV must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > _MAX_CSV_BYTES:
        raise WorkspaceError("targets CSV size is invalid")
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_CSV_BYTES + 1)
        if len(data) > _MAX_CSV_BYTES:
            raise WorkspaceError("targets CSV size is invalid")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkspaceError("targets CSV must be UTF-8 CSV") from exc


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


@contextmanager
def _integer_text_limit() -> Generator[None]:
    """Allow decimal metadata within the bounded CSV/pending file limits."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def _validate_target_header(fieldnames: list[str] | None) -> list[str]:
    """Validate the supported, unambiguous CSV header."""
    if fieldnames is None or len(fieldnames) != len(set(fieldnames)):
        raise WorkspaceError("targets CSV must have a unique header row")
    fields = set(fieldnames)
    if not _REQUIRED_FIELDS.issubset(fields):
        raise WorkspaceError("targets CSV requires name and url columns")
    if fields - _ALLOWED_FIELDS:
        raise WorkspaceError("targets CSV contains unsupported columns")
    return fieldnames


def _normalize_target_row(
    fieldnames: list[str], row: Sequence[object], row_number: int
) -> tuple[str, dict[str, object]] | None:
    """Validate one interest before grouping, including disabled rows."""
    if len(row) > len(fieldnames):
        raise WorkspaceError(f"row {row_number}: too many columns")
    values: dict[str, str] = {}
    for key, value in zip(fieldnames, row, strict=False):
        if not isinstance(value, str):
            raise WorkspaceError(f"row {row_number}: {key}: invalid CSV value")
        values[key] = value.strip()
    if not any(values.values()):
        return None
    for field in _INTEREST_TEXT_FIELDS | {"url"}:
        if values.get(field) in _DITTO_TOKENS:
            raise WorkspaceError(
                f"row {row_number}: {field}: replace ditto with an explicit value"
            )
    if not values.get("name"):
        raise WorkspaceError(f"row {row_number}: name must be non-empty")
    try:
        url = _validate_url(values.get("url", ""))
    except WorkspaceError as exc:
        raise WorkspaceError(f"row {row_number}: {exc}") from exc
    priority_text = values.get("priority", "")
    priority = None
    if priority_text:
        if not re.fullmatch(r"[0-9]+", priority_text):
            raise WorkspaceError(
                f"row {row_number}: priority must be a positive ASCII decimal integer"
            )
        priority = int(priority_text)
        if priority <= 0:
            raise WorkspaceError(
                f"row {row_number}: priority must be a positive ASCII decimal integer"
            )
    interest: dict[str, object] = {
        field: values.get(field, "") for field in _INTEREST_TEXT_FIELDS
    }
    interest["priority"] = priority
    interest["enabled"] = _parse_enabled(values.get("enabled", ""), row_number)
    return url, interest


def _group_target_interests(
    rows: Sequence[tuple[str, dict[str, object]]],
) -> list[dict[str, object]]:
    """Group exact URLs while preserving URL order and every interest row."""
    groups: dict[str, list[dict[str, object]]] = {}
    for url, interest in rows:
        groups.setdefault(url, []).append(interest)
    targets: list[dict[str, object]] = []
    target_ids: set[str] = set()
    for url, interests in groups.items():
        target_id = _target_id(url)
        if target_id in target_ids:
            raise WorkspaceError("duplicate_target_id")
        target_ids.add(target_id)
        selected = next((item for item in interests if item["enabled"]), interests[0])
        enabled = any(item["enabled"] for item in interests)
        targets.append({
            "target_id": target_id,
            "name": selected["name"],
            "url": url,
            "enabled": enabled,
            "action": "monitor" if enabled else "skip_disabled",
            "interests": interests,
        })
    return targets


def load_targets(targets: str | Path) -> list[dict[str, object]]:
    """Load, validate, and normalize all interests from the supplied CSV file."""
    previous_limit = csv.field_size_limit(_MAX_CSV_BYTES)
    row_number = 1
    try:
        reader = csv.reader(io.StringIO(_read_csv(Path(targets))), strict=True)
        fieldnames = _validate_target_header(next(reader, None))
        rows: list[tuple[str, dict[str, object]]] = []
        with _integer_text_limit():
            for row_number, row in enumerate(reader, start=2):
                normalized = _normalize_target_row(fieldnames, row, row_number)
                if normalized is not None:
                    rows.append(normalized)
    except csv.Error as exc:
        raise WorkspaceError(f"CSV record after {row_number}: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)
    if not rows:
        raise WorkspaceError("targets CSV contains no targets")
    return _group_target_interests(rows)



def _existing_pending_paths(
    state: Path, target_id: str
) -> tuple[Path, Path] | None:
    """Resolve the current pending transaction paths without creating them."""
    target_id = _validate_target_id(target_id)
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    target = pending / target_id
    info = _optional_lstat(target, "pending target")
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("pending target must be a non-symlink directory")
    return target / "state.json", target / "candidate.txt"

def _pending_paths(
    state: Path, target_id: str, *, create: bool = False
) -> tuple[Path, Path]:
    """Resolve pending metadata and candidate paths from one layout."""
    target_id = _validate_target_id(target_id)
    existing = _existing_pending_paths(state, target_id)
    if existing is not None:
        return existing
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
    candidate_data: bytes | None = None,
) -> dict[str, object]:
    """Atomically promote a candidate when its expected baseline matches."""
    state = _workspace(state)
    target_id = _validate_target_id(target_id)
    expected = _validate_sha256(expected_sha256, "expected_sha256", allow_none=True)
    candidate_digest = _validate_sha256(candidate_sha256, "candidate_sha256")
    if candidate_data is None:
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

    reports_dir = _report_dir(workspace)
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
    state: Path,
    target: Mapping[str, object],
    run_id: str,
    *,
    link_depth: int = _DEFAULT_LINK_DEPTH,
    max_links: int = _MAX_LINKS,
) -> dict[str, object]:
    target_id = _validate_target_id(target["target_id"])
    _recover_pending(state, target_id)
    snapshots = _ensure_directory(
        state / "snapshots", "snapshot directory", sync_parent=True
    )
    previous = snapshots / f"{target_id}.txt"
    candidate_data: bytes | None = None
    with tempfile.TemporaryDirectory(prefix=".monitor-", dir=state) as staging:
        candidate = Path(staging) / "candidate.txt"
        arguments = ["--url", str(target["url"]), "--output", str(candidate)]
        if previous.exists():
            arguments.extend(["--previous", str(previous)])
        namespace = monitor._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            arguments
        )
        result = monitor.run(namespace)
        link_collection = result.get("links")
        has_links = (
            bool(link_collection)
            or bool(getattr(link_collection, "omitted_hashes", ()))
            or bool(getattr(link_collection, "overflow_count", 0))
        )
        if link_depth > 0 and result.get("status") == "changed" and has_links:
            result["link_review"] = _follow_added_links(
                result,
                _read_snapshot(previous) or b"",
                source_url=str(target["url"]),
                link_depth=link_depth,
                max_links=max_links,
            )
        if result.get("status") in {"baseline", "changed"}:
            candidate_data = _read_text_bytes(candidate, "candidate")
    return _handle_monitor_result(
        state, target, result, run_id, candidate_data=candidate_data
    )


def _validate_link_options(link_depth: int, max_links: int) -> None:
    """Validate configurable link traversal limits."""
    if type(link_depth) is not int or link_depth < 0:
        raise WorkspaceError("link_depth must be a non-negative integer")
    if type(max_links) is not int or not 1 <= max_links <= _MAX_LINKS:
        raise WorkspaceError(f"max_links must be an integer from 1 to {_MAX_LINKS}")


def _follow_added_links(  # ruff: ignore[too-many-locals, too-many-statements]
    result: Mapping[str, object],
    previous: bytes,
    *,
    source_url: str = "",
    link_depth: int = _DEFAULT_LINK_DEPTH,
    max_links: int = _MAX_LINKS,
) -> dict[str, object]:
    """Fetch newly added navigation links breadth-first within shared budgets."""
    _validate_link_options(link_depth, max_links)
    if link_depth == 0:
        return {"documents": [], "omitted": 0, "incomplete": False}

    link_collection = result["links"]
    links = cast("dict[str, str]", link_collection)
    previous_hashes = set(_NAVIGATION_HASH_RE.findall(previous.decode("utf-8")))
    omitted_hashes = set(getattr(link_collection, "omitted_hashes", ()))
    existing_urls = {url for digest, url in links.items() if digest in previous_hashes}
    source_urls = {str(result["source_url"]).split("#", 1)[0], source_url}
    added = sorted(set(links.values()) - existing_urls - source_urls)
    queue = [(url, 1) for url in added[:max_links]]
    seen = set(source_urls)
    seen.update(url for url, _depth in queue)
    scheduled = len(queue)
    documents: list[dict[str, object]] = []
    omitted = len(omitted_hashes - previous_hashes)
    omitted += getattr(link_collection, "overflow_count", 0)
    omitted += max(0, len(added) - max_links)
    incomplete = omitted > 0
    deadline = monotonic() + _LINK_TIMEOUT
    remaining_bytes = _MAX_LINK_TOTAL_BYTES
    review_bytes = _MAX_LINK_REVIEW_BYTES
    index = 0
    while index < len(queue):
        remaining_time = deadline - monotonic()
        if remaining_time <= 0 or remaining_bytes <= 0 or review_bytes <= 0:
            omitted += len(queue) - index
            incomplete = True
            break
        url, depth = queue[index]
        index += 1
        entry: dict[str, object] = {"url": url}
        byte_limit = min(remaining_bytes, _MAX_LINK_BYTES)
        try:
            document = monitor.fetch_document(
                url, timeout=min(remaining_time, 30.0), max_bytes=byte_limit
            )
            child_links = monitor.LinkCollection() if depth < link_depth else None
            text = _linked_document_text(document, links=child_links)
            bounded = monitor._utf8_prefix(  # pyright: ignore[reportPrivateUsage]
                text, min(review_bytes, _MAX_LINK_TEXT_BYTES)
            )
            truncated = bounded != text
            entry.update({
                "source_url": document.source_url,
                "text": bounded,
                "truncated": truncated,
            })
            review_bytes -= len(bounded.encode("utf-8"))
            incomplete = incomplete or truncated
            byte_limit = len(document.body)
            if child_links is not None:
                child_omitted = (
                    len(child_links.omitted_hashes) + child_links.overflow_count
                )
                omitted += child_omitted
                incomplete = incomplete or child_omitted > 0
                seen.add(document.source_url.split("#", 1)[0])
                for nested_url in sorted(set(child_links.values())):
                    if nested_url in seen:
                        continue
                    seen.add(nested_url)
                    if scheduled >= max_links:
                        omitted += 1
                        incomplete = True
                        continue
                    queue.append((nested_url, depth + 1))
                    scheduled += 1
        except (monitor.MonitorError, OSError, ValueError) as exc:
            entry["error"] = str(exc)
            incomplete = True
        # Reserve the full allowance for failed requests, actual bytes on success.
        remaining_bytes -= byte_limit
        documents.append(entry)
    return {"documents": documents, "omitted": omitted, "incomplete": incomplete}


def _linked_document_text(
    document: monitor.Document, *, links: dict[str, str] | None = None
) -> str:
    """Normalize child content and reject empty extraction."""
    text = monitor.normalize_document(document, links=links)
    if not text:
        raise monitor.MonitorError("normalization produced empty content")
    return text


def _validate_link_review(value: object) -> dict[str, object]:
    """Validate the bounded persisted child-document review context."""
    if not isinstance(value, dict):
        raise WorkspaceError("pending link review is invalid")
    review = cast("dict[str, object]", value)
    if set(review) != {
        "documents",
        "omitted",
        "incomplete",
    }:
        raise WorkspaceError("pending link review is invalid")
    documents = review["documents"]
    omitted = review["omitted"]
    if (
        not isinstance(documents, list)
        or len(cast("list[object]", documents)) > _MAX_LINKS
        or type(omitted) is not int
        or omitted < 0
        or type(review["incomplete"]) is not bool
    ):
        raise WorkspaceError("pending link review is invalid")
    for entry in cast("list[object]", documents):
        if not isinstance(entry, dict):
            raise WorkspaceError("pending link review is invalid")
        item = cast("dict[str, object]", entry)
        if set(item) not in (
            {"url", "error"},
            {"url", "source_url", "text", "truncated"},
        ):
            raise WorkspaceError("pending link review is invalid")
        for field in set(item) - {"truncated"}:
            if not isinstance(item[field], str):
                raise WorkspaceError("pending link review is invalid")
        _validate_url(str(item["url"]))
        if "truncated" in item and type(item["truncated"]) is not bool:
            raise WorkspaceError("pending link review is invalid")
    return review


def _optional_lstat(path: Path, description: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError(f"cannot stat {description}") from exc



def _remove_pending(state: Path, target_id: str) -> None:
    pending = _ensure_directory(
        state / "pending", "pending directory", sync_parent=True
    )
    target_id = _validate_target_id(target_id)
    target = pending / target_id
    info = _optional_lstat(target, "pending target")
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
    ):
        raise WorkspaceError("pending target must be a non-symlink directory")
    try:
        if info is not None:
            shutil.rmtree(target)
        _fsync_directory(pending)
    except OSError as exc:
        raise WorkspaceError("cannot remove pending transaction") from exc

def _serialize_pending(payload: Mapping[str, object]) -> bytes:
    """Preflight the complete escaped transaction against its backup ceiling."""
    _validate_pending_context(payload)
    with _integer_text_limit():
        data = (json.dumps(payload, sort_keys=True) + "\n").encode()
    if len(data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("pending decision size is invalid")
    return data


def _write_pending_file(destination: Path, payload: Mapping[str, object]) -> None:
    data = _serialize_pending(payload)
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
    kind = record.get("kind")
    if kind == "archive":
        if (
            record.get("version") != _ARCHIVE_INTENT_VERSION
            or record.get("target_id") != target_id
        ):
            raise WorkspaceError("pending recovery record is invalid")
        return _validate_archive_intent(record)
    if (
        type(record.get("version")) is not int
        or record["version"] != _RECOVERY_RECORD_VERSION
        or record.get("target_id") != target_id
    ):
        raise WorkspaceError("pending recovery record is invalid")
    if kind == "replace":
        if set(record) != {
            "group_dir_existed",
            "kind",
            "old_candidate",
            "old_state",
            "target_id",
            "version",
        }:
            raise WorkspaceError("pending recovery record is invalid")
        if not isinstance(record.get("group_dir_existed"), bool):
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


def _read_recovery_file(
    path: Path, target_id: str, description: str
) -> dict[str, object] | None:
    info = _optional_lstat(path, description)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"{description} must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise WorkspaceError(f"{description} must be private")
    if info.st_size <= 0 or info.st_size > _MAX_RECOVERY_RECORD_BYTES:
        raise WorkspaceError(f"{description} size is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"{description} is invalid") from exc
    return _validate_recovery_record(value, target_id)


def _recovery_record_path(state: Path, target_id: str) -> Path | None:
    directory = _recovery_directory(state, create=False)
    if directory is None:
        return None
    return directory / f"{_validate_target_id(target_id)}.json"


def _commit_record_path(state: Path, target_id: str) -> Path | None:
    directory = _recovery_directory(state, create=False)
    if directory is None:
        return None
    # Appending the suffix avoids collisions with another target ID containing dots.
    return directory / f"{_validate_target_id(target_id)}.json.commit"



def _read_recovery_record(state: Path, target_id: str) -> dict[str, object] | None:
    target_id = _validate_target_id(target_id)
    path = _recovery_record_path(state, target_id)
    if path is None:
        return None
    record = _read_recovery_file(path, target_id, "pending recovery record")
    if record is not None and record.get("kind") == "commit":
        raise WorkspaceError("pending recovery record is invalid")
    return record

def _read_commit_record(state: Path, target_id: str) -> dict[str, object] | None:
    target_id = _validate_target_id(target_id)
    path = _commit_record_path(state, target_id)
    if path is None:
        return None
    record = _read_recovery_file(path, target_id, "pending commit record")
    if record is not None and record.get("kind") != "commit":
        raise WorkspaceError("pending commit record is invalid")
    return record


def _write_recovery_record_at(
    state: Path, record: Mapping[str, object], *, commit: bool
) -> None:
    target_id = _validate_target_id(record.get("target_id"))
    normalized = _validate_recovery_record(dict(record), target_id)
    if commit and normalized.get("kind") != "commit":
        raise WorkspaceError("pending commit record is invalid")
    data = (json.dumps(normalized, sort_keys=True) + "\n").encode()
    if not data or len(data) > _MAX_RECOVERY_RECORD_BYTES:
        raise WorkspaceError("pending recovery record size is invalid")
    directory = _recovery_directory(state, create=True)
    if directory is None:
        raise WorkspaceError("pending recovery directory is unavailable")
    path = directory / (f"{target_id}.json.commit" if commit else f"{target_id}.json")
    description = "pending commit record" if commit else "pending recovery record"
    current = _optional_lstat(path, description)
    if current is not None and (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) & 0o077
    ):
        raise WorkspaceError(f"{description} is unsafe")
    temporary = _write_temporary_file(path, data, description)
    try:
        temporary.replace(path)
    except OSError as exc:
        raise WorkspaceError(f"cannot persist {description}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(directory)
    except OSError as exc:
        raise WorkspaceError(f"cannot fsync {description} directory") from exc


def _write_recovery_record(state: Path, record: Mapping[str, object]) -> None:
    _write_recovery_record_at(state, record, commit=False)


def _write_commit_record(state: Path, record: Mapping[str, object]) -> None:
    _write_recovery_record_at(state, record, commit=True)


def _retire_recovery_file(
    state: Path,
    target_id: str,
    *,
    commit: bool,
    ignore_errors: bool,
) -> None:
    directory = _recovery_directory(state, create=False)
    if directory is None:
        return
    target_id = _validate_target_id(target_id)
    path = directory / (f"{target_id}.json.commit" if commit else f"{target_id}.json")
    description = "pending commit record" if commit else "pending recovery record"
    info = _optional_lstat(path, description)
    if info is None:
        return
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise WorkspaceError(f"{description} is unsafe")
    try:
        path.unlink()
        _fsync_directory(directory)
    except OSError as exc:
        if not ignore_errors:
            raise WorkspaceError(f"cannot retire {description}") from exc


def _retire_recovery_record(
    state: Path, target_id: str, *, ignore_errors: bool = False
) -> None:
    _retire_recovery_file(state, target_id, commit=False, ignore_errors=ignore_errors)


def _retire_commit_record(
    state: Path, target_id: str, *, ignore_errors: bool = False
) -> None:
    _retire_recovery_file(state, target_id, commit=True, ignore_errors=ignore_errors)


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
        "old_candidate": (
            None if old_candidate is None else base64.b64encode(old_candidate).decode()
        ),
        "old_state": (
            None if old_state is None else base64.b64encode(old_state).decode()
        ),
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
    target = pending / target_id
    target_info = _optional_lstat(target, "pending target")
    if target_info is not None and (
        stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode)
    ):
        raise WorkspaceError("pending target must be a non-symlink directory")

    old_state = _decode_recovery_backup(record.get("old_state"))
    old_candidate = _decode_recovery_backup(record.get("old_candidate"))
    if record["group_dir_existed"]:
        target = _ensure_directory(
            target, "pending target directory", sync_parent=True
        )
        _remove_pending_write_temporaries(target)
        _restore_transaction_file(target / "candidate.txt", old_candidate, "candidate")
        _restore_transaction_file(target / "state.json", old_state, "pending decision")
        try:
            _fsync_directory(target)
        except OSError as exc:
            raise WorkspaceError("cannot fsync restored pending transaction") from exc
    elif target_info is not None:
        _remove_pending_write_temporaries(target)
        _restore_transaction_file(target / "candidate.txt", None, "candidate")
        _restore_transaction_file(target / "state.json", None, "pending decision")
        try:
            target.rmdir()
        except OSError as exc:
            raise WorkspaceError("cannot restore pending target directory") from exc

    try:
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


def _replacement_matches_commit(
    state: Path, target_id: str, commit: Mapping[str, object]
) -> bool:
    target_id = _validate_target_id(target_id)
    state_info = _optional_lstat(state, "workspace state directory")
    if (
        state_info is None
        or stat.S_ISLNK(state_info.st_mode)
        or not stat.S_ISDIR(state_info.st_mode)
    ):
        raise WorkspaceError("workspace state must be a non-symlink directory")
    pending_dir = state / "pending"
    target_dir = state / "pending" / target_id
    for directory, description in (
        (pending_dir, "pending directory"),
        (target_dir, "pending target directory"),
    ):
        info = _optional_lstat(directory, description)
        if info is None:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError(f"{description} must be a non-symlink directory")
    state_path = target_dir / "state.json"
    candidate_path = target_dir / "candidate.txt"
    state_info = _optional_lstat(state_path, "pending decision")
    candidate_info = _optional_lstat(candidate_path, "candidate")
    if state_info is None or candidate_info is None:
        return False
    if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISREG(state_info.st_mode):
        raise WorkspaceError("pending decision must be a regular non-symlink file")
    if stat.S_ISLNK(candidate_info.st_mode) or not stat.S_ISREG(candidate_info.st_mode):
        raise WorkspaceError("candidate must be a regular non-symlink file")
    try:
        pending = _read_pending_json(state_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, WorkspaceError):
        return False
    if not isinstance(pending, dict):
        return False
    pending = cast("dict[str, object]", pending)
    if frozenset(pending) not in _PENDING_FIELD_SETS:
        return False
    if pending.get("target_id") != target_id or pending.get("revision") != commit.get(
        "revision"
    ):
        return False
    run_id = pending.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        return False
    if type(pending.get("diff_truncated")) is not bool:
        return False
    try:
        _validate_pending_context(pending)
    except WorkspaceError:
        return False
    candidate_data = _read_text_bytes(candidate_path, "candidate")
    try:
        candidate_sha256 = _validate_sha256(
            pending.get("candidate_sha256"), "candidate_sha256"
        )
        _validate_sha256(
            pending.get("expected_sha256"), "expected_sha256", allow_none=True
        )
    except WorkspaceError:
        return False
    return hashlib.sha256(candidate_data).hexdigest() == candidate_sha256


def _replacement_previous_revision(record: Mapping[str, object]) -> str | None:
    old_state = _decode_recovery_backup(record.get("old_state"))
    if old_state is None:
        return None
    try:
        with _integer_text_limit():
            value = json.loads(old_state.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("pending recovery record is invalid") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("pending recovery record is invalid")
    _validate_pending_context(cast("dict[str, object]", value))
    previous = cast("dict[str, object]", value).get("revision")
    if not isinstance(previous, str) or not _REVISION_RE.fullmatch(previous):
        raise WorkspaceError("pending recovery record is invalid")
    return previous


def _recover_pending(
    state: Path, target_id: str, *, revision: str | None = None
) -> dict[str, object] | None:
    target_id = _validate_target_id(target_id)
    record = _read_recovery_record(state, target_id)
    if record is not None and record["kind"] == "archive":
        # An archive obligation completes (or stays blocked) before any other
        # recovery, discard, or replacement may touch the pending evidence.
        _complete_archive(state.parent.parent, state, record)
        return None
    commit = _read_commit_record(state, target_id)
    if commit is not None:
        if record is None:
            # The undo record is retired only after the commit marker is durable.
            _retire_commit_record(state, target_id)
            return None
        if record.get("kind") != "replace":
            raise WorkspaceError("pending recovery records conflict")
        if revision is None:
            raise WorkspaceError(
                "pending replacement recovery requires a decision revision"
            )
        if revision == commit.get("revision"):
            if not _replacement_matches_commit(state, target_id, commit):
                raise WorkspaceError("pending committed replacement is incomplete")
            _retire_recovery_record(state, target_id)
            _retire_commit_record(state, target_id)
            return None
        if revision != _replacement_previous_revision(record):
            raise WorkspaceError("decision revision does not match pending replacement")
        # Remove the commit marker durably before restoring the prior review. If
        # this fails, leave both records intact so finalize can choose by revision.
        _retire_commit_record(state, target_id)
        _restore_pending_replacement(state, record)
        _retire_recovery_record(state, target_id)
        return None
    if record is None:
        return None
    kind = record["kind"]
    if kind == "replace":
        _restore_pending_replacement(state, record)
        _retire_recovery_record(state, target_id)
        return None
    _complete_cleanup_record(state, record)
    return record



def _install_pending_replacement(
    state: Path,
    payload: Mapping[str, object],
    candidate_data: bytes,
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

    durable_candidate = _read_text_bytes(target / "candidate.txt", "candidate")
    if durable_candidate != candidate_data:
        raise WorkspaceError("pending candidate read-back mismatch")
    durable_state = _read_pending(state, target_id)
    if durable_state != dict(payload):
        raise WorkspaceError("pending decision read-back mismatch")

    _write_commit_record(
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
    _serialize_pending(payload)
    target_id = _validate_target_id(payload.get("target_id"))
    _recover_pending(state, target_id)
    undo = _capture_pending_replacement(state, target_id)
    _write_recovery_record(state, undo)
    try:
        _install_pending_replacement(state, payload, candidate_data)
    except (OSError, WorkspaceError):
        try:
            # Keep the undo record untouched until the separate commit marker is
            # durably removed. A failed rollback can then replay this backup.
            _retire_commit_record(state, target_id)
            _restore_pending_replacement(state, undo)
            _retire_recovery_record(state, target_id)
        except (OSError, WorkspaceError) as recovery_exc:
            raise WorkspaceError(
                "pending update failed and rollback could not be completed"
            ) from recovery_exc
        raise
    try:
        _retire_recovery_record(state, target_id)
    except WorkspaceError:
        # The durable commit marker keeps a returned replacement recoverable if
        # undo retirement cannot be confirmed.
        return
    _retire_commit_record(state, target_id, ignore_errors=True)


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
    candidate_data: bytes | None = None,
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
            candidate_data=candidate_data,
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

    diff = result.get("diff", "")
    if not isinstance(diff, str):
        raise WorkspaceError("monitor returned an invalid diff")
    pending = {
        "target_id": target_id,
        "run_id": run_id,
        "revision": secrets.token_hex(16),
        "expected_sha256": result.get("previous_sha256"),
        "candidate_sha256": result.get("sha256"),
        "diff_truncated": result.get("diff_truncated") is True,
        "name": str(target["name"]),
        "url": str(target.get("url", "")),
        "diff": diff,
        "interests": _enabled_interests(target),
    }
    if "link_review" in result:
        link_review = _validate_link_review(result["link_review"])
        pending["link_review"] = link_review
        if link_review["incomplete"]:
            pending["diff_truncated"] = True
    if candidate_data is None:
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
        "run_id": run_id,
        "target_id": target_id,
        "revision": pending["revision"],
        "name": target["name"],
        "url": target["url"],
        "interests": pending["interests"],
        "diff": diff,
        "diff_truncated": pending["diff_truncated"],
        **({"link_review": pending["link_review"]} if "link_review" in pending else {}),
    }


def _compact_review(review: Mapping[str, object]) -> dict[str, object]:
    """Return the small review handle used by orchestration layers."""
    keys = (
        "action",
        "run_id",
        "target_id",
        "revision",
        "name",
        "diff_truncated",
    )
    return {key: review[key] for key in keys}


def check(
    workspace: str | Path,
    targets: str | Path,
    *,
    compact: bool = False,
    link_depth: int = _DEFAULT_LINK_DEPTH,
    max_links: int = _MAX_LINKS,
) -> dict[str, object]:
    """Check targets without refetching any target that already has a review."""
    _validate_link_options(link_depth, max_links)
    root = _workspace(workspace)
    configured_targets = load_targets(targets)
    targets_by_id = {str(target["target_id"]): target for target in configured_targets}
    existing = _collect_pending_reviews(
        root, _target_review_contexts(configured_targets)
    )
    existing_reviews = cast("list[dict[str, object]]", existing["reviews"])
    retained_reviews: list[dict[str, object]] = []
    pending_ids: set[str] = set()
    for review in existing_reviews:
        target_id = str(review["target_id"])
        target = targets_by_id.get(target_id)
        if target is None or target["action"] == "skip_disabled":
            discard_pending(root, target_id)
            continue
        retained_reviews.append(review)
        pending_ids.add(target_id)

    state = _state_dir(root)
    run_id = _new_run_id()
    outcomes: list[dict[str, object]] = list(retained_reviews)
    for target in configured_targets:
        if str(target["target_id"]) in pending_ids:
            continue
        if target["action"] == "skip_disabled":
            outcomes.append({
                "action": "skipped",
                "target_id": target["target_id"],
                "name": target["name"],
            })
            continue
        try:
            outcome = _monitor_target(
                state,
                target,
                run_id,
                link_depth=link_depth,
                max_links=max_links,
            )
            outcomes.append(
                _compact_review(outcome)
                if compact and outcome.get("action") == "review"
                else outcome
            )
        except (monitor.MonitorError, OSError, WorkspaceError) as exc:
            outcomes.append({
                "action": "error",
                "target_id": target["target_id"],
                "name": target["name"],
                "error": str(exc),
            })
    return {"run_id": run_id, "targets": outcomes}


def _validate_interests(value: object) -> list[dict[str, object]]:
    """Validate the authoritative nested interest schema without scalar fallback."""
    if not isinstance(value, list):
        raise WorkspaceError("pending interests are invalid")
    interests: list[dict[str, object]] = []
    for raw in cast("list[object]", value):
        if (
            not isinstance(raw, dict)
            or set(cast("dict[str, object]", raw)) != _INTEREST_FIELDS
        ):
            raise WorkspaceError("pending interests are invalid")
        interest = cast("dict[str, object]", raw)
        for field in _INTEREST_TEXT_FIELDS:
            text = interest[field]
            if not isinstance(text, str) or text != text.strip():
                raise WorkspaceError("pending interests are invalid")
        priority = interest["priority"]
        if (
            not interest["name"]
            or type(interest["enabled"]) is not bool
            or (priority is not None and (type(priority) is not int or priority <= 0))
        ):
            raise WorkspaceError("pending interests are invalid")
        interests.append(interest)
    return interests



def _validate_pending_context(pending: Mapping[str, object]) -> None:
    """Validate the current persisted review context."""
    for field in _PENDING_TEXT_FIELDS:
        if not isinstance(pending.get(field), str):
            raise WorkspaceError("pending decision is invalid")
    _validate_interests(pending.get("interests"))
    if "link_review" in pending:
        _validate_link_review(pending["link_review"])

def _read_pending_json(path: Path) -> object:
    """Bound both the stated and actual serialized transaction size."""
    info = path.lstat()
    if info.st_size <= 0 or info.st_size > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("pending decision size is invalid")
    with path.open("rb") as stream:
        data = stream.read(_MAX_SNAPSHOT_BYTES + 1)
    if len(data) > _MAX_SNAPSHOT_BYTES:
        raise WorkspaceError("pending decision size is invalid")
    with _integer_text_limit():
        return json.loads(data.decode("utf-8"))



def _read_pending(state: Path, target_id: str) -> dict[str, object]:
    path, _ = _pending_paths(state, target_id)
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceError("no valid pending decision exists for target") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("pending decision must be a regular non-symlink file")
    try:
        value = _read_pending_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("no valid pending decision exists for target") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("pending decision is invalid")
    pending = cast("dict[str, object]", value)
    if frozenset(pending) not in _PENDING_FIELD_SETS:
        raise WorkspaceError("pending decision is invalid")
    _validate_pending_context(pending)
    run_id = pending.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise WorkspaceError("pending decision is invalid")
    if type(pending.get("diff_truncated")) is not bool:
        raise WorkspaceError("pending decision is invalid")
    return pending


def _pending_target_ids(state: Path) -> list[str]:
    """List current pending target directories without following unsafe entries."""
    pending_dir = state / "pending"
    try:
        info = pending_dir.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise WorkspaceError("pending directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("pending directory must be a non-symlink directory")

    target_ids: list[str] = []
    try:
        entries = sorted(pending_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise WorkspaceError("cannot list pending directory") from exc
    for entry in entries:
        try:
            entry_info = entry.lstat()
        except OSError as exc:
            raise WorkspaceError("cannot stat pending entry") from exc
        if entry.name.startswith("."):
            if (
                entry.name.endswith(".tmp")
                and stat.S_ISREG(entry_info.st_mode)
                and not stat.S_ISLNK(entry_info.st_mode)
            ):
                continue
            raise WorkspaceError("pending directory contains an unsafe entry")
        if not stat.S_ISDIR(entry_info.st_mode) or stat.S_ISLNK(entry_info.st_mode):
            raise WorkspaceError("pending directory contains an unsupported entry")
        target_ids.append(_validate_target_id(entry.name))
    return target_ids

def _enabled_interests(context: Mapping[str, object]) -> list[dict[str, object]]:
    """Return the enabled interests from the current authoritative schema."""
    return [
        item
        for item in _validate_interests(context.get("interests"))
        if item["enabled"]
    ]


def _target_review_contexts(
    targets: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    return {
        str(target["target_id"]): {
            "name": target["name"],
            "url": target["url"],
            "interests": _enabled_interests(target),
        }
        for target in targets
    }

def _current_review_contexts(
    targets: str | Path | None,
) -> dict[str, dict[str, object]] | None:
    """Return supplied CSV review context keyed by stable target ID."""
    if targets is None:
        return None
    try:
        configured_targets = load_targets(targets)
    except WorkspaceError:
        return None
    return _target_review_contexts(configured_targets)



def _saved_review_context(
    target_id: str, pending: Mapping[str, object]
) -> dict[str, object]:
    del target_id
    return {
        "name": pending["name"],
        "url": pending["url"],
        "interests": _enabled_interests(pending),
    }


def _pending_review(
    state: Path,
    target_id: str,
    *,
    current_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    pending = _read_pending(state, target_id)
    candidate_data = _read_text_bytes(_candidate_path(state, target_id), "candidate")
    candidate_sha256 = _validate_sha256(
        pending.get("candidate_sha256"), "candidate_sha256"
    )
    if hashlib.sha256(candidate_data).hexdigest() != candidate_sha256:
        raise WorkspaceError("candidate_sha256 does not match candidate")

    if current_context:
        context = dict(current_context)
    else:
        context = _saved_review_context(target_id, pending)
        if current_context == {}:
            context["interests"] = []

    return {
        "action": "review",
        "run_id": pending["run_id"],
        "target_id": target_id,
        "revision": pending["revision"],
        **context,
        "diff": pending["diff"],
        "diff_truncated": pending["diff_truncated"],
        **({"link_review": pending["link_review"]} if "link_review" in pending else {}),
    }

def _pending_review_handle(
    state: Path,
    target_id: str,
    *,
    current_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    pending = _read_pending(state, target_id)
    if current_context:
        context = dict(current_context)
    else:
        context = _saved_review_context(target_id, pending)
    return _compact_review({
        "action": "review",
        "run_id": pending["run_id"],
        "target_id": target_id,
        "revision": pending["revision"],
        **context,
        "diff_truncated": pending["diff_truncated"],
    })


def _prepare_pending_for_read(state: Path, target_id: str) -> None:
    """Recover interrupted writes while preserving committed revision ambiguity."""
    target_id = _validate_target_id(target_id)
    record = _read_recovery_record(state, target_id)
    commit = _read_commit_record(state, target_id)
    if commit is not None and record is not None and record.get("kind") == "replace":
        if not _replacement_matches_commit(state, target_id, commit):
            raise WorkspaceError("pending committed replacement is incomplete")
        return
    _recover_pending(state, target_id)


def pending_reviews(
    workspace: str | Path,
    *,
    targets: str | Path | None = None,
    target_id: str | None = None,
) -> dict[str, object]:
    """Return resumable pending reviews without refetching monitored targets."""
    root = _workspace(workspace)
    return _collect_pending_reviews(
        root, _current_review_contexts(targets), target_id=target_id
    )


def _collect_pending_reviews(
    root: Path,
    current_contexts: Mapping[str, Mapping[str, object]] | None,
    *,
    target_id: str | None = None,
) -> dict[str, object]:
    """Collect reviews against one validated configuration or saved recovery context."""
    state = _state_dir(root)
    target_ids = _pending_target_ids(state)
    blocked: dict[str, str] = {}
    for pending_target_id in target_ids:
        try:
            _prepare_pending_for_read(state, pending_target_id)
        except WorkspaceError as exc:
            record = _read_recovery_record(state, pending_target_id)
            if record is None or record["kind"] != "archive":
                raise
            if pending_target_id == target_id:
                raise
            blocked[pending_target_id] = str(exc)
    target_ids = [item for item in _pending_target_ids(state) if item not in blocked]
    if target_id is not None:
        target_id = _validate_target_id(target_id)
        if target_id not in target_ids:
            raise WorkspaceError("no valid pending decision exists for target")
        return {
            "reviews": [
                _pending_review(
                    state,
                    target_id,
                    current_context=(
                        None
                        if current_contexts is None
                        else current_contexts.get(target_id, {})
                    ),
                )
            ]
        }

    result: dict[str, object] = {
        "reviews": [
            _pending_review_handle(
                state,
                current,
                current_context=(
                    None
                    if current_contexts is None
                    else current_contexts.get(current, {})
                ),
            )
            for current in target_ids
        ]
    }
    if blocked:
        result["blocked"] = [
            {"error": error, "target_id": blocked_id}
            for blocked_id, error in sorted(blocked.items())
        ]
    return result


def discard_pending(workspace: str | Path, target_id: str) -> dict[str, object]:
    """Discard one pending review so a conflicted target can be checked again."""
    root = _workspace(workspace)
    state = _state_dir(root)
    intent = _read_recovery_record(state, target_id)
    if intent is not None and intent["kind"] == "archive":
        try:
            _complete_archive(root, state, intent)
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"evidence archival transaction is in progress and blocked: {exc}"
            ) from exc
        raise WorkspaceError(
            "evidence archival transaction was completed during recovery; "
            "nothing remains to discard"
        )
    _prepare_pending_for_read(state, target_id)
    pending = _read_pending(state, target_id)
    run_id = str(pending["run_id"])
    _recover_pending(state, target_id, revision=str(pending["revision"]))
    _discard_pending(state, target_id)
    return {
        "action": "discarded",
        "run_id": run_id,
        "target_id": target_id,
    }


def _require(condition: object, message: str) -> None:
    if not condition:
        raise WorkspaceError(message)


def _json_text(value: object) -> str:
    with _integer_text_limit():
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ingestion_id(target_id: str, revision: str) -> str:
    """Identify one accepted revision transaction, not URL or content."""
    canonical = json.dumps(
        ["wsum-ingestion", 1, target_id, revision], separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_limited(path: Path, limit: int, description: str) -> bytes:
    info = _optional_lstat(path, description)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise WorkspaceError(f"{description} is missing or invalid")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"cannot read {description}") from exc
    _require(len(data) <= limit, f"{description} is missing or invalid")
    return data


def _json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        with _integer_text_limit():
            value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"{description} is invalid") from exc
    _require(isinstance(value, dict), f"{description} is invalid")
    return cast("dict[str, Any]", value)


def _hash_file(path: Path, limit: int, description: str) -> tuple[int, str]:
    info = _optional_lstat(path, description)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise WorkspaceError(f"{description} is missing or invalid")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise WorkspaceError(f"cannot read {description}") from exc
    _require(size <= limit, f"{description} is missing or invalid")
    return size, digest.hexdigest()


def _archive_links_text(link_review: object) -> str:
    if link_review is None:
        body: dict[str, object] = {
            "available": False,
            "documents": [],
            "incomplete": False,
            "note": "No linked-document review was retained for this revision.",
            "omitted": 0,
            "schema": _LINKS_SCHEMA,
        }
    else:
        review = cast("dict[str, Any]", link_review)
        body = {
            "available": True,
            "documents": [
                {"entry_id": f"link-{index}", **cast("dict[str, Any]", document)}
                for index, document in enumerate(review["documents"], start=1)
            ],
            "incomplete": review["incomplete"],
            "omitted": review["omitted"],
            "schema": _LINKS_SCHEMA,
        }
    return _json_text(body)


def _archive_payloads(
    review: Mapping[str, object], candidate: bytes
) -> dict[str, bytes]:
    return {
        "parent.txt": candidate,
        "diff.txt": str(review["diff"]).encode("utf-8"),
        "links.json": _archive_links_text(review.get("link_review")).encode("utf-8"),
    }


def _archive_metadata(
    *,
    ingestion_id: str,
    review: Mapping[str, object],
    interests: object,
    payloads: Mapping[str, bytes],
    pending: Mapping[str, object],
    archived_at: str,
    report_sha256: str,
) -> str:
    links = json.loads(payloads["links.json"])
    return _json_text({
        "archived_at": archived_at,
        "candidate_sha256": pending["candidate_sha256"],
        "decision": {"material": True, "report_sha256": report_sha256},
        "diff_truncated": review["diff_truncated"],
        "diff_truncated_note": (
            "Retained flag; also true when linked evidence was incomplete."
        ),
        "expected_sha256": pending["expected_sha256"],
        "ingestion_id": ingestion_id,
        "interests": interests,
        "payloads": {
            name: {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                **_payload_flags(name, links),
            }
            for name, data in payloads.items()
        },
        "provenance": {
            "archived_at_meaning": "time this bundle was prepared, not source time",
            "source_fetched_at": None,
            "source_published_at": None,
            "unavailable": ["source_fetched_at", "source_published_at"],
        },
        "revision": review["revision"],
        "run_id": review["run_id"],
        "schema": _EVIDENCE_SCHEMA,
        "target_id": review["target_id"],
        "url": review["url"],
    })


def _payload_flags(name: str, links: Mapping[str, object]) -> dict[str, object]:
    if name == "parent.txt":
        return {"complete_normalized_candidate": True}
    if name == "links.json":
        return {
            "available": links["available"],
            "incomplete": links["incomplete"],
            "omitted": links["omitted"],
        }
    return {}


def _validate_archive_metadata(
    metadata: Mapping[str, Any], ingestion_id: str
) -> dict[str, dict[str, Any]]:
    payloads = metadata.get("payloads")
    valid = (
        metadata.get("schema") == _EVIDENCE_SCHEMA
        and metadata.get("ingestion_id") == ingestion_id
        and isinstance(metadata.get("target_id"), str)
        and isinstance(metadata.get("revision"), str)
        and isinstance(payloads, dict)
        and set(cast("dict[str, Any]", payloads)) == set(_ARCHIVE_PAYLOAD_LIMITS)
    )
    _require(valid, "evidence metadata is invalid")
    _require(
        _ingestion_id(str(metadata["target_id"]), str(metadata["revision"]))
        == ingestion_id,
        "evidence metadata is invalid",
    )
    entries = cast("dict[str, object]", payloads)
    for name, raw in entries.items():
        entry = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
        _require(
            type(entry.get("bytes")) is int
            and 0 <= entry["bytes"] <= _ARCHIVE_PAYLOAD_LIMITS[name]
            and isinstance(entry.get("sha256"), str)
            and _SHA256_RE.fullmatch(entry["sha256"]) is not None,
            "evidence metadata is invalid",
        )
    return cast("dict[str, dict[str, Any]]", entries)


def _validate_archive_intent(record: dict[str, object]) -> dict[str, object]:
    report = record.get("report")
    metadata_text = record.get("metadata")
    diff = record.get("diff")
    valid = (
        frozenset(record) == _INTENT_FIELDS
        and isinstance(diff, str)
        and isinstance(report, str)
        and 0 < len(report.encode("utf-8")) <= _MAX_ARCHIVE_REPORT_BYTES
        and isinstance(metadata_text, str)
        and len(metadata_text.encode("utf-8")) <= _MAX_ARCHIVE_METADATA_BYTES
        and isinstance(record.get("archived_at"), str)
        and isinstance(record.get("revision"), str)
        and _REVISION_RE.fullmatch(cast("str", record["revision"])) is not None
        and isinstance(record.get("run_id"), str)
        and _RUN_ID_RE.fullmatch(cast("str", record["run_id"])) is not None
    )
    _require(valid, "pending recovery record is invalid")
    _require(
        record["report_sha256"] == _text_sha256(cast("str", report))
        and record["metadata_sha256"] == _text_sha256(cast("str", metadata_text))
        and record["ingestion_id"]
        == _ingestion_id(str(record["target_id"]), str(record["revision"])),
        "pending recovery record is invalid",
    )
    _validate_sha256(record["candidate_sha256"], "candidate_sha256")
    _validate_sha256(record["expected_sha256"], "expected_sha256", allow_none=True)
    metadata = _json_object(cast("str", metadata_text).encode("utf-8"), "metadata")
    frozen = _validate_archive_metadata(metadata, cast("str", record["ingestion_id"]))
    diff_bytes = cast("str", diff).encode("utf-8")
    _require(
        frozen["diff.txt"]["bytes"] == len(diff_bytes)
        and frozen["diff.txt"]["sha256"] == hashlib.sha256(diff_bytes).hexdigest(),
        "pending recovery record is invalid",
    )
    return record


def _evidence_directory(root: Path, *, create: bool) -> Path | None:
    path = _internal_dir(root) / _EVIDENCE_DIR
    if create:
        return _ensure_directory(path, "evidence directory", sync_parent=True)
    info = _optional_lstat(path, "evidence directory")
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("evidence directory must be a non-symlink directory")
    return path


def _read_receipt(
    root: Path, ingestion_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return a digest-verified committed bundle, or None when not committed."""
    evidence = _evidence_directory(root, create=False)
    if evidence is None:
        return None
    bundle = evidence / ingestion_id
    info = _optional_lstat(bundle, "evidence bundle")
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("evidence bundle must be a non-symlink directory")
    if _optional_lstat(bundle / "committed.json", "evidence receipt") is None:
        return None  # staged evidence without a receipt is never an accepted update
    receipt = _json_object(
        _read_limited(
            bundle / "committed.json", _MAX_ARCHIVE_RECEIPT_BYTES, "evidence receipt"
        ),
        "evidence receipt",
    )
    metadata_bytes = _read_limited(
        bundle / "metadata.json", _MAX_ARCHIVE_METADATA_BYTES, "evidence metadata"
    )
    metadata = _json_object(metadata_bytes, "evidence metadata")
    payloads = _validate_archive_metadata(metadata, ingestion_id)
    decision = metadata.get("decision")
    _require(
        frozenset(receipt) == _RECEIPT_FIELDS
        and receipt["schema"] == _EVIDENCE_SCHEMA
        and receipt["ingestion_id"] == ingestion_id
        and receipt["material"] is True
        and receipt["manifest_sha256"] == hashlib.sha256(metadata_bytes).hexdigest()
        and isinstance(decision, dict)
        and receipt["report_section_sha256"]
        == cast("dict[str, Any]", decision).get("report_sha256")
        and receipt["target_id"] == metadata["target_id"]
        and receipt["revision"] == metadata["revision"]
        and receipt["run_id"] == metadata.get("run_id")
        and receipt["archived_at"] == metadata.get("archived_at"),
        "evidence receipt is invalid",
    )
    for name, entry in payloads.items():
        size, digest = _hash_file(
            bundle / name, _ARCHIVE_PAYLOAD_LIMITS[name], f"evidence {name}"
        )
        _require(
            size == entry["bytes"] and digest == entry["sha256"],
            f"evidence {name} does not match its digest",
        )
    return metadata, receipt


def _install_evidence_file(path: Path, data: bytes, description: str) -> None:
    """Install one bundle file atomically, reusing identical bytes."""
    info = _optional_lstat(path, description)
    if info is not None:
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_size == len(data)
            and path.read_bytes() == data,
            f"{description} conflicts with existing evidence",
        )
        return
    temporary = _write_temporary_file(path, data, description)
    try:
        temporary.replace(path)
    except OSError as exc:
        raise WorkspaceError(f"cannot install {description}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        raise WorkspaceError(f"cannot fsync {description} directory") from exc
    _require(path.read_bytes() == data, f"{description} read-back mismatch")


def _stage_evidence(
    root: Path, intent: Mapping[str, object], payloads: Mapping[str, bytes]
) -> Path:
    evidence = cast("Path", _evidence_directory(root, create=True))
    bundle = _ensure_directory(
        evidence / str(intent["ingestion_id"]),
        "evidence bundle directory",
        sync_parent=True,
    )
    files = {**payloads, "metadata.json": str(intent["metadata"]).encode("utf-8")}
    for name, data in files.items():
        _install_evidence_file(bundle / name, data, f"evidence {name}")
    return bundle


def _archive_receipt_text(intent: Mapping[str, object]) -> str:
    return _json_text({
        "archived_at": intent["archived_at"],
        "ingestion_id": intent["ingestion_id"],
        "manifest_sha256": intent["metadata_sha256"],
        "material": True,
        "report_section_sha256": intent["report_sha256"],
        "revision": intent["revision"],
        "run_id": intent["run_id"],
        "schema": _EVIDENCE_SCHEMA,
        "target_id": intent["target_id"],
    })


def _archive_result(root: Path, source: Mapping[str, object]) -> dict[str, object]:
    ingestion_id = str(source["ingestion_id"])
    return {
        "action": "finalized",
        "evidence_path": str(root / _INTERNAL_DIR / _EVIDENCE_DIR / ingestion_id),
        "ingestion_id": ingestion_id,
        "material": True,
        "report_path": str(root / _OUTPUT_DIR / _REPORT_DIR / f"{source['run_id']}.md"),
        "target_id": source["target_id"],
    }


def _complete_archive(
    root: Path, state: Path, intent: Mapping[str, object]
) -> dict[str, object]:
    """Resume or finish a prepared archive transaction from its frozen intent."""
    target_id = str(intent["target_id"])
    existing = _read_receipt(root, str(intent["ingestion_id"]))
    if existing is None:
        try:
            pending = _read_pending(state, target_id)
            candidate = _read_text_bytes(_candidate_path(state, target_id), "candidate")
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"archive transaction cannot resume without pending evidence: {exc}"
            ) from exc
        # The diff is frozen in the intent so archive recovery is independent of
        # any later baseline promotion.
        review = {**pending, "diff": intent["diff"]}
        payloads = _archive_payloads(review, candidate)
        frozen = _validate_archive_metadata(
            _json_object(str(intent["metadata"]).encode("utf-8"), "metadata"),
            str(intent["ingestion_id"]),
        )
        _require(
            pending["revision"] == intent["revision"]
            and pending["run_id"] == intent["run_id"]
            and hashlib.sha256(candidate).hexdigest() == intent["candidate_sha256"]
            and pending["candidate_sha256"] == intent["candidate_sha256"]
            and all(
                len(data) == frozen[name]["bytes"]
                and hashlib.sha256(data).hexdigest() == frozen[name]["sha256"]
                for name, data in payloads.items()
            ),
            "pending review does not match the archive transaction",
        )
        bundle = _stage_evidence(root, intent, payloads)
        promoted = _promote_snapshot(
            state,
            target_id=target_id,
            expected_sha256=intent["expected_sha256"],
            candidate_sha256=intent["candidate_sha256"],
            candidate_data=candidate,
        )
        _require(
            promoted.get("action") == "snapshot_promoted",
            "snapshot conflict; archive transaction preserved",
        )
        _write_report(root, str(intent["run_id"]), target_id, str(intent["report"]))
        _install_evidence_file(
            bundle / "committed.json",
            _archive_receipt_text(intent).encode("utf-8"),
            "evidence receipt",
        )
    else:
        _, receipt = existing
        _require(
            receipt["manifest_sha256"] == intent["metadata_sha256"]
            and receipt["report_section_sha256"] == intent["report_sha256"],
            "evidence receipt does not match the archive transaction",
        )
    cleanup = _finalize_cleanup_record(
        target_id,
        str(intent["revision"]),
        True,  # ruff: ignore[boolean-positional-value-in-call]
        str(intent["report"]),
        str(intent["run_id"]),
    )
    # Replacing the intent retires the archive obligation only after the receipt
    # is durable; the remaining cleanup is the ordinary finalize cleanup.
    _write_recovery_record(state, cleanup)
    _complete_cleanup_record(state, cleanup)
    return _archive_result(root, intent)


def _prepare_archive(
    root: Path,
    state: Path,
    target_id: str,
    report: str,
    targets: str | Path | None,
) -> dict[str, object]:
    """Validate and freeze one material decision, then complete its archive."""
    if targets is None:
        raise WorkspaceError("--targets is required to prepare an evidence archive")
    target = next(
        (item for item in load_targets(targets) if item["target_id"] == target_id),
        None,
    )
    if target is None or target["action"] == "skip_disabled":
        raise WorkspaceError(
            "target is not active in the current configuration; evidence not archived"
        )
    pending = _read_pending(state, target_id)
    snapshot = _read_snapshot(state / "snapshots" / f"{target_id}.txt")
    snapshot_sha256 = None if snapshot is None else hashlib.sha256(snapshot).hexdigest()
    if snapshot_sha256 not in {pending["expected_sha256"], pending["candidate_sha256"]}:
        return {"action": "snapshot_conflict", "target_id": target_id}
    context = _target_review_contexts([target])[target_id]
    review = _pending_review(state, target_id, current_context=context)
    candidate = _read_text_bytes(_candidate_path(state, target_id), "candidate")
    payloads = _archive_payloads(review, candidate)
    for name, data in payloads.items():
        _require(
            len(data) <= _ARCHIVE_PAYLOAD_LIMITS[name],
            f"{name} exceeds the evidence archive size limit",
        )
    _require(
        len(report.encode("utf-8")) <= _MAX_ARCHIVE_REPORT_BYTES,
        "report exceeds the evidence archive size limit",
    )
    ingestion_id = _ingestion_id(target_id, str(review["revision"]))
    metadata = _archive_metadata(
        ingestion_id=ingestion_id,
        review=review,
        interests=context["interests"],
        payloads=payloads,
        pending=pending,
        archived_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        report_sha256=_text_sha256(report),
    )
    _require(
        len(metadata.encode("utf-8")) <= _MAX_ARCHIVE_METADATA_BYTES,
        "evidence metadata exceeds the evidence archive size limit",
    )
    intent: dict[str, object] = {
        "archived_at": json.loads(metadata)["archived_at"],
        "candidate_sha256": pending["candidate_sha256"],
        "diff": review["diff"],
        "expected_sha256": pending["expected_sha256"],
        "ingestion_id": ingestion_id,
        "kind": "archive",
        "metadata": metadata,
        "metadata_sha256": _text_sha256(metadata),
        "report": report,
        "report_sha256": _text_sha256(report),
        "revision": review["revision"],
        "run_id": review["run_id"],
        "target_id": target_id,
        "version": _ARCHIVE_INTENT_VERSION,
    }
    _require(
        len(_json_text(intent).encode("utf-8")) <= _MAX_ARCHIVE_INTENT_BYTES,
        "archive intent exceeds the evidence archive size limit",
    )
    _write_recovery_record(state, intent)
    return _complete_archive(root, state, intent)


def _finalize_archive_intent(
    root: Path,
    state: Path,
    intent: Mapping[str, object],
    revision: str,
    report: str | None,
) -> dict[str, object]:
    """Finish an outstanding archive obligation whatever options the caller gave."""
    _require(
        report is not None
        and intent["revision"] == revision
        and intent["report_sha256"] == _text_sha256(report),
        "decision does not match the prepared archive transaction",
    )
    return _complete_archive(root, state, intent)


def _finalize_from_receipt(
    root: Path,
    state: Path,
    recovery: Mapping[str, object] | None,
    ingestion_id: str,
    revision: str,
    report: str | None,
) -> dict[str, object] | None:
    """Answer a retry from a durable receipt before any pending state is needed."""
    existing = _read_receipt(root, ingestion_id)
    if existing is None:
        return None
    _, receipt = existing
    _require(
        report is not None and receipt["report_section_sha256"] == _text_sha256(report),
        "decision does not match the archived evidence receipt",
    )
    if (
        recovery is not None
        and recovery.get("kind") == "cleanup"
        and recovery.get("revision") == revision
    ):
        _complete_cleanup_record(state, recovery)
    return _archive_result(root, receipt)


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
        result["report_path"] = str(root / _OUTPUT_DIR / _REPORT_DIR / f"{run_id}.md")
    return result


def finalize(
    workspace: str | Path,
    payload: Mapping[str, object],
    *,
    targets: str | Path | None = None,
    archive_evidence: bool = False,
) -> dict[str, object]:
    """Apply one semantic decision and safely advance its baseline.

    With ``archive_evidence``, a material decision also commits a durable,
    digest-verified evidence bundle under ``internal/evidence/<ingestion-id>/``.
    """
    target_id, revision, material, report = _validate_decision(payload)

    root = _workspace(workspace)
    state = _state_dir(root)
    commit = _read_commit_record(state, target_id)
    recovery = _read_recovery_record(state, target_id)
    if recovery is not None and recovery["kind"] == "archive":
        return _finalize_archive_intent(root, state, recovery, revision, report)
    archived = _finalize_from_receipt(
        root, state, recovery, _ingestion_id(target_id, revision), revision, report
    )
    if archived is not None:
        return archived
    if commit is not None:
        _recover_pending(state, target_id, revision=revision)
        recovery = _read_recovery_record(state, target_id)
    if recovery is not None and recovery.get("kind") == "cleanup":
        if recovery.get("purpose") == "finalize":
            if not _finalize_record_matches(recovery, revision, material, report):
                raise WorkspaceError("decision does not match pending cleanup")
            if archive_evidence:
                raise WorkspaceError(
                    "evidence archival cannot be adopted after finalization "
                    "completed without it"
                )
            _complete_cleanup_record(state, recovery)
            return _finalized_result(root, recovery)
        _recover_pending(state, target_id)
    elif recovery is not None:
        _recover_pending(state, target_id)

    if archive_evidence and _existing_pending_paths(state, target_id) is None:
        raise WorkspaceError(
            "no pending review or evidence receipt exists for this revision; "
            "archival cannot be adopted after a completed finalization"
        )
    pending = _read_pending(state, target_id)
    if pending["target_id"] != target_id:
        raise WorkspaceError("pending decision target does not match")
    if pending["revision"] != revision:
        raise WorkspaceError("decision revision does not match pending review")
    if pending["diff_truncated"] is True and not material:
        return {"action": "manual_review_required", "target_id": target_id}

    if archive_evidence and report is not None:
        return _prepare_archive(root, state, target_id, report, targets)

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
    parser.add_argument(
        "--targets",
        help="path to the target CSV, relative to the current directory or absolute",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--compact", action="store_true")
    check_parser.add_argument("--link-depth", type=int, default=_DEFAULT_LINK_DEPTH)
    check_parser.add_argument("--max-links", type=int, default=_MAX_LINKS)

    pending_parser = subparsers.add_parser("pending")
    pending_parser.add_argument("--target-id")

    discard_parser = subparsers.add_parser("discard")
    discard_parser.add_argument("--target-id", required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--archive-evidence", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the workspace-facing orchestration command."""
    args = _parser().parse_args(argv)
    if args.command == "check" and args.targets is None:
        print(
            json.dumps({"error": "--targets is required for check"}),
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "check":
            result = check(
                args.workspace,
                args.targets,
                compact=args.compact,
                link_depth=args.link_depth,
                max_links=args.max_links,
            )
        elif args.command == "pending":
            result = pending_reviews(
                args.workspace,
                targets=args.targets,
                target_id=args.target_id,
            )
        elif args.command == "discard":
            result = discard_pending(args.workspace, args.target_id)
        else:
            result = finalize(
                args.workspace,
                _read_decision(),
                targets=args.targets,
                archive_evidence=args.archive_evidence,
            )
    except (WorkspaceError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    with _integer_text_limit():
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
