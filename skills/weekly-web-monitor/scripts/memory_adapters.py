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

    from audit import AuditRecord
    from models import NotificationRecord, RunRecord, State, Target
    from outbox import OutboxRecord


class MemoryOperationalStore:
    def __init__(self, targets: Sequence[Target]) -> None:
        self.targets = list(targets)
        self.states: dict[str, State] = {}
        self.runs: dict[str, RunRecord] = {}
        self.notifications: dict[str, NotificationRecord] = {}
        self.outbox: dict[str, OutboxRecord] = {}
        self.audit: list[AuditRecord] = []

    def load_enabled_targets(self) -> list[Target]:
        return [target for target in self.targets if target.enabled]

    def get_state(self, target_id: str) -> State | None:
        return self.states.get(target_id)

    def replace_state(self, state: State) -> None:
        self.states[state.target_id] = state

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def append_run(self, run: RunRecord) -> None:
        self.runs.setdefault(run.run_id, run)

    def get_notification(self, event_id: str) -> NotificationRecord | None:
        return self.notifications.get(event_id)

    def upsert_notification(self, notification: NotificationRecord) -> None:
        self.notifications[notification.event_id] = notification

    def upsert_notifications_atomically(
        self, notifications: Sequence[NotificationRecord]
    ) -> None:
        updates = {item.event_id: item for item in notifications}
        if len(updates) != len(notifications):
            msg = "notification_invalid"
            raise MonitorError(
                msg, "notification batch contains duplicate IDs"
            )
        self.notifications = {**self.notifications, **updates}

    def append_audit(self, record: AuditRecord) -> None:
        self.audit.append(record)

    def get_outbox(self, event_id: str) -> OutboxRecord | None:
        return self.outbox.get(event_id)

    def upsert_outbox(self, record: OutboxRecord) -> None:
        self.outbox[record.event_id] = record


class MemoryDriveConnector:
    def __init__(self) -> None:
        self.paths: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.created: dict[str, str] = {}
        self.fail_upload = False

    def find_file(self, path: str) -> str | None:
        return self.paths.get(path)

    def upload_file(self, path: str, content: bytes, mime_type: str) -> str:
        del mime_type
        if self.fail_upload:
            msg = "drive_write_failed"
            raise MonitorError(
                msg, "fixture upload failed", retryable=True
            )
        reference = f"drive:{hashlib.sha256(path.encode()).hexdigest()}"
        self.paths[path] = reference
        self.files[reference] = content
        self.created[reference] = datetime.now(UTC).isoformat()
        return reference

    def download_file(self, file_ref: str) -> bytes:
        if file_ref not in self.files:
            msg = "snapshot_missing"
            raise MonitorError(msg, "fixture snapshot is missing")
        return self.files[file_ref]

    def list_files(self, prefix: str) -> list[dict[str, str]]:
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
        self.files.pop(file_ref, None)
        self.created.pop(file_ref, None)
        for path, reference in list(self.paths.items()):
            if reference == file_ref:
                del self.paths[path]


@dataclass
class FixtureResponse:
    body: bytes
    content_type: str = "text/html"
    charset: str = ""
    status: int = 200
    etag: str = ""
    last_modified: str = ""


class FixtureFetcher:
    def __init__(
        self, responses: Mapping[str, FixtureResponse | BaseException]
    ) -> None:
        self.responses = dict(responses)
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.workspaces: list[str] = []

    def fetch(self, target: Target, state: State, workspace: Any) -> FetchResult:
        self.calls[target.target_id] += 1
        self.workspaces.append(str(workspace))
        response = self.responses[target.target_id]
        if isinstance(response, BaseException):
            raise response
        if response.status == 304:
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
    def __init__(self, fail_groups: Sequence[str] = ()) -> None:
        self.fail_groups = set(fail_groups)
        self.messages: list[tuple[str, str, str]] = []

    def send_message(self, notification_group: str, message: str) -> str:
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

    def summarize(self, request: Mapping[str, Any]) -> dict[str, Any]:
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
