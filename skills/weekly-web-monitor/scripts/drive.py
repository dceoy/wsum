"""Idempotent Google Drive snapshot operations through an injected connector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from errors import MonitorError
from models import HASH_RE, validate_target_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from diff import DiffResult
    from normalize import NormalizedContent


class DriveConnector(Protocol):
    """Least-privilege connector surface used by SnapshotStore."""

    def find_file(self, path: str) -> str | None: ...

    def upload_file(self, path: str, content: bytes, mime_type: str) -> str: ...

    def download_file(self, file_ref: str) -> bytes: ...

    def list_files(self, prefix: str) -> Sequence[Mapping[str, Any]]: ...

    def delete_file(self, file_ref: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SnapshotPaths:
    normalized: str
    metadata: str
    diff: str


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    file_ref: str
    path: str


def snapshot_paths(
    target_id: str, normalized_hash: str, previous_hash: str = ""
) -> SnapshotPaths:
    validate_target_id(target_id)
    if not HASH_RE.fullmatch(normalized_hash):
        msg = "invalid_snapshot"
        raise MonitorError(msg, "normalized_hash is invalid")
    if previous_hash and not HASH_RE.fullmatch(previous_hash):
        msg = "invalid_snapshot"
        raise MonitorError(msg, "previous_hash is invalid")
    prefix = f"snapshots/{target_id}/{normalized_hash}"
    diff_name = f"diff-{previous_hash}.json" if previous_hash else "diff.json"
    return SnapshotPaths(
        normalized=f"{prefix}/normalized.txt",
        metadata=f"{prefix}/metadata.json",
        diff=f"{prefix}/{diff_name}",
    )


class SnapshotStore:
    def __init__(
        self, connector: DriveConnector, *, max_snapshot_bytes: int = 10_000_000
    ) -> None:
        if not 1_024 <= max_snapshot_bytes <= 50_000_000:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "snapshot size limit is invalid"
            )
        self._connector = connector
        self._max_snapshot_bytes = max_snapshot_bytes

    def _ensure_file(self, path: str, content: bytes, mime_type: str) -> str:
        try:
            existing = self._connector.find_file(path)
            if existing:
                if len(existing) > 1_000:
                    msg = "drive_reference_invalid"
                    raise MonitorError(
                        msg,
                        "Drive connector returned an oversized file reference",
                    )
                return existing
            reference = self._connector.upload_file(path, content, mime_type)
        except MonitorError:
            raise
        except Exception as exc:
            msg = "drive_write_failed"
            raise MonitorError(
                msg,
                "Drive snapshot write failed",
                retryable=True,
            ) from exc
        if not reference or len(reference) > 1_000:
            msg = "drive_write_failed"
            raise MonitorError(
                msg,
                "Drive connector returned no usable file reference",
            )
        return reference

    def save(
        self,
        target_id: str,
        content: NormalizedContent,
        diff: DiffResult | None = None,
        previous_hash: str = "",
    ) -> str:
        paths = snapshot_paths(target_id, content.normalized_hash, previous_hash)
        normalized_bytes = content.text.encode("utf-8")
        if len(normalized_bytes) > self._max_snapshot_bytes:
            msg = "snapshot_too_large"
            raise MonitorError(
                msg, "normalized snapshot exceeds the size limit"
            )
        normalized_ref = self._ensure_file(
            paths.normalized, normalized_bytes, "text/plain; charset=utf-8"
        )
        metadata = content.as_dict(include_text=False)
        self._ensure_file(
            paths.metadata,
            json.dumps(
                metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            "application/json",
        )
        if diff is not None:
            self._ensure_file(
                paths.diff,
                json.dumps(
                    diff.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                "application/json",
            )
        return normalized_ref

    def load_normalized(self, snapshot_ref: str) -> str:
        if not snapshot_ref or len(snapshot_ref) > 1_000:
            msg = "snapshot_missing"
            raise MonitorError(msg, "snapshot reference is missing")
        try:
            content = self._connector.download_file(snapshot_ref)
            if not isinstance(content, bytes):
                msg = "snapshot_invalid"
                raise MonitorError(
                    msg, "stored snapshot is not a byte sequence"
                )
            if len(content) > self._max_snapshot_bytes:
                msg = "snapshot_too_large"
                raise MonitorError(
                    msg, "stored snapshot exceeds the size limit"
                )
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "snapshot_invalid"
            raise MonitorError(
                msg, "stored normalized snapshot is not UTF-8"
            ) from exc
        except MonitorError:
            raise
        except Exception as exc:
            msg = "snapshot_missing"
            raise MonitorError(
                msg, "stored normalized snapshot could not be loaded"
            ) from exc

    def plan_cleanup(
        self,
        target_id: str,
        *,
        current_ref: str,
        retain_snapshots: int = 12,
    ) -> list[CleanupCandidate]:
        validate_target_id(target_id)
        if retain_snapshots < 1:
            msg = "invalid_configuration"
            raise MonitorError(
                msg, "retain_snapshots must be at least one"
            )
        try:
            files = list(self._connector.list_files(f"snapshots/{target_id}/"))
        except Exception as exc:
            msg = "connector_unavailable"
            raise MonitorError(
                msg,
                "Drive snapshot listing failed",
                retryable=True,
            ) from exc
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for file in files:
            path = str(file.get("path", ""))
            parts = path.split("/")
            if len(parts) != 4 or parts[:2] != ["snapshots", target_id]:
                continue
            groups.setdefault(parts[2], []).append(file)
        ordered = sorted(
            groups.items(),
            key=lambda item: max(str(file.get("created_at", "")) for file in item[1]),
            reverse=True,
        )
        retained_hashes = {digest for digest, _ in ordered[:retain_snapshots]}
        current_group_digest = next(
            (
                digest
                for digest, group in ordered
                if any(str(file.get("file_ref", "")) == current_ref for file in group)
            ),
            None,
        )
        if current_ref and current_group_digest is not None:
            # The current baseline's hash can fall outside the newest
            # ``retain_snapshots`` groups (e.g. an older capture is still
            # the active baseline). Retain that whole group so its
            # metadata/diff audit artifacts survive alongside normalized.txt,
            # not just the single file matching ``current_ref``.
            retained_hashes.add(current_group_digest)
        candidates: list[CleanupCandidate] = []
        for digest, group in ordered:
            if digest in retained_hashes:
                continue
            for file in group:
                file_ref = str(file.get("file_ref", ""))
                path = str(file.get("path", ""))
                if file_ref and file_ref != current_ref:
                    candidates.append(CleanupCandidate(file_ref, path))
        return candidates

    def execute_cleanup(
        self, candidates: Sequence[CleanupCandidate], *, current_ref: str
    ) -> None:
        for candidate in candidates:
            if candidate.file_ref == current_ref:
                continue
            self._connector.delete_file(candidate.file_ref)
