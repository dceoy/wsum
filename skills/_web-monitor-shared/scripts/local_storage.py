"""Local filesystem persistence adapters for the web monitor."""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from drive import snapshot_paths
from errors import MonitorError
from models import (
    HASH_RE,
    Attempt,
    NotificationRecord,
    RunRecord,
    State,
    Target,
    validate_target_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from diff import DiffResult
    from normalize import NormalizedContent

_MAX_METADATA_BYTES = 10_000_000
_MIN_MAX_SNAPSHOT_BYTES = 1_024
_MAX_MAX_SNAPSHOT_BYTES = 50_000_000
_MAX_SNAPSHOT_REF_LENGTH = 1_000
_SNAPSHOT_REF_PART_COUNT = 4


def _read_bytes(
    path: Path,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> bytes:
    """Read a local file while translating filesystem failures.

    Returns:
        The file bytes.

    Raises:
        MonitorError: If the file cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MonitorError(code, message, retryable=retryable) from exc


def _load_json(path: Path, default: object) -> object:
    """Load one bounded JSON document, returning ``default`` when absent.

    Returns:
        The decoded JSON value or ``default`` if ``path`` does not exist.

    Raises:
        MonitorError: If the path is not a regular file, cannot be read, is
            oversized, or contains invalid JSON.
    """
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file():
        msg = "local_storage_invalid"
        raise MonitorError(msg, "local storage path must be a regular file")
    payload = _read_bytes(
        path,
        "local_storage_io",
        "local storage file could not be read",
        retryable=True,
    )
    if len(payload) > _MAX_METADATA_BYTES:
        msg = "local_storage_invalid"
        raise MonitorError(msg, "local storage metadata exceeds the size limit")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "local_storage_invalid"
        raise MonitorError(msg, "local storage file contains invalid JSON") from exc


def _object_map(value: object, label: str) -> dict[str, object]:
    """Validate a JSON object with string keys.

    Returns:
        A typed copy of the JSON object.

    Raises:
        MonitorError: If ``value`` is not an object with string keys.
    """
    if not isinstance(value, dict):
        msg = "local_storage_invalid"
        raise MonitorError(msg, f"{label} must be a JSON object")
    raw = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in raw):
        msg = "local_storage_invalid"
        raise MonitorError(msg, f"{label} must be a JSON object")
    return cast("dict[str, object]", raw)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """Validate one JSON record mapping.

    Returns:
        ``value`` narrowed to a mapping accepted by the record validators.

    Raises:
        MonitorError: If ``value`` is not a JSON object.
    """
    if not isinstance(value, dict):
        msg = "local_storage_invalid"
        raise MonitorError(msg, f"{label} must be a JSON object")
    return cast("Mapping[str, object]", value)


def _write_json(path: Path, value: object) -> None:
    """Atomically replace one local JSON document.

    Raises:
        MonitorError: If the parent directory or file cannot be written.
    """
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(payload) > _MAX_METADATA_BYTES:
        msg = "local_storage_invalid"
        raise MonitorError(msg, "local storage metadata exceeds the size limit")
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        temp_path.write_bytes(payload)
        temp_path.chmod(0o600)
        temp_path.replace(path)
    except OSError as exc:
        msg = "local_storage_io"
        raise MonitorError(
            msg, "local storage file could not be written", retryable=True
        ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _integer(value: object, label: str) -> int:
    """Parse one integer field from local JSON.

    Returns:
        The parsed integer.

    Raises:
        MonitorError: If ``value`` is not integer-like.
    """
    try:
        return int(str(value))
    except ValueError as exc:
        msg = "local_storage_invalid"
        raise MonitorError(msg, f"{label} must be an integer") from exc


def _run_from_mapping(value: Mapping[str, object]) -> RunRecord:
    """Restore a :class:`RunRecord` from local JSON.

    Returns:
        The validated run record.

    Raises:
        MonitorError: If the attempts list is malformed.
    """
    raw_attempts = value.get("attempts", [])
    if not isinstance(raw_attempts, list):
        msg = "local_storage_invalid"
        raise MonitorError(msg, "run attempts must be a JSON array")
    attempts = tuple(
        Attempt(
            number=_integer(item_map.get("number", 0), "run attempt number"),
            result=str(item_map.get("result", "")),
            error_code=str(item_map.get("error_code", "")),
        )
        for item in cast("list[object]", raw_attempts)
        for item_map in (_mapping(item, "run attempt"),)
    )
    return RunRecord(
        run_id=str(value.get("run_id", "")),
        target_id=str(value.get("target_id", "")),
        result=str(value.get("result", "")),
        change_score=_integer(value.get("change_score", 0), "run change_score"),
        summary=str(value.get("summary", "")),
        error_code=str(value.get("error_code", "")),
        started_at=str(value.get("started_at", "")),
        finished_at=str(value.get("finished_at", "")),
        attempts=attempts,
    )


def _notification_from_mapping(value: Mapping[str, object]) -> NotificationRecord:
    """Restore a :class:`NotificationRecord` from local JSON.

    Returns:
        The validated notification record.
    """
    return NotificationRecord(
        event_id=str(value.get("event_id", "")),
        target_id=str(value.get("target_id", "")),
        status=str(value.get("status", "")),
        notified_at=str(value.get("notified_at", "")),
        kind=str(value.get("kind", "change")),
        last_error=str(value.get("last_error", "")),
    )


class LocalOperationalStore:
    """Thread-safe JSON-backed operational and notification store."""

    def __init__(self, root: str | Path) -> None:
        """Bind the store to a caller-selected local runtime directory.

        Raises:
            MonitorError: If the runtime directory cannot be created.
        """
        try:
            self._root = Path(root).expanduser().resolve()
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = "local_storage_io"
            raise MonitorError(
                msg, "local runtime directory could not be created"
            ) from exc
        self._lock = threading.RLock()

    def load_enabled_targets(self) -> list[Target]:
        """Load and validate enabled targets from ``targets.json``.

        Returns:
            The enabled targets in file order.

        Raises:
            MonitorError: If the configuration is missing, malformed, or
                contains duplicate target IDs.
        """
        with self._lock:
            raw = _load_json(self._root / "targets.json", None)
        if raw is None:
            msg = "local_configuration_missing"
            raise MonitorError(
                msg, "targets.json is required in the local runtime directory"
            )
        if not isinstance(raw, list):
            msg = "local_storage_invalid"
            raise MonitorError(msg, "targets.json must contain a JSON array")
        targets = [
            Target.from_mapping(_mapping(item, "target"))
            for item in cast("list[object]", raw)
        ]
        target_ids = [target.target_id for target in targets]
        if len(target_ids) != len(set(target_ids)):
            msg = "local_storage_invalid"
            raise MonitorError(msg, "targets.json contains duplicate target IDs")
        return [target for target in targets if target.enabled]

    def get_state(self, target_id: str) -> State | None:
        """Return the stored state for ``target_id``, if any."""
        validate_target_id(target_id)
        with self._lock:
            records = _object_map(
                _load_json(self._root / "state.json", {}), "state.json"
            )
            raw = records.get(target_id)
        return State.from_mapping(_mapping(raw, "state")) if raw is not None else None

    def replace_state(self, state: State) -> None:
        """Insert or replace one target state atomically."""
        with self._lock:
            path = self._root / "state.json"
            records = _object_map(_load_json(path, {}), "state.json")
            records[state.target_id] = state.as_dict()
            _write_json(path, records)

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the stored run record for ``run_id``, if any."""
        with self._lock:
            records = _object_map(_load_json(self._root / "runs.json", {}), "runs.json")
            raw = records.get(run_id)
        return _run_from_mapping(_mapping(raw, "run")) if raw is not None else None

    def append_run(self, run: RunRecord) -> None:
        """Idempotently persist one run, keyed by ``run_id``."""
        with self._lock:
            path = self._root / "runs.json"
            records = _object_map(_load_json(path, {}), "runs.json")
            if run.run_id in records:
                return
            records[run.run_id] = run.as_dict()
            _write_json(path, records)

    def get_notification(self, event_id: str) -> NotificationRecord | None:
        """Return the stored notification record for ``event_id``, if any."""
        with self._lock:
            records = _object_map(
                _load_json(self._root / "notifications.json", {}),
                "notifications.json",
            )
            raw = records.get(event_id)
        return (
            _notification_from_mapping(_mapping(raw, "notification"))
            if raw is not None
            else None
        )

    def upsert_notification(self, notification: NotificationRecord) -> None:
        """Insert or replace one notification record atomically."""
        self.upsert_notifications_atomically((notification,))

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None:
        """Insert or replace all notification records with one atomic file write.

        Raises:
            MonitorError: If ``notifications`` contains duplicate event IDs.
        """
        event_ids = [item.event_id for item in notifications]
        if len(event_ids) != len(set(event_ids)):
            msg = "notification_invalid"
            raise MonitorError(msg, "notification batch contains duplicate IDs")
        if not notifications:
            return
        with self._lock:
            path = self._root / "notifications.json"
            records = _object_map(_load_json(path, {}), "notifications.json")
            records.update({item.event_id: item.as_dict() for item in notifications})
            _write_json(path, records)


class LocalSnapshotStore:
    """Content-addressed snapshot storage rooted in the local filesystem."""

    def __init__(
        self, root: str | Path, *, max_snapshot_bytes: int = 10_000_000
    ) -> None:
        """Bind snapshot storage to ``root`` with a bounded content size.

        Raises:
            MonitorError: If the size limit is invalid or ``root`` cannot be
                created.
        """
        if not (
            _MIN_MAX_SNAPSHOT_BYTES <= max_snapshot_bytes <= _MAX_MAX_SNAPSHOT_BYTES
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "snapshot size limit is invalid")
        try:
            self._root = Path(root).expanduser().resolve()
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = "local_storage_io"
            raise MonitorError(
                msg, "local runtime directory could not be created"
            ) from exc
        self._max_snapshot_bytes = max_snapshot_bytes
        self._lock = threading.RLock()

    def _resolve_relative(self, relative: str) -> Path:
        """Resolve a POSIX-style storage path without allowing root escape.

        Returns:
            The resolved filesystem path below the configured root.

        Raises:
            MonitorError: If ``relative`` is absolute or escapes the root.
        """
        posix_path = PurePosixPath(relative)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            msg = "snapshot_invalid"
            raise MonitorError(msg, "snapshot path is invalid")
        candidate = (self._root / Path(*posix_path.parts)).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            msg = "snapshot_invalid"
            raise MonitorError(msg, "snapshot path escapes the local runtime directory")
        return candidate

    @staticmethod
    def _existing_file_matches(path: Path, content: bytes) -> bool:
        """Validate an existing artifact and compare its bytes.

        Returns:
            ``True`` when ``path`` already stores ``content``; ``False`` when
            the path does not exist.

        Raises:
            MonitorError: If an existing path is not a regular file, cannot be
                read, or contains different bytes.
        """
        if not path.exists():
            return False
        if path.is_symlink() or not path.is_file():
            msg = "snapshot_invalid"
            raise MonitorError(msg, "snapshot artifact is not a regular file")
        existing = _read_bytes(
            path,
            "local_storage_io",
            "local snapshot could not be read",
            retryable=True,
        )
        if existing != content:
            msg = "snapshot_collision"
            raise MonitorError(msg, "snapshot path already contains different content")
        return True

    @staticmethod
    def _write_new_file(path: Path, content: bytes) -> None:
        """Write a new snapshot artifact by atomic rename.

        Raises:
            MonitorError: If the filesystem write fails.
        """
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_bytes(content)
            temp.chmod(0o600)
            temp.replace(path)
        except OSError as exc:
            msg = "local_storage_io"
            raise MonitorError(
                msg, "local snapshot could not be written", retryable=True
            ) from exc
        finally:
            temp.unlink(missing_ok=True)

    def _ensure_file(self, relative: str, content: bytes) -> None:
        """Idempotently persist one content-addressed snapshot artifact."""
        path = self._resolve_relative(relative)
        if self._existing_file_matches(path, content):
            return
        self._write_new_file(path, content)

    def save(
        self,
        target_id: str,
        content: NormalizedContent,
        diff: DiffResult | None = None,
        previous_hash: str = "",
    ) -> str:
        """Persist normalized content, metadata, and an optional bounded diff.

        Returns:
            A relative snapshot reference suitable for :class:`State`.

        Raises:
            MonitorError: If the normalized content exceeds the configured limit.
        """
        normalized_bytes = content.text.encode()
        if len(normalized_bytes) > self._max_snapshot_bytes:
            msg = "snapshot_too_large"
            raise MonitorError(msg, "normalized snapshot exceeds the size limit")
        paths = snapshot_paths(target_id, content.normalized_hash, previous_hash)
        metadata = json.dumps(
            content.as_dict(include_text=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with self._lock:
            self._ensure_file(paths.normalized, normalized_bytes)
            self._ensure_file(paths.metadata, metadata)
            if diff is not None:
                diff_bytes = json.dumps(
                    diff.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                self._ensure_file(paths.diff, diff_bytes)
        return paths.normalized

    def load_normalized(self, snapshot_ref: str) -> str:
        """Load and decode one normalized snapshot by relative reference.

        Returns:
            The normalized UTF-8 text.

        Raises:
            MonitorError: If the reference shape, stored file, size, or encoding
                is invalid.
        """
        if not snapshot_ref or len(snapshot_ref) > _MAX_SNAPSHOT_REF_LENGTH:
            msg = "snapshot_missing"
            raise MonitorError(msg, "snapshot reference is missing")
        parts = PurePosixPath(snapshot_ref).parts
        if (
            len(parts) != _SNAPSHOT_REF_PART_COUNT
            or parts[0] != "snapshots"
            or parts[3] != "normalized.txt"
            or not HASH_RE.fullmatch(parts[2])
        ):
            msg = "snapshot_invalid"
            raise MonitorError(msg, "snapshot reference is invalid")
        validate_target_id(parts[1])
        path = self._resolve_relative(snapshot_ref)
        if path.is_symlink() or not path.is_file():
            msg = "snapshot_missing"
            raise MonitorError(msg, "stored normalized snapshot is missing")
        content = _read_bytes(
            path,
            "snapshot_missing",
            "stored normalized snapshot could not be loaded",
        )
        if len(content) > self._max_snapshot_bytes:
            msg = "snapshot_too_large"
            raise MonitorError(msg, "stored snapshot exceeds the size limit")
        try:
            return content.decode()
        except UnicodeDecodeError as exc:
            msg = "snapshot_invalid"
            raise MonitorError(msg, "stored normalized snapshot is not UTF-8") from exc
