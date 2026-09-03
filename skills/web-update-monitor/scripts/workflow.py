"""Deterministic workflow decisions for the web update monitor skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific
    fcntl = None  # type: ignore[assignment]

import monitor

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERSISTENCE_MODES = {"local", "google-drive"}
_FETCH_MODES = {"static", "browser"}
_NOTIFICATION_STATUSES = {"pending", "sending", "delivered"}
_NOTIFICATION_PROTOCOL_VERSION = 2
_NOTIFICATION_FIELDS = frozenset({
    "version",
    "event_id",
    "target_id",
    "sha256",
    "destination",
    "message",
    "status",
    "attempt",
    "last_error",
    "updated_at",
})
_LOCAL_CAS_FIELDS = frozenset({
    "protocol_version",
    "action",
    "expected_notification",
    "notification",
    "expected_snapshot_sha256",
    "next_signal",
})
_LOCAL_CAS_LEGACY_FIELDS = _LOCAL_CAS_FIELDS - {"expected_snapshot_sha256"}
_LOCAL_CAS_SIGNALS = frozenset({
    "pending_persisted",
    "sending_claimed",
    "delivered_persisted",
    "failure_persisted",
})
_LOCAL_RELEASE_FIELDS = frozenset({
    "protocol_version",
    "action",
    "target_id",
    "event_id",
    "expected_status",
})
_LOCAL_SNAPSHOT_FIELDS = frozenset({
    "version",
    "action",
    "target_id",
    "expected_sha256",
    "candidate_sha256",
    "claim_event_id",
})
_TARGET_CLAIM_FIELDS = frozenset({
    "version",
    "target_id",
    "event_id",
    "sha256",
})
_RELEASEABLE_NOTIFICATION_STATUSES = {"pending", "delivered"}
_MAX_LOCAL_RECORD_BYTES = 1024 * 1024
_MAX_LOCAL_SNAPSHOT_BYTES = 40 * 1024 * 1024
_LOCAL_SNAPSHOT_PROTOCOL_VERSION = 1
_MIN_CANDIDATE_PATH_PARTS = 2


class WorkflowError(RuntimeError):
    """Expected workflow input or state-transition failure."""


class LocalStoreError(WorkflowError):
    """Expected local persistence failure."""


class NotificationRecord(TypedDict):
    """Validated durable notification record."""

    version: int
    event_id: str
    target_id: str
    sha256: str
    destination: str
    message: str
    status: str
    attempt: int
    last_error: str
    updated_at: str


class TargetClaim(TypedDict):
    """Validated durable claim for one target's notification window."""

    version: int
    target_id: str
    event_id: str
    sha256: str


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


def _validate_url(value: object) -> str:
    url = _require_string(value, "url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise WorkflowError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WorkflowError("url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise WorkflowError("url must not contain credentials")
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

    target_id = _require_string(target.get("target_id"), "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    name = _require_string(target.get("name"), "name")
    url = _validate_url(target.get("url"))
    enabled = target.get("enabled")
    if not isinstance(enabled, bool):
        raise WorkflowError("enabled must be a boolean")
    watch_focus = _require_string(
        target.get("watch_focus", ""), "watch_focus", allow_empty=True
    )
    notification_group = _require_string(
        target.get("notification_group", ""),
        "notification_group",
        allow_empty=True,
    )
    fetch_mode = _require_string(target.get("fetch_mode", "static"), "fetch_mode")
    if fetch_mode not in _FETCH_MODES:
        raise WorkflowError("fetch_mode must be static or browser")

    return {
        "version": 1,
        "target_id": target_id,
        "name": name,
        "url": url,
        "enabled": enabled,
        "action": "monitor" if enabled else "skip_disabled",
        "watch_focus": watch_focus,
        "notification_group": notification_group,
        "fetch_mode": fetch_mode,
    }


def validate_targets(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one run's persistence mode and target set."""
    persistence_mode = _require_string(
        payload.get("persistence_mode"), "persistence_mode"
    )
    if persistence_mode not in _PERSISTENCE_MODES:
        raise WorkflowError("persistence_mode must be local or google-drive")
    raw_targets = _require_list(payload.get("targets"), "targets")
    targets = [validate_target(target) for target in raw_targets]
    ids = [str(target["target_id"]) for target in targets]
    if len(ids) != len(set(ids)):
        raise WorkflowError("duplicate_target_id")
    return {"persistence_mode": persistence_mode, "targets": targets}


def change_action(payload: Mapping[str, object]) -> dict[str, object]:
    """Choose the next workflow action from a monitor result and LLM judgment."""
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
    if materiality:
        return {"action": "notify"}
    return {"action": "promote_snapshot"}


def notification_event_id(target_id: str, sha256: str) -> str:
    """Return the stable event ID for one target and normalized snapshot hash."""
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    if not _SHA256_RE.fullmatch(sha256):
        raise WorkflowError("sha256 must be a lowercase hexadecimal SHA-256")
    return hashlib.sha256(f"{target_id}\0{sha256}".encode()).hexdigest()


def _snapshot_hash(
    value: object, field: str, *, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    sha256 = _require_string(value, field)
    if not _SHA256_RE.fullmatch(sha256):
        raise WorkflowError(f"{field} must be a lowercase hexadecimal SHA-256")
    return sha256


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class LocalNotificationStore:
    """Persist notification records with an exact, durable local CAS."""

    def __init__(self, runtime_dir: str | Path, target_id: str) -> None:
        """Initialize a store rooted at an existing runtime directory."""
        if not _TARGET_ID_RE.fullmatch(target_id):
            raise LocalStoreError("invalid_target_id")
        self._target_id = target_id
        self._runtime_dir = self._require_directory(Path(runtime_dir), "runtime_dir")
        self._notifications_dir = self._runtime_dir / "notifications"
        self._lock_path = self._notifications_dir / f".{target_id}.lock"
        self._claim_path = self._notifications_dir / f".{target_id}.claim.json"
        self._snapshots_dir = self._runtime_dir / "snapshots"

    def compare_and_swap(
        self,
        event_id: str,
        expected: Mapping[str, object] | None,
        replacement: Mapping[str, object],
        *,
        expected_snapshot_sha256: str | None = None,
    ) -> dict[str, object]:
        """Apply an exact replacement and return the durable read-back."""
        path = self._record_path(event_id)
        self._ensure_notifications_directory()
        expected_record = None if expected is None else dict(expected)
        replacement_record = dict(replacement)
        if expected_record is not None:
            self._validate_record(expected_record, event_id)
        self._validate_record(replacement_record, event_id)
        with self._locked():
            current = self._read_record(path)
            if current is not None:
                self._validate_record(current, event_id)
            target_claim = self._load_target_claim()
            if target_claim is not None and target_claim["event_id"] != event_id:
                return {
                    "applied": False,
                    "notification": current,
                    "target_claim": target_claim,
                }
            if current != expected_record:
                return {"applied": False, "notification": current}
            if replacement_record["status"] == "sending":
                self._validate_snapshots_directory()
                current_snapshot = self._read_snapshot(
                    self._snapshot_path(), "local snapshot"
                )
                current_snapshot_sha256 = self._snapshot_sha256(current_snapshot)
                if current_snapshot_sha256 != expected_snapshot_sha256:
                    return {
                        "applied": False,
                        "notification": current,
                        "snapshot_conflict": {
                            "expected_sha256": expected_snapshot_sha256,
                            "current_sha256": current_snapshot_sha256,
                        },
                    }
            if replacement_record["status"] == "sending" and target_claim is None:
                self._write_record(
                    self._claim_path,
                    self._new_claim(event_id, replacement_record["sha256"]),
                )
            self._write_record(path, replacement_record)
            durable = self._read_record(path)
            if durable != replacement_record:
                raise LocalStoreError("notification read-back mismatch")
            return {"applied": True, "notification": durable}

    def read(self, event_id: str) -> dict[str, object] | None:
        """Read one notification record under the per-target lock."""
        path = self._record_path(event_id)
        self._ensure_notifications_directory()
        with self._locked():
            record = self._read_record(path)
            if record is not None:
                self._validate_record(record, event_id)
            return record

    def promote_snapshot(
        self,
        candidate_path: Path,
        *,
        expected_sha256: str | None,
        candidate_sha256: str,
        claim_event_id: str | None,
    ) -> dict[str, object]:
        """Promote a candidate snapshot with target ownership and a baseline CAS."""
        if not _SHA256_RE.fullmatch(candidate_sha256):
            raise LocalStoreError("candidate snapshot hash is invalid")
        if claim_event_id is not None and claim_event_id != notification_event_id(
            self._target_id, candidate_sha256
        ):
            raise LocalStoreError("claim event does not match candidate snapshot")
        if expected_sha256 is not None and not _SHA256_RE.fullmatch(expected_sha256):
            raise LocalStoreError("expected snapshot hash is invalid")

        candidate = self._candidate_path(candidate_path)
        self._ensure_notifications_directory()
        self._ensure_snapshots_directory()
        destination = self._snapshot_path()

        with self._locked():
            target_claim = self._load_target_claim()
            current = self._read_snapshot(destination, "local snapshot")
            current_sha256 = self._snapshot_sha256(current)
            if target_claim is not None:
                if claim_event_id != target_claim["event_id"]:
                    return {
                        "applied": False,
                        "current_sha256": current_sha256,
                        "target_claim": target_claim,
                    }
                notification = self._read_record(
                    self._record_path(target_claim["event_id"])
                )
                if notification is None:
                    raise LocalStoreError("target claim notification is missing")
                self._validate_record(notification, target_claim["event_id"])
                if notification["status"] != "delivered":
                    raise LocalStoreError("target claim notification is not delivered")
            elif claim_event_id is not None:
                return {
                    "applied": False,
                    "current_sha256": current_sha256,
                    "target_claim": None,
                }

            candidate_bytes = self._read_snapshot(candidate, "candidate snapshot")
            if candidate_bytes is None:
                raise LocalStoreError("candidate snapshot is missing")
            if self._snapshot_sha256(candidate_bytes) != candidate_sha256:
                raise LocalStoreError("candidate snapshot hash mismatch")
            if current == candidate_bytes:
                self._fsync_file(destination, "local snapshot")
                self._fsync_directory(destination.parent)
                durable = self._read_snapshot(destination, "local snapshot")
                if durable != candidate_bytes:
                    raise LocalStoreError("snapshot read-back mismatch")
                return {
                    "applied": True,
                    "already": True,
                    "previous_sha256": current_sha256,
                    "sha256": candidate_sha256,
                }
            if current_sha256 != expected_sha256:
                return {
                    "applied": False,
                    "current_sha256": current_sha256,
                }

            self._write_bytes(
                destination,
                candidate_bytes,
                "snapshot",
                _MAX_LOCAL_SNAPSHOT_BYTES,
            )
            durable = self._read_snapshot(destination, "local snapshot")
            if durable != candidate_bytes:
                raise LocalStoreError("snapshot read-back mismatch")
            return {
                "applied": True,
                "already": False,
                "previous_sha256": current_sha256,
                "sha256": candidate_sha256,
            }

    def _candidate_path(self, candidate_path: Path) -> Path:
        base = self._runtime_dir.absolute()
        raw_path = Path(candidate_path)
        path = raw_path if raw_path.is_absolute() else base / raw_path
        path = path.absolute()
        try:
            relative = path.relative_to(base)
        except ValueError as exc:
            raise LocalStoreError(
                "candidate snapshot must be inside runtime_dir"
            ) from exc
        if (
            len(relative.parts) < _MIN_CANDIDATE_PATH_PARTS
            or relative.parts[0] != "candidates"
            or ".." in relative.parts
        ):
            raise LocalStoreError(
                "candidate snapshot must be under the candidates directory"
            )

        current = base
        for part in relative.parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError as exc:
                raise LocalStoreError("candidate snapshot is missing") from exc
            except OSError as exc:
                raise LocalStoreError("cannot inspect candidate snapshot") from exc
            if stat.S_ISLNK(info.st_mode):
                raise LocalStoreError("candidate snapshot must not be a symlink")
        try:
            final_info = current.lstat()
        except OSError as exc:
            raise LocalStoreError("cannot inspect candidate snapshot") from exc
        if stat.S_ISLNK(final_info.st_mode):
            raise LocalStoreError("candidate snapshot must not be a symlink")
        if not stat.S_ISREG(final_info.st_mode):
            raise LocalStoreError("candidate snapshot must be a regular file")
        if current == self._snapshots_dir / f"{self._target_id}.txt":
            raise LocalStoreError("candidate snapshot must differ from destination")
        return current

    def _ensure_snapshots_directory(self) -> None:
        created = False
        try:
            info = self._snapshots_dir.lstat()
        except FileNotFoundError:
            try:
                self._snapshots_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                created = True
            try:
                info = self._snapshots_dir.lstat()
            except OSError as exc:
                raise LocalStoreError(
                    "local snapshots directory is unavailable"
                ) from exc
        except OSError as exc:
            raise LocalStoreError("local snapshots directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LocalStoreError("local snapshots path must be a directory")
        if created:
            self._fsync_directory(self._runtime_dir)

    def _validate_snapshots_directory(self) -> None:
        try:
            info = self._snapshots_dir.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LocalStoreError("local snapshots directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LocalStoreError("local snapshots path must be a directory")

    def _snapshot_path(self) -> Path:
        return self._snapshots_dir / f"{self._target_id}.txt"

    def _load_target_claim(self) -> TargetClaim | None:
        target_claim_record = self._read_claim(self._claim_path)
        target_claim = None
        if target_claim_record is not None:
            target_claim = self._validate_claim(target_claim_record)
            return target_claim

        sending_records = self._find_sending_records()
        if len(sending_records) > 1:
            raise LocalStoreError(
                "multiple sending records require manual reconciliation"
            )
        if sending_records:
            sending_record = sending_records[0]
            if target_claim is None:
                target_claim = self._new_claim(
                    _require_string(sending_record["event_id"], "event_id"),
                    sending_record["sha256"],
                )
                self._write_record(self._claim_path, target_claim)
            elif target_claim["event_id"] != sending_record["event_id"]:
                raise LocalStoreError(
                    "multiple target claims require manual reconciliation"
                )
        return target_claim

    def _find_sending_records(self) -> list[dict[str, object]]:
        try:
            candidates = self._notifications_dir.iterdir()
        except OSError as exc:
            raise LocalStoreError("cannot scan local notification records") from exc

        sending_records: list[dict[str, object]] = []
        for candidate in candidates:
            if candidate in {self._claim_path, self._lock_path}:
                continue
            if not candidate.name.endswith(".json"):
                continue
            event_id = candidate.name[:-5]
            if not _SHA256_RE.fullmatch(event_id):
                continue
            record = self._read_record(candidate)
            if record is None:
                continue
            try:
                record_target_id = _require_string(
                    record.get("target_id"), "notification target_id"
                )
                _validate_local_record(
                    record,
                    target_id=record_target_id,
                    event_id=event_id,
                )
            except WorkflowError as exc:
                raise LocalStoreError("notification record schema is invalid") from exc
            if record_target_id == self._target_id and record["status"] == "sending":
                sending_records.append(record)
        return sending_records

    def release_target_claim(
        self, event_id: str, expected_status: str
    ) -> dict[str, object]:
        """Release a target claim after its notification window is complete."""
        if expected_status not in _RELEASEABLE_NOTIFICATION_STATUSES:
            raise LocalStoreError("target claim release status is invalid")
        path = self._record_path(event_id)
        self._ensure_notifications_directory()
        with self._locked():
            record = self._read_record(path)
            if record is not None:
                self._validate_record(record, event_id)
            target_claim = self._read_claim(self._claim_path)
            if target_claim is None:
                if record is None:
                    raise LocalStoreError("target claim is missing")
                if record["status"] == "sending":
                    raise LocalStoreError("target claim is missing")
                if record["status"] != expected_status:
                    raise LocalStoreError("notification is not ready to release")
                return {"released": False, "notification": record}
            self._validate_claim(target_claim)
            if target_claim["event_id"] != event_id:
                return {
                    "released": False,
                    "notification": record,
                    "target_claim": target_claim,
                }
            if record is None or record["status"] != expected_status:
                raise LocalStoreError("notification is not ready to release")
            try:
                self._claim_path.unlink()
            except OSError as exc:
                raise LocalStoreError("cannot remove local target claim") from exc
            self._fsync_directory(self._notifications_dir)
            if self._read_claim(self._claim_path) is not None:
                raise LocalStoreError("target claim read-back mismatch")
            return {"released": True, "target_claim": target_claim}

    def _validate_record(self, record: Mapping[str, object], event_id: str) -> None:
        try:
            _validate_local_record(
                record,
                target_id=self._target_id,
                event_id=event_id,
            )
        except WorkflowError as exc:
            raise LocalStoreError("notification record schema is invalid") from exc

    def _validate_claim(self, value: object) -> TargetClaim:
        try:
            claim = _validate_target_claim(value)
        except WorkflowError as exc:
            raise LocalStoreError("target claim schema is invalid") from exc
        if claim["target_id"] != self._target_id:
            raise LocalStoreError("target claim target_id does not match")
        return claim

    def _new_claim(self, event_id: str, sha256: object) -> TargetClaim:
        return {
            "version": 1,
            "target_id": self._target_id,
            "event_id": event_id,
            "sha256": _require_string(sha256, "target claim sha256"),
        }

    def _record_path(self, event_id: str) -> Path:
        if not _SHA256_RE.fullmatch(event_id):
            raise LocalStoreError("invalid_event_id")
        return self._notifications_dir / f"{event_id}.json"

    @staticmethod
    def _require_directory(path: Path, field: str) -> Path:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LocalStoreError(f"{field} must be an existing directory") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LocalStoreError(f"{field} must be an existing directory")
        return path

    def _ensure_notifications_directory(self) -> None:
        created = False
        try:
            info = self._notifications_dir.lstat()
        except FileNotFoundError:
            try:
                self._notifications_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                created = True
            try:
                info = self._notifications_dir.lstat()
            except OSError as exc:
                raise LocalStoreError(
                    "local notifications directory is unavailable"
                ) from exc
        except OSError as exc:
            raise LocalStoreError(
                "local notifications directory is unavailable"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LocalStoreError("local notifications path must be a directory")
        if created:
            self._fsync_directory(self._runtime_dir)

    @staticmethod
    def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
        safe_flags = flags
        safe_flags |= int(getattr(os, "O_CLOEXEC", 0))
        safe_flags |= int(getattr(os, "O_NOFOLLOW", 0))
        safe_flags |= int(getattr(os, "O_NONBLOCK", 0))
        file_descriptor = os.open(path, safe_flags, mode)
        try:
            file_mode = os.fstat(file_descriptor).st_mode
        except OSError:
            os.close(file_descriptor)
            raise
        if not stat.S_ISREG(file_mode):
            os.close(file_descriptor)
            raise LocalStoreError("local persistence path must be a regular file")
        return file_descriptor

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        if fcntl is None:
            raise LocalStoreError("local persistence requires POSIX advisory locks")

        file_descriptor: int | None = None
        lock_acquired = False
        try:
            try:
                file_descriptor = self._open_regular(
                    self._lock_path, os.O_RDWR | os.O_CREAT
                )
            except LocalStoreError:
                raise
            except OSError as exc:
                raise LocalStoreError("cannot open local notification lock") from exc
            try:
                os.fchmod(file_descriptor, 0o600)
            except OSError as exc:
                raise LocalStoreError("cannot secure local notification lock") from exc
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_EX)
                lock_acquired = True
            except OSError as exc:
                raise LocalStoreError("cannot acquire local notification lock") from exc
            yield
        finally:
            if lock_acquired and file_descriptor is not None:
                try:
                    fcntl.flock(file_descriptor, fcntl.LOCK_UN)
                except OSError as exc:
                    raise LocalStoreError(
                        "cannot release local notification lock"
                    ) from exc
            if file_descriptor is not None:
                os.close(file_descriptor)

    @classmethod
    def _read_record(cls, path: Path) -> dict[str, object] | None:
        return cls._read_json_object(path, "notification record")

    @classmethod
    def _read_claim(cls, path: Path) -> dict[str, object] | None:
        return cls._read_json_object(path, "target claim")

    @classmethod
    def _read_json_object(
        cls, path: Path, description: str
    ) -> dict[str, object] | None:
        data = cls._read_bytes(path, _MAX_LOCAL_RECORD_BYTES, description)
        if data is None:
            return None
        try:
            value: object = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStoreError(f"{description} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise LocalStoreError(f"{description} must be an object")
        return cast("dict[str, object]", value)

    @classmethod
    def _read_bytes(cls, path: Path, max_bytes: int, description: str) -> bytes | None:
        try:
            file_descriptor = cls._open_regular(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except LocalStoreError:
            raise
        except OSError as exc:
            raise LocalStoreError(f"cannot read {description}: {exc}") from exc
        try:
            with os.fdopen(file_descriptor, "rb") as record_file:
                file_descriptor = -1
                data = record_file.read(max_bytes + 1)
        except OSError as exc:
            raise LocalStoreError(f"cannot read {description}: {exc}") from exc
        finally:
            if file_descriptor != -1:
                os.close(file_descriptor)
        if len(data) > max_bytes:
            raise LocalStoreError(f"{description} is too large")
        return data

    @staticmethod
    def _write_record(path: Path, record: Mapping[str, object]) -> None:
        data = LocalNotificationStore._serialize_record(record)
        LocalNotificationStore._write_bytes(
            path, data, "notification record", _MAX_LOCAL_RECORD_BYTES
        )

    @staticmethod
    def _write_bytes(path: Path, data: bytes, description: str, max_bytes: int) -> None:
        if len(data) > max_bytes:
            raise LocalStoreError(f"{description} is too large")
        temporary_path = LocalNotificationStore._write_temporary_bytes(
            path, data, description
        )
        try:
            temporary_path.replace(path)
            LocalNotificationStore._fsync_directory(path.parent)
        except LocalStoreError:
            raise
        except OSError as exc:
            raise LocalStoreError(f"cannot durably write {description}: {exc}") from exc
        finally:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _write_temporary_record(path: Path, data: bytes) -> Path:
        return LocalNotificationStore._write_temporary_bytes(
            path, data, "notification record"
        )

    @staticmethod
    def _write_temporary_bytes(path: Path, data: bytes, description: str) -> Path:
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
        except OSError as exc:
            raise LocalStoreError(f"cannot create temporary {description}") from exc
        temporary_path = Path(temporary_name)
        completed = False
        try:
            os.fchmod(file_descriptor, 0o600)
            LocalNotificationStore._write_and_sync(file_descriptor, data)
            completed = True
        except OSError as exc:
            raise LocalStoreError(
                f"cannot write temporary {description}: {exc}"
            ) from exc
        finally:
            os.close(file_descriptor)
            if not completed:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
        return temporary_path

    @staticmethod
    def _write_and_sync(file_descriptor: int, data: bytes) -> None:
        with os.fdopen(file_descriptor, "wb", closefd=False) as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

    @staticmethod
    def _serialize_record(record: Mapping[str, object]) -> bytes:
        try:
            data = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise LocalStoreError(
                f"cannot serialize notification record: {exc}"
            ) from exc
        if len(data) > _MAX_LOCAL_RECORD_BYTES:
            raise LocalStoreError("notification record is too large")
        return data

    @classmethod
    def _read_snapshot(cls, path: Path, description: str) -> bytes | None:
        data = cls._read_bytes(path, _MAX_LOCAL_SNAPSHOT_BYTES, description)
        if data is None:
            return None
        if not data:
            raise LocalStoreError(f"{description} is empty")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalStoreError(f"{description} is not valid UTF-8") from exc
        return data

    @staticmethod
    def _snapshot_sha256(data: bytes | None) -> str | None:
        if data is None:
            return None
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _fsync_file(cls, path: Path, description: str) -> None:
        try:
            file_descriptor = cls._open_regular(path, os.O_RDONLY)
        except OSError as exc:
            raise LocalStoreError(f"cannot open {description}") from exc
        try:
            os.fsync(file_descriptor)
        except OSError as exc:
            raise LocalStoreError(f"cannot fsync {description}") from exc
        finally:
            os.close(file_descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_DIRECTORY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        try:
            file_descriptor = os.open(path, flags)
        except OSError as exc:
            raise LocalStoreError("cannot open local persistence directory") from exc
        try:
            os.fsync(file_descriptor)
        except OSError as exc:
            raise LocalStoreError("cannot fsync local persistence directory") from exc
        finally:
            os.close(file_descriptor)


def _notification_context(
    payload: Mapping[str, object],
) -> tuple[str, str, str, str, str]:
    target_id = _require_string(payload.get("target_id"), "target_id")
    sha256 = _require_string(payload.get("sha256"), "sha256")
    event_id = notification_event_id(target_id, sha256)
    destination = _require_string(payload.get("destination"), "destination")
    message = _require_string(payload.get("message"), "message")
    return target_id, sha256, event_id, destination, message


def _validate_notification(
    value: object,
    *,
    target_id: str,
    sha256: str,
    event_id: str,
    destination: str,
) -> NotificationRecord:
    record = _require_mapping(value, "notification")
    if type(record.get("version")) is not int or record.get("version") != 1:
        raise WorkflowError("notification version must be 1")
    expected = {
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
    }
    for field, expected_value in expected.items():
        if record.get(field) != expected_value:
            raise WorkflowError(f"notification {field} does not match the event")
    message = _require_string(record.get("message"), "notification message")
    status = _require_string(record.get("status"), "notification status")
    if status not in _NOTIFICATION_STATUSES:
        raise WorkflowError("notification status is invalid")
    attempt = record.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise WorkflowError("notification attempt must be a non-negative integer")
    last_error = _require_string(
        record.get("last_error", ""), "notification last_error", allow_empty=True
    )
    updated_at = _require_string(record.get("updated_at"), "notification updated_at")
    return {
        "version": 1,
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
        "message": message,
        "status": status,
        "attempt": attempt,
        "last_error": last_error,
        "updated_at": updated_at,
    }


def _validate_target_claim(value: object) -> TargetClaim:
    """Validate the exact schema stored by the local target-claim backend."""
    claim = _require_mapping(value, "target claim")
    if frozenset(claim) != _TARGET_CLAIM_FIELDS:
        raise WorkflowError("target claim fields are invalid")
    if type(claim.get("version")) is not int or claim.get("version") != 1:
        raise WorkflowError("target claim version is invalid")
    target_id = _require_string(claim.get("target_id"), "target claim target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("target claim target_id is invalid")
    event_id = _require_string(claim.get("event_id"), "target claim event_id")
    sha256 = _require_string(claim.get("sha256"), "target claim sha256")
    if notification_event_id(target_id, sha256) != event_id:
        raise WorkflowError("target claim event_id does not match")
    return {
        "version": 1,
        "target_id": target_id,
        "event_id": event_id,
        "sha256": sha256,
    }


def _validate_local_record(
    value: Mapping[str, object], *, target_id: str, event_id: str
) -> dict[str, object]:
    """Validate the exact schema stored by the local notification backend."""
    if frozenset(value) != _NOTIFICATION_FIELDS:
        raise WorkflowError("notification record fields are invalid")
    record_target_id = _require_string(value.get("target_id"), "target_id")
    record_sha256 = _require_string(value.get("sha256"), "sha256")
    if record_target_id != target_id:
        raise WorkflowError("notification target_id does not match the event")
    if not _SHA256_RE.fullmatch(record_sha256):
        raise WorkflowError("notification sha256 is invalid")
    if notification_event_id(target_id, record_sha256) != event_id:
        raise WorkflowError("notification event_id does not match the event")
    _validate_notification(
        value,
        target_id=target_id,
        sha256=record_sha256,
        event_id=event_id,
        destination=_require_string(value.get("destination"), "destination"),
    )
    return dict(value)


def _new_notification(
    *, target_id: str, sha256: str, event_id: str, destination: str, message: str
) -> NotificationRecord:
    return {
        "version": 1,
        "event_id": event_id,
        "target_id": target_id,
        "sha256": sha256,
        "destination": destination,
        "message": message,
        "status": "pending",
        "attempt": 0,
        "last_error": "",
        "updated_at": _timestamp(),
    }


def _replace_status(
    record: NotificationRecord,
    status: str,
    *,
    attempt: int | None = None,
    last_error: str | None = None,
) -> NotificationRecord:
    updated = record.copy()
    updated["status"] = status
    if attempt is not None:
        updated["attempt"] = attempt
    if last_error is not None:
        updated["last_error"] = last_error
    updated["updated_at"] = _timestamp()
    return updated


def _protocol_response(action: str, **fields: object) -> dict[str, object]:
    """Return one versioned notification protocol response."""
    response: dict[str, object] = {
        "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
        "action": action,
    }
    response.update(fields)
    return response


def _compare_and_swap_response(
    expected: NotificationRecord | None,
    replacement: NotificationRecord,
    next_signal: str,
    expected_snapshot_sha256: str | None,
) -> dict[str, object]:
    """Describe one exact durable notification replacement."""
    return _protocol_response(
        "compare_and_swap",
        expected_notification=expected,
        notification=replacement,
        expected_snapshot_sha256=expected_snapshot_sha256,
        next_signal=next_signal,
    )


def _strict_notification(
    value: object,
    *,
    field: str,
    target_id: str,
    sha256: str,
    event_id: str,
    destination: str | None = None,
) -> dict[str, object]:
    record = _require_mapping(value, field)
    if frozenset(record) != _NOTIFICATION_FIELDS:
        raise WorkflowError(f"{field} must contain exactly the notification fields")
    record_destination = _require_string(
        record.get("destination"), f"{field} destination"
    )
    if destination is not None and record_destination != destination:
        raise WorkflowError(f"{field} destination does not match the event")
    _validate_notification(
        record,
        target_id=target_id,
        sha256=sha256,
        event_id=event_id,
        destination=record_destination,
    )
    return dict(record)


def local_notification_cas(
    payload: Mapping[str, object], runtime_dir: str | Path
) -> dict[str, object]:
    """Apply one protocol CAS in the local durable notification store."""
    if frozenset(payload) not in {_LOCAL_CAS_FIELDS, _LOCAL_CAS_LEGACY_FIELDS}:
        raise WorkflowError("local CAS request fields are invalid")
    if (
        type(payload.get("protocol_version")) is not int
        or payload.get("protocol_version") != _NOTIFICATION_PROTOCOL_VERSION
    ):
        raise WorkflowError("protocol_version must be 2")
    if payload.get("action") != "compare_and_swap":
        raise WorkflowError("action must be compare_and_swap")
    expected_snapshot_sha256 = _snapshot_hash(
        payload.get("expected_snapshot_sha256"),
        "expected_snapshot_sha256",
        allow_none=True,
    )
    next_signal = _require_string(payload.get("next_signal"), "next_signal")
    if next_signal not in _LOCAL_CAS_SIGNALS:
        raise WorkflowError("next_signal is invalid")

    raw_replacement = _require_mapping(payload.get("notification"), "notification")
    target_id = _require_string(raw_replacement.get("target_id"), "target_id")
    sha256 = _require_string(raw_replacement.get("sha256"), "sha256")
    event_id = notification_event_id(target_id, sha256)
    replacement = _strict_notification(
        raw_replacement,
        field="notification",
        target_id=target_id,
        sha256=sha256,
        event_id=event_id,
    )
    replacement_destination = _require_string(
        replacement.get("destination"), "notification destination"
    )

    raw_expected = payload.get("expected_notification")
    if raw_expected is None:
        expected = None
    else:
        expected = _strict_notification(
            raw_expected,
            field="expected_notification",
            target_id=target_id,
            sha256=sha256,
            event_id=event_id,
            destination=replacement_destination,
        )

    result = LocalNotificationStore(runtime_dir, target_id).compare_and_swap(
        event_id,
        expected,
        replacement,
        expected_snapshot_sha256=expected_snapshot_sha256,
    )
    if not result["applied"]:
        snapshot_conflict = result.get("snapshot_conflict")
        if snapshot_conflict is not None:
            conflict = _require_mapping(snapshot_conflict, "snapshot conflict")
            return {
                "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
                "action": "snapshot_compare_and_swap_conflict",
                "expected_snapshot_sha256": conflict["expected_sha256"],
                "current_snapshot_sha256": conflict["current_sha256"],
                "notification": result["notification"],
            }
        target_claim = result.get("target_claim")
        if target_claim is not None:
            return {
                "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
                "action": "target_claim_conflict",
                "target_claim": target_claim,
                "notification": result["notification"],
            }
        return {
            "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
            "action": "compare_and_swap_conflict",
            "notification": result["notification"],
        }
    return {
        "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
        "action": "compare_and_swap_applied",
        "notification": result["notification"],
        "next_signal": next_signal,
    }


def local_notification_release(
    payload: Mapping[str, object], runtime_dir: str | Path
) -> dict[str, object]:
    """Release a local target claim after terminal notification handling."""
    if frozenset(payload) != _LOCAL_RELEASE_FIELDS:
        raise WorkflowError("local claim release request fields are invalid")
    if (
        type(payload.get("protocol_version")) is not int
        or payload.get("protocol_version") != _NOTIFICATION_PROTOCOL_VERSION
    ):
        raise WorkflowError("protocol_version must be 2")
    if payload.get("action") != "release_target_claim":
        raise WorkflowError("action must be release_target_claim")
    target_id = _require_string(payload.get("target_id"), "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    event_id = _require_string(payload.get("event_id"), "event_id")
    expected_status = _require_string(payload.get("expected_status"), "expected_status")
    if expected_status not in _RELEASEABLE_NOTIFICATION_STATUSES:
        raise WorkflowError("expected_status must be pending or delivered")

    result = LocalNotificationStore(runtime_dir, target_id).release_target_claim(
        event_id, expected_status
    )
    target_claim = result.get("target_claim")
    if not result["released"] and target_claim is not None:
        return {
            "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
            "action": "target_claim_conflict",
            "target_claim": target_claim,
        }
    return {
        "protocol_version": _NOTIFICATION_PROTOCOL_VERSION,
        "action": (
            "target_claim_released"
            if result["released"]
            else "target_claim_already_released"
        ),
        "target_id": target_id,
        "event_id": event_id,
    }


def local_snapshot_promote(
    payload: Mapping[str, object],
    runtime_dir: str | Path,
    candidate_path: str | Path,
) -> dict[str, object]:
    """Promote one local candidate through a target-scoped snapshot CAS."""
    if frozenset(payload) != _LOCAL_SNAPSHOT_FIELDS:
        raise WorkflowError("local snapshot request fields are invalid")
    if (
        type(payload.get("version")) is not int
        or payload.get("version") != _LOCAL_SNAPSHOT_PROTOCOL_VERSION
    ):
        raise WorkflowError("version must be 1")
    if payload.get("action") != "promote_snapshot":
        raise WorkflowError("action must be promote_snapshot")
    target_id = _require_string(payload.get("target_id"), "target_id")
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise WorkflowError("invalid_target_id")
    expected_sha256 = _snapshot_hash(
        payload.get("expected_sha256"), "expected_sha256", allow_none=True
    )
    candidate_sha256 = _snapshot_hash(
        payload.get("candidate_sha256"), "candidate_sha256"
    )
    if candidate_sha256 is None:
        raise WorkflowError("candidate_sha256 is required")
    raw_claim_event_id = payload.get("claim_event_id")
    claim_event_id = _snapshot_hash(
        raw_claim_event_id, "claim_event_id", allow_none=True
    )
    expected_event_id = notification_event_id(target_id, candidate_sha256)
    if claim_event_id is not None and claim_event_id != expected_event_id:
        raise WorkflowError("claim_event_id does not match candidate_sha256")
    result = LocalNotificationStore(runtime_dir, target_id).promote_snapshot(
        Path(candidate_path),
        expected_sha256=expected_sha256,
        candidate_sha256=candidate_sha256,
        claim_event_id=claim_event_id,
    )
    if not result["applied"]:
        target_claim = result.get("target_claim")
        if "target_claim" in result:
            return {
                "version": _LOCAL_SNAPSHOT_PROTOCOL_VERSION,
                "action": "target_claim_conflict",
                "current_sha256": result["current_sha256"],
                "target_claim": target_claim,
            }
        return {
            "version": _LOCAL_SNAPSHOT_PROTOCOL_VERSION,
            "action": "snapshot_compare_and_swap_conflict",
            "expected_sha256": expected_sha256,
            "current_sha256": result["current_sha256"],
        }
    return {
        "version": _LOCAL_SNAPSHOT_PROTOCOL_VERSION,
        "action": (
            "snapshot_already_promoted" if result["already"] else "snapshot_promoted"
        ),
        "target_id": target_id,
        "previous_sha256": result["previous_sha256"],
        "candidate_sha256": result["sha256"],
    }


def notification_step(payload: Mapping[str, object]) -> dict[str, object]:
    """Advance the durable notification protocol by one verified step."""
    target_id, sha256, event_id, destination, message = _notification_context(payload)
    expected_snapshot_sha256 = _snapshot_hash(
        payload.get("expected_snapshot_sha256"),
        "expected_snapshot_sha256",
        allow_none=True,
    )
    signal = _require_string(payload.get("signal", "start"), "signal")
    raw_record = payload.get("notification")
    record = (
        None
        if raw_record is None
        else _validate_notification(
            raw_record,
            target_id=target_id,
            sha256=sha256,
            event_id=event_id,
            destination=destination,
        )
    )

    if signal == "start":
        if record is None:
            return _compare_and_swap_response(
                None,
                _new_notification(
                    target_id=target_id,
                    sha256=sha256,
                    event_id=event_id,
                    destination=destination,
                    message=message,
                ),
                "pending_persisted",
                expected_snapshot_sha256,
            )
        status = record["status"]
        if status == "pending":
            return _compare_and_swap_response(
                record,
                _replace_status(
                    record,
                    "sending",
                    attempt=record["attempt"] + 1,
                    last_error="",
                ),
                "sending_claimed",
                expected_snapshot_sha256,
            )
        if status == "sending":
            return _protocol_response("manual_reconciliation")
        return _protocol_response("promote_snapshot")

    if record is None:
        raise WorkflowError("notification is required after start")
    status = record["status"]
    if signal == "pending_persisted":
        if status != "pending":
            raise WorkflowError("pending_persisted requires a pending notification")
        return _compare_and_swap_response(
            record,
            _replace_status(
                record,
                "sending",
                attempt=record["attempt"] + 1,
                last_error="",
            ),
            "sending_claimed",
            expected_snapshot_sha256,
        )
    if signal == "sending_claimed":
        if status != "sending":
            raise WorkflowError("sending_claimed requires a sending notification")
        return _protocol_response("send_slack", notification=record)
    if signal == "slack_delivered":
        if status != "sending":
            raise WorkflowError("slack_delivered requires a sending notification")
        return _compare_and_swap_response(
            record,
            _replace_status(record, "delivered", last_error=""),
            "delivered_persisted",
            expected_snapshot_sha256,
        )
    if signal == "slack_failed":
        if status != "sending":
            raise WorkflowError("slack_failed requires a sending notification")
        error = _require_string(payload.get("error"), "error")
        return _compare_and_swap_response(
            record,
            _replace_status(record, "pending", last_error=error),
            "failure_persisted",
            expected_snapshot_sha256,
        )
    if signal == "delivered_persisted":
        if status != "delivered":
            raise WorkflowError("delivered_persisted requires a delivered notification")
        return _protocol_response("promote_snapshot")
    if signal == "failure_persisted":
        if status != "pending":
            raise WorkflowError("failure_persisted requires a pending notification")
        return _protocol_response("stop")
    raise WorkflowError("signal is invalid")


def _read_payload() -> dict[str, object]:
    try:
        payload: object = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise WorkflowError("stdin must contain one JSON object") from exc
    if not isinstance(payload, dict):
        raise WorkflowError("stdin must contain one JSON object")
    return cast("dict[str, object]", payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic workflow state."
    )
    parser.add_argument(
        "operation",
        choices=(
            "validate-targets",
            "change-action",
            "notification-step",
            "local-notification-cas",
            "local-notification-release",
            "local-snapshot-promote",
        ),
    )
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--candidate", type=Path)
    return parser


def run(
    operation: str,
    payload: Mapping[str, object],
    *,
    runtime_dir: str | Path | None = None,
    candidate_path: str | Path | None = None,
) -> dict[str, object]:
    """Execute one workflow operation."""
    if operation == "validate-targets":
        return validate_targets(payload)
    if operation == "change-action":
        return change_action(payload)
    if operation == "notification-step":
        return notification_step(payload)
    if operation == "local-notification-cas":
        if runtime_dir is None:
            raise WorkflowError("--runtime-dir is required for local persistence")
        return local_notification_cas(payload, runtime_dir)
    if operation == "local-notification-release":
        if runtime_dir is None:
            raise WorkflowError("--runtime-dir is required for local persistence")
        return local_notification_release(payload, runtime_dir)
    if operation == "local-snapshot-promote":
        if runtime_dir is None:
            raise WorkflowError("--runtime-dir is required for local persistence")
        if candidate_path is None:
            raise WorkflowError("--candidate is required")
        return local_snapshot_promote(payload, runtime_dir, candidate_path)
    raise WorkflowError("operation is invalid")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    try:
        args = _parser().parse_args(argv)
        result = run(
            args.operation,
            _read_payload(),
            runtime_dir=args.runtime_dir,
            candidate_path=args.candidate,
        )
    except (LocalStoreError, WorkflowError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
