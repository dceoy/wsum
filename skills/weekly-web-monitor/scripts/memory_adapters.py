"""Fixture-only adapters for dry runs and tests; never use as durable storage."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from errors import MonitorError
from fetch import FetchResult
from notifications import ConfirmedDeliveryFailureError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from audit import AuditRecord
    from models import NotificationRecord, RunRecord, State, Target
    from outbox import OutboxRecord

_NOT_MODIFIED_STATUS = 304


class MemoryOperationalStore:
    """In-memory OperationalStore/AuditSink fixture for dry runs and tests."""

    def __init__(self, targets: Sequence[Target]) -> None:
        """Seed the store with a fixed list of targets."""
        self.targets = list(targets)
        self.states: dict[str, State] = {}
        self.runs: dict[str, RunRecord] = {}
        self.notifications: dict[str, NotificationRecord] = {}
        self.outbox: dict[str, OutboxRecord] = {}
        self.audit: list[AuditRecord] = []

    def load_enabled_targets(self) -> list[Target]:
        """Return the enabled targets, in seeded order."""
        return [target for target in self.targets if target.enabled]

    def get_state(self, target_id: str) -> State | None:
        """Return the stored state for ``target_id``, if any."""
        return self.states.get(target_id)

    def replace_state(self, state: State) -> None:
        """Insert or replace the stored state for its target."""
        self.states[state.target_id] = state

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the stored run record for ``run_id``, if any."""
        return self.runs.get(run_id)

    def append_run(self, run: RunRecord) -> None:
        """Idempotently append a run record, keyed by run_id."""
        self.runs.setdefault(run.run_id, run)

    def get_notification(self, event_id: str) -> NotificationRecord | None:
        """Return the stored notification record for ``event_id``, if any."""
        return self.notifications.get(event_id)

    def upsert_notification(self, notification: NotificationRecord) -> None:
        """Insert or update a single notification record."""
        self.notifications[notification.event_id] = notification

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None:
        """Insert or update all ``notifications`` as a single atomic batch.

        Raises:
            MonitorError: If ``notifications`` contains a duplicate event_id.
        """
        updates = {item.event_id: item for item in notifications}
        if len(updates) != len(notifications):
            msg = "notification_invalid"
            raise MonitorError(msg, "notification batch contains duplicate IDs")
        self.notifications = {**self.notifications, **updates}

    def append_audit(self, record: AuditRecord) -> None:
        """Append one audit record."""
        self.audit.append(record)

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        """Return the stored Outbox record for ``event_id``, if any."""
        return self.outbox.get(event_id)

    def upsert_outbox(self, record: OutboxRecord) -> None:
        """Insert or update a single Outbox record."""
        self.outbox[record.event_id] = record


class MemoryDriveConnector:
    """In-memory DriveConnector fixture for dry runs and tests."""

    def __init__(self) -> None:
        """Create an empty in-memory Drive fixture."""
        self.paths: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.created: dict[str, str] = {}
        self.fail_upload = False

    def find_file(self, path: str) -> str | None:
        """Return the file reference stored at ``path``, if any."""
        return self.paths.get(path)

    def upload_file(self, path: str, content: bytes, mime_type: str) -> str:
        """Store ``content`` at ``path`` and return a fixture file reference.

        Returns:
            The fixture Drive file reference for ``path``.

        Raises:
            MonitorError: If ``fail_upload`` is set on this fixture.
        """
        del mime_type
        if self.fail_upload:
            msg = "drive_write_failed"
            raise MonitorError(msg, "fixture upload failed", retryable=True)
        reference = f"drive:{hashlib.sha256(path.encode()).hexdigest()}"
        self.paths[path] = reference
        self.files[reference] = content
        self.created[reference] = datetime.now(UTC).isoformat()
        return reference

    def download_file(self, file_ref: str) -> bytes:
        """Return the raw bytes stored at ``file_ref``.

        Raises:
            MonitorError: If ``file_ref`` is not stored.
        """
        if file_ref not in self.files:
            msg = "snapshot_missing"
            raise MonitorError(msg, "fixture snapshot is missing")
        return self.files[file_ref]

    def list_files(self, prefix: str) -> list[dict[str, str]]:
        """List file metadata for every stored file under ``prefix``.

        Returns:
            One dict per matching file, with path/file_ref/created_at keys.
        """
        return [
            {
                "path": path,
                "file_ref": reference,
                "created_at": self.created[reference],
            }
            for path, reference in self.paths.items()
            if path.startswith(prefix)
        ]

    def delete_file(self, file_ref: str) -> None:
        """Delete the file referenced by ``file_ref``, if stored."""
        self.files.pop(file_ref, None)
        self.created.pop(file_ref, None)
        for path, reference in list(self.paths.items()):
            if reference == file_ref:
                del self.paths[path]


@dataclass
class FixtureResponse:
    """A canned HTTP-like response for :class:`FixtureFetcher`."""

    body: bytes
    content_type: str = "text/html"
    charset: str = ""
    status: int = 200
    etag: str = ""
    last_modified: str = ""


class FixtureFetcher:
    """A deterministic TargetFetcher fixture backed by canned responses."""

    def __init__(
        self, responses: Mapping[str, FixtureResponse | BaseException]
    ) -> None:
        """Seed the fetcher with one canned response or exception per target."""
        self.responses = dict(responses)
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.workspaces: list[str] = []

    def fetch(self, target: Target, state: State, workspace: Path) -> FetchResult:
        """Return the canned response for ``target``, raising if it is an exception.

        Returns:
            The fixture FetchResult for this target.
        """
        self.calls[target.target_id] += 1
        self.workspaces.append(str(workspace))
        response = self.responses[target.target_id]
        if isinstance(response, BaseException):
            raise response
        if response.status == _NOT_MODIFIED_STATUS:
            return FetchResult(
                result="unchanged",
                final_url=target.url,
                status=304,
                content_type="",
                charset="",
                content_length=0,
                etag=response.etag or state.etag,
                last_modified=response.last_modified or state.last_modified,
                fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                redirect_count=0,
            )
        return FetchResult(
            result="fetched",
            final_url=target.url,
            status=response.status,
            content_type=response.content_type,
            charset=response.charset,
            content_length=len(response.body),
            etag=response.etag,
            last_modified=response.last_modified,
            fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            redirect_count=0,
            body=response.body,
        )


class MemorySlackConnector:
    """In-memory SlackConnector fixture for dry runs and tests."""

    def __init__(self, fail_groups: Sequence[str] = ()) -> None:
        """Seed the connector with the notification groups that fail to send."""
        self.fail_groups = set(fail_groups)
        self.messages: list[tuple[str, str, str]] = []

    def send_message(self, notification_group: str, message: str) -> str:
        """Record the message and return a delivery reference.

        Returns:
            The fixture delivery reference for the recorded message.

        Raises:
            ConfirmedDeliveryFailureError: If ``notification_group`` is in
                this fixture's configured failing groups.
        """
        if notification_group in self.fail_groups:
            msg = "notification_send_failed"
            raise ConfirmedDeliveryFailureError(
                msg,
                "fixture Slack delivery failed",
                retryable=True,
            )
        reference = f"slack:{len(self.messages) + 1}"
        self.messages.append((notification_group, message, reference))
        return reference


class EvidenceSummaryClient:
    """Fixture model that creates a schema-valid material summary from one section."""

    def summarize(  # ruff: ignore[no-self-use] -- instance method to conform to the
        # summary-client interface used elsewhere via duck typing
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a schema-valid material summary from the request's first section.

        Returns:
            A material=True summary payload matching the response schema.
        """
        section = request["changed_sections"][0]
        before = next(iter(section["before"]), "")
        after = next(iter(section["after"]), "")
        source_url = request["target"]["source_url"]
        return {
            "material": True,
            "significance": request["deterministic_assessment"]["significance"],
            "summary_ja": "監視対象の内容に重要な変更が確認されました。",
            "evidence": [
                {
                    "section_id": section["section_id"],
                    "claim_ja": "該当箇所の変更を確認しました。",
                    "before": before,
                    "after": after,
                }
            ],
            "recommended_action_ja": "変更内容を確認してください。",
            "notification_text_ja": (
                f"監視対象の内容が更新されました。詳細を確認してください。\n{source_url}"
            ),
            "source_url": source_url,
        }
