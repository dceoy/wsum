"""Tests for deterministic workflow decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - trusted CLI
import sys
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import workflow
from workflow import (
    LocalNotificationStore,
    LocalStoreError,
    WorkflowError,
    change_action,
    notification_step,
    validate_targets,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def _target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "enabled": True,
    }
    target.update(overrides)
    return target


def _event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_id": "example",
        "sha256": "a" * 64,
        "destination": "alerts",
        "message": "changed",
        "signal": "start",
        "notification": None,
        "expected_snapshot_sha256": None,
        "previous_event_id": None,
    }
    payload.update(overrides)
    return payload


def _dict_field(result: Mapping[str, object], field: str) -> dict[str, object]:
    value = result.get(field)
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _record(result: Mapping[str, object]) -> dict[str, object]:
    return _dict_field(result, "notification")


def _local_cas_payload(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "protocol_version": result["protocol_version"],
        "action": result["action"],
        "expected_notification": result["expected_notification"],
        "notification": result["notification"],
        "expected_snapshot_sha256": result["expected_snapshot_sha256"],
        "next_signal": result["next_signal"],
    }


def _run_local_cas(
    directive: Mapping[str, object], runtime_dir: Path
) -> dict[str, object]:
    return workflow.run(
        "local-notification-cas",
        _local_cas_payload(directive),
        runtime_dir=runtime_dir,
    )


def _run_local_state(runtime_dir: Path) -> dict[str, object]:
    return workflow.run(
        "local-notification-state",
        {
            "protocol_version": 3,
            "action": "read_notification_state",
            "target_id": "example",
        },
        runtime_dir=runtime_dir,
    )


def _run_local_release(
    record: Mapping[str, object], expected_status: str, runtime_dir: Path
) -> dict[str, object]:
    payload = {
        "protocol_version": 3,
        "action": "release_target_claim",
        "target_id": record["target_id"],
        "event_id": record["event_id"],
        "expected_status": expected_status,
    }
    return workflow.run("local-notification-release", payload, runtime_dir=runtime_dir)


def _write_candidate(runtime_dir: Path, name: str, content: str) -> tuple[Path, str]:
    candidates_dir = runtime_dir / "candidates"
    candidates_dir.mkdir(exist_ok=True)
    candidate = candidates_dir / name
    candidate.write_text(content)
    return candidate, hashlib.sha256(content.encode()).hexdigest()


def _local_snapshot_payload(
    *,
    target_id: str,
    expected_sha256: str | None,
    candidate_sha256: str,
    claim_event_id: str | None = None,
) -> dict[str, object]:
    return {
        "version": 2,
        "action": "promote_snapshot",
        "target_id": target_id,
        "expected_sha256": expected_sha256,
        "candidate_sha256": candidate_sha256,
        "claim_event_id": claim_event_id,
    }


def _run_local_snapshot_promote(
    payload: Mapping[str, object], candidate: Path, runtime_dir: Path
) -> dict[str, object]:
    return workflow.run(
        "local-snapshot-promote",
        payload,
        runtime_dir=runtime_dir,
        candidate_path=candidate,
    )


def _create_sending_claim(
    runtime_dir: Path,
    sha256: str,
    expected_snapshot_sha256: str | None = None,
    previous_event_id: str | None = None,
) -> dict[str, object]:
    first = notification_step(
        _event_payload(
            sha256=sha256,
            expected_snapshot_sha256=expected_snapshot_sha256,
            previous_event_id=previous_event_id,
        )
    )
    _run_local_cas(first, runtime_dir)
    pending = _record(first)
    claim = notification_step(
        _event_payload(
            notification=pending,
            sha256=sha256,
            signal="pending_persisted",
            expected_snapshot_sha256=expected_snapshot_sha256,
            previous_event_id=previous_event_id,
        )
    )
    return _record(_run_local_cas(claim, runtime_dir))


def _deliver_local_notification(
    runtime_dir: Path,
    previous_content: str,
    current_content: str,
    candidate_name: str,
) -> tuple[dict[str, object], Path, str, str]:
    previous_sha256 = hashlib.sha256(previous_content.encode()).hexdigest()
    state = _run_local_state(runtime_dir)
    previous_event_id = state["previous_event_id"]
    candidate, current_sha256 = _write_candidate(
        runtime_dir, candidate_name, current_content
    )
    start = notification_step(
        _event_payload(
            sha256=current_sha256,
            expected_snapshot_sha256=previous_sha256,
            previous_event_id=previous_event_id,
        )
    )
    pending = _record(_run_local_cas(start, runtime_dir))
    sending_step = notification_step(
        _event_payload(
            notification=pending,
            sha256=current_sha256,
            expected_snapshot_sha256=previous_sha256,
            previous_event_id=previous_event_id,
            signal="pending_persisted",
        )
    )
    sending = _record(_run_local_cas(sending_step, runtime_dir))
    assert (
        notification_step(
            _event_payload(
                notification=sending,
                sha256=current_sha256,
                expected_snapshot_sha256=previous_sha256,
                previous_event_id=previous_event_id,
                signal="sending_claimed",
            )
        )["action"]
        == "send_slack"
    )
    delivered_step = notification_step(
        _event_payload(
            notification=sending,
            sha256=current_sha256,
            expected_snapshot_sha256=previous_sha256,
            previous_event_id=previous_event_id,
            signal="slack_delivered",
        )
    )
    delivered = _record(_run_local_cas(delivered_step, runtime_dir))
    record_path = runtime_dir / "notifications" / f"{delivered['event_id']}.json"
    assert record_path.exists()
    return delivered, candidate, previous_sha256, current_sha256


def test_validate_targets_normalizes_defaults() -> None:
    result = validate_targets({"persistence_mode": "local", "targets": [_target()]})

    assert result["persistence_mode"] == "local"
    assert result["targets"] == [
        {
            "version": 1,
            "target_id": "example",
            "name": "Example",
            "url": "https://example.com/",
            "enabled": True,
            "action": "monitor",
            "watch_focus": "",
            "notification_group": "",
            "fetch_mode": "static",
        }
    ]


@pytest.mark.parametrize(
    ("enabled", "action"),
    [(True, "monitor"), (False, "skip_disabled")],
    ids=["enabled", "disabled"],
)
def test_validate_targets_assigns_target_action(enabled: bool, action: str) -> None:
    result = validate_targets({
        "persistence_mode": "local",
        "targets": [_target(enabled=enabled)],
    })

    targets = result["targets"]
    assert isinstance(targets, list)
    assert isinstance(targets[0], dict)
    assert targets[0]["action"] == action


def test_validate_targets_accepts_benign_query() -> None:
    result = validate_targets({
        "persistence_mode": "local",
        "targets": [_target(url="https://example.com/?page=2")],
    })

    targets = result["targets"]
    assert isinstance(targets, list)
    assert isinstance(targets[0], dict)
    assert targets[0]["url"] == "https://example.com/?page=2"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target_id": "bad/id"}, "invalid_target_id"),
        ({"url": "file:///tmp/a"}, "absolute HTTP"),
        ({"url": "https://user:pass@example.com/"}, "credentials"),
        ({"url": "https://example.com/?token=secret"}, "credentials"),
        (
            {
                "url": "https://example.com/?next=https%3A%2F%2Fexample.com%2F%3Ftoken%3Dsecret"
            },
            "credentials",
        ),
        (
            {
                "url": "/".join([
                    "https://hooks.slack.com",
                    "services",
                    "T00000000",
                    "B00000000",
                    "X" * 24,
                ])
            },
            "credentials",
        ),
        ({"url": "https://example.com/#x"}, "fragment"),
        ({"enabled": "yes"}, "enabled"),
        ({"fetch_mode": "auto"}, "fetch_mode"),
        ({"include_selector": "main"}, "selector_migration_required"),
        ({"exclude_selectors": ["nav"]}, "selector_migration_required"),
    ],
)
def test_validate_targets_rejects_invalid_target(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(WorkflowError, match=message):
        validate_targets({
            "persistence_mode": "google-drive",
            "targets": [_target(**override)],
        })


def test_validate_targets_rejects_duplicates_and_mode() -> None:
    with pytest.raises(WorkflowError, match="duplicate_target_id"):
        validate_targets({
            "persistence_mode": "local",
            "targets": [_target(), _target()],
        })
    with pytest.raises(WorkflowError, match="persistence_mode"):
        validate_targets({"persistence_mode": "mixed", "targets": []})


@pytest.mark.parametrize(
    ("payload", "action"),
    [
        ({"status": "baseline"}, "promote_snapshot"),
        ({"status": "unchanged"}, "discard_candidate"),
        (
            {"status": "changed", "diff_truncated": False, "materiality": None},
            "assess_materiality",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": False},
            "promote_snapshot",
        ),
        (
            {"status": "changed", "diff_truncated": False, "materiality": True},
            "notify",
        ),
        (
            {"status": "changed", "diff_truncated": True, "materiality": False},
            "manual_review",
        ),
    ],
)
def test_change_action(payload: dict[str, object], action: str) -> None:
    assert change_action(payload) == {"action": action}


@pytest.mark.parametrize(
    (
        "sha256",
        "expected_snapshot_sha256",
        "previous_event_id",
        "expected_event_id",
    ),
    [
        (
            "b" * 64,
            "a" * 64,
            None,
            "578795babbda5177e677fbdf039233d4baf2f7b5f6da1ab7a40a3256b2b224af",
        ),
        (
            "b" * 64,
            "a" * 64,
            "c" * 64,
            "a2720743d8ac5321ec8fda688e2147f2ee4612f53d2996acc6403032b1f6f080",
        ),
        (
            "b" * 64,
            None,
            None,
            "ba0b4e936c6896608e2e80d7aaaf185281ec94dbae7530cd88030c84a9c0615d",
        ),
    ],
    ids=[
        "first-baseline-transition",
        "recurring-baseline-transition",
        "initial-baseline",
    ],
)
def test_notification_event_id_scopes_the_baseline_transition(
    sha256: str,
    expected_snapshot_sha256: str | None,
    previous_event_id: str | None,
    expected_event_id: str,
) -> None:
    assert (
        workflow.notification_event_id(
            "example", sha256, expected_snapshot_sha256, previous_event_id
        )
        == expected_event_id
    )


@pytest.mark.parametrize(
    "field",
    ["expected_snapshot_sha256", "previous_event_id"],
    ids=["expected-snapshot", "previous-event"],
)
def test_notification_step_requires_explicit_transition_state(field: str) -> None:
    payload = _event_payload()
    payload.pop(field)

    with pytest.raises(WorkflowError, match=field):
        notification_step(payload)


def test_local_notification_state_starts_without_a_cursor(tmp_path: Path) -> None:
    assert _run_local_state(tmp_path) == {
        "protocol_version": 3,
        "action": "notification_state_read",
        "target_id": "example",
        "previous_event_id": None,
    }


@pytest.mark.parametrize(
    ("cursor", "message"),
    [
        (
            {"version": 2, "target_id": "example", "last_event_id": None},
            "version",
        ),
        (
            {"version": 1, "target_id": "other", "last_event_id": None},
            "target_id",
        ),
        (
            {"version": 1, "target_id": "example", "last_event_id": "invalid"},
            "schema",
        ),
        (
            {
                "version": 1,
                "target_id": "example",
                "last_event_id": None,
                "extra": True,
            },
            "schema",
        ),
    ],
    ids=["wrong-version", "wrong-target", "bad-event", "extra-field"],
)
def test_local_notification_state_rejects_invalid_cursor(
    tmp_path: Path, cursor: dict[str, object], message: str
) -> None:
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    (notifications_dir / ".example.cursor.json").write_text(json.dumps(cursor))

    with pytest.raises(LocalStoreError, match=message):
        _run_local_state(tmp_path)


def test_notification_protocol_success_path() -> None:
    first = notification_step(_event_payload())
    assert first["protocol_version"] == 3
    assert first["action"] == "compare_and_swap"
    assert first["expected_notification"] is None
    assert first["next_signal"] == "pending_persisted"
    pending = _record(first)
    assert pending["status"] == "pending"
    assert pending["attempt"] == 0
    assert pending["previous_event_id"] is None
    assert pending["expected_snapshot_sha256"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", str(pending["event_id"]))

    sending_step = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    assert sending_step["protocol_version"] == 3
    assert sending_step["action"] == "compare_and_swap"
    assert sending_step["expected_notification"] == pending
    sending = _record(sending_step)
    assert sending_step["next_signal"] == "sending_claimed"
    assert sending["status"] == "sending"
    assert sending["attempt"] == 1

    send = notification_step(
        _event_payload(notification=sending, signal="sending_claimed")
    )
    assert send["protocol_version"] == 3
    assert send["action"] == "send_slack"

    delivered_step = notification_step(
        _event_payload(notification=sending, signal="slack_delivered")
    )
    assert delivered_step["action"] == "compare_and_swap"
    assert delivered_step["expected_notification"] == sending
    delivered = _record(delivered_step)
    assert delivered_step["next_signal"] == "delivered_persisted"
    assert delivered["status"] == "delivered"

    done = notification_step(
        _event_payload(notification=delivered, signal="delivered_persisted")
    )
    assert done == {"protocol_version": 3, "action": "promote_snapshot"}


def test_notification_protocol_restart_and_failure_paths() -> None:
    pending = _record(notification_step(_event_payload()))
    resumed = notification_step(_event_payload(notification=pending))
    assert resumed["action"] == "compare_and_swap"
    assert resumed["expected_notification"] == pending
    sending = _record(resumed)
    assert resumed["next_signal"] == "sending_claimed"
    assert sending["status"] == "sending"

    ambiguous = notification_step(_event_payload(notification=sending))
    assert ambiguous == {
        "protocol_version": 3,
        "action": "manual_reconciliation",
    }

    failed = notification_step(
        _event_payload(
            notification=sending,
            signal="slack_failed",
            error="timeout",
        )
    )
    assert failed["action"] == "compare_and_swap"
    assert failed["expected_notification"] == sending
    retry = _record(failed)
    assert failed["next_signal"] == "failure_persisted"
    assert retry["status"] == "pending"
    assert retry["attempt"] == 1
    assert retry["last_error"] == "timeout"
    stopped = notification_step(
        _event_payload(notification=retry, signal="failure_persisted")
    )
    assert stopped == {"protocol_version": 3, "action": "stop"}

    delivered = dict(sending)
    delivered["status"] = "delivered"
    assert notification_step(_event_payload(notification=delivered)) == {
        "protocol_version": 3,
        "action": "promote_snapshot",
    }


def test_local_notification_cas_persists_and_reads_back(tmp_path: Path) -> None:
    first = notification_step(_event_payload())
    applied = _run_local_cas(first, tmp_path)

    assert applied["protocol_version"] == 3
    assert applied["action"] == "compare_and_swap_applied"
    assert applied["next_signal"] == first["next_signal"]
    pending = _record(applied)
    event_id = str(pending["event_id"])
    notifications_dir = tmp_path / "notifications"
    record_path = notifications_dir / f"{event_id}.json"

    assert json.loads(record_path.read_text()) == pending
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(notifications_dir.stat().st_mode) & 0o077 == 0
    assert LocalNotificationStore(tmp_path, "example").read(event_id) == pending

    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    claimed = _run_local_cas(claim, tmp_path)
    assert claimed["action"] == "compare_and_swap_applied"
    assert _record(claimed)["status"] == "sending"
    assert json.loads(record_path.read_text()) == _record(claimed)


def test_local_notification_cas_conflict_returns_current_record(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    first_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_replacement = _record(second_claim)
    second_replacement["updated_at"] = "2000-01-01T00:00:00+00:00"
    second_claim["notification"] = second_replacement

    first_result = _run_local_cas(first_claim, tmp_path)
    assert first_result["action"] == "compare_and_swap_applied"
    winner = _record(first_result)

    conflict = _run_local_cas(second_claim, tmp_path)
    assert conflict == {
        "protocol_version": 3,
        "action": "compare_and_swap_conflict",
        "notification": winner,
    }
    event_id = str(winner["event_id"])
    assert (
        json.loads((tmp_path / "notifications" / f"{event_id}.json").read_text())
        == winner
    )


def test_local_notification_cas_rejects_stale_baseline_before_sending(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    expected_sha256 = hashlib.sha256(baseline.encode()).hexdigest()
    first = notification_step(
        _event_payload(
            sha256="a" * 64,
            expected_snapshot_sha256=expected_sha256,
        )
    )
    pending_result = _run_local_cas(first, tmp_path)
    pending = _record(pending_result)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "newer\n")
    promoted = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=candidate_sha256,
        ),
        candidate,
        tmp_path,
    )
    assert promoted["action"] == "snapshot_promoted"

    sending = notification_step(
        _event_payload(
            notification=pending,
            sha256="a" * 64,
            signal="pending_persisted",
            expected_snapshot_sha256=expected_sha256,
        )
    )

    assert _run_local_cas(sending, tmp_path) == {
        "protocol_version": 3,
        "action": "snapshot_compare_and_swap_conflict",
        "expected_snapshot_sha256": expected_sha256,
        "current_snapshot_sha256": candidate_sha256,
        "notification": pending,
    }
    assert not (tmp_path / "notifications" / ".example.claim.json").exists()


def test_local_notification_cas_rejects_present_record_for_absent_expected(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(
            notification=pending,
            signal="pending_persisted",
        )
    )
    claim["expected_notification"] = None

    assert _run_local_cas(claim, tmp_path) == {
        "protocol_version": 3,
        "action": "compare_and_swap_conflict",
        "notification": pending,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol_version", 1, "protocol_version"),
        ("action", "overwrite", "action"),
        ("next_signal", "sending_persisted", "next_signal"),
    ],
    ids=["wrong-version", "wrong-action", "wrong-signal"],
)
def test_local_notification_cas_rejects_invalid_directive(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    directive = notification_step(_event_payload())
    directive[field] = value

    with pytest.raises(WorkflowError, match=message):
        _run_local_cas(directive, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", True, "version"),
        ("attempt", False, "attempt"),
        ("extra", "value", "exactly"),
    ],
    ids=["boolean-version", "boolean-attempt", "extra-field"],
)
def test_local_notification_cas_rejects_invalid_record(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    directive = notification_step(_event_payload())
    replacement = _record(directive)
    replacement[field] = value
    directive["notification"] = replacement

    with pytest.raises(WorkflowError, match=message):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_unsafe_local_state(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").symlink_to(tmp_path / "outside.json")

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{", "valid JSON"),
        (b"x" * (1024 * 1024 + 1), "too large"),
    ],
    ids=["malformed-json", "oversized-record"],
)
def test_local_notification_cas_rejects_malformed_or_oversized_state(
    tmp_path: Path, content: bytes, message: str
) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").write_bytes(content)

    with pytest.raises(LocalStoreError, match=message):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_invalid_existing_schema(
    tmp_path: Path,
) -> None:
    directive = notification_step(_event_payload())
    record = dict(_record(directive))
    record["target_id"] = "different-target"
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").write_text(json.dumps(record))

    with pytest.raises(LocalStoreError, match="schema"):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_lock_symlink(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("not a lock")
    (notifications_dir / ".example.lock").symlink_to(lock_target)

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_rejects_nonregular_record(tmp_path: Path) -> None:
    directive = notification_step(_event_payload())
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    event_id = str(_record(directive)["event_id"])
    (notifications_dir / f"{event_id}.json").mkdir()

    with pytest.raises(LocalStoreError):
        _run_local_cas(directive, tmp_path)


def test_local_notification_cas_fsyncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    original_fsync = os.fsync
    synced: list[int] = []

    def tracking_fsync(file_descriptor: int) -> None:
        synced.append(file_descriptor)
        original_fsync(file_descriptor)

    monkeypatch.setattr(workflow.os, "fsync", tracking_fsync)
    assert _run_local_cas(claim, tmp_path)["action"] == "compare_and_swap_applied"
    assert len(synced) == 4


def test_local_notification_cas_preserves_old_record_on_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )

    def fail_fsync(_: int) -> None:
        raise OSError

    monkeypatch.setattr(workflow.os, "fsync", fail_fsync)
    with pytest.raises(LocalStoreError, match="temporary notification"):
        _run_local_cas(claim, tmp_path)

    event_id = str(pending["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    assert json.loads(record_path.read_text()) == pending
    assert list(record_path.parent.glob(".*.tmp")) == []


def test_local_notification_cas_reports_directory_fsync_failure_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )

    original_fsync_directory = cast(
        "Callable[[Path], None]",
        LocalNotificationStore._fsync_directory,  # pyright: ignore[reportPrivateUsage]
    )
    calls = 0

    def fail_second_directory_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        original_fsync_directory(path)

    monkeypatch.setattr(
        LocalNotificationStore,
        "_fsync_directory",
        staticmethod(fail_second_directory_fsync),
    )
    with pytest.raises(LocalStoreError, match="durably write"):
        _run_local_cas(claim, tmp_path)

    sending = _record(claim)
    event_id = str(pending["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    assert json.loads(record_path.read_text()) == sending
    assert list(record_path.parent.glob(".*.tmp")) == []
    assert notification_step(_event_payload(notification=sending)) == {
        "protocol_version": 3,
        "action": "manual_reconciliation",
    }


def test_local_notification_cas_serializes_different_events_until_promotion(
    tmp_path: Path,
) -> None:
    delivered_sha256 = hashlib.sha256(b"delivered\n").hexdigest()
    second_sha256 = hashlib.sha256(b"second\n").hexdigest()
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "example.txt").write_text("delivered\n")
    first = notification_step(
        _event_payload(
            sha256=delivered_sha256,
            expected_snapshot_sha256=delivered_sha256,
        )
    )
    _run_local_cas(first, tmp_path)
    sending = _record(
        _run_local_cas(
            notification_step(
                _event_payload(
                    notification=_record(first),
                    sha256=delivered_sha256,
                    expected_snapshot_sha256=delivered_sha256,
                    previous_event_id=None,
                    signal="pending_persisted",
                )
            ),
            tmp_path,
        )
    )
    claim_path = tmp_path / "notifications" / ".example.claim.json"
    assert json.loads(claim_path.read_text()) == {
        "version": 2,
        "target_id": "example",
        "event_id": sending["event_id"],
        "previous_event_id": sending["previous_event_id"],
        "expected_snapshot_sha256": delivered_sha256,
        "sha256": delivered_sha256,
    }

    second = notification_step(
        _event_payload(
            sha256=second_sha256,
            expected_snapshot_sha256=delivered_sha256,
        )
    )
    assert _run_local_cas(second, tmp_path) == {
        "protocol_version": 3,
        "action": "target_claim_conflict",
        "target_claim": {
            "version": 2,
            "target_id": "example",
            "event_id": sending["event_id"],
            "previous_event_id": sending["previous_event_id"],
            "expected_snapshot_sha256": delivered_sha256,
            "sha256": delivered_sha256,
        },
        "notification": None,
    }

    assert claim_path.exists()
    assert _run_local_cas(second, tmp_path)["action"] == "target_claim_conflict"

    assert _run_local_release(
        _record(
            _run_local_cas(
                notification_step(
                    _event_payload(
                        notification=sending,
                        sha256=delivered_sha256,
                        expected_snapshot_sha256=delivered_sha256,
                        signal="slack_delivered",
                    )
                ),
                tmp_path,
            )
        ),
        "delivered",
        tmp_path,
    ) == {
        "protocol_version": 3,
        "action": "target_claim_released",
        "target_id": "example",
        "event_id": sending["event_id"],
    }
    assert not claim_path.exists()
    assert _run_local_cas(second, tmp_path)["action"] == "notification_cursor_conflict"
    state = _run_local_state(tmp_path)
    second = notification_step(
        _event_payload(
            sha256=second_sha256,
            expected_snapshot_sha256=delivered_sha256,
            previous_event_id=state["previous_event_id"],
        )
    )
    second_result = _run_local_cas(second, tmp_path)
    assert second_result["action"] == "compare_and_swap_applied"
    second_claim = notification_step(
        _event_payload(
            sha256=second_sha256,
            expected_snapshot_sha256=delivered_sha256,
            notification=_record(second_result),
            previous_event_id=state["previous_event_id"],
            signal="pending_persisted",
        )
    )
    assert _run_local_cas(second_claim, tmp_path)["action"] == (
        "compare_and_swap_applied"
    )


def test_local_notification_cas_backfills_legacy_sending_claim(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    sending = _record(
        notification_step(
            _event_payload(notification=pending, signal="pending_persisted")
        )
    )
    event_id = str(sending["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    record_path.write_text(json.dumps(sending))

    second = notification_step(_event_payload(sha256="b" * 64))
    blocked = _run_local_cas(second, tmp_path)
    assert blocked["action"] == "target_claim_conflict"
    assert blocked["target_claim"] == {
        "version": 2,
        "target_id": "example",
        "event_id": event_id,
        "previous_event_id": sending["previous_event_id"],
        "expected_snapshot_sha256": None,
        "sha256": "a" * 64,
    }
    assert (
        json.loads((tmp_path / "notifications" / ".example.claim.json").read_text())
        == blocked["target_claim"]
    )


def test_local_notification_release_rejects_in_flight_claim(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    _run_local_cas(claim, tmp_path)
    sending = _record(claim)

    with pytest.raises(LocalStoreError, match="not ready to release"):
        _run_local_release(sending, "delivered", tmp_path)


def test_local_notification_release_requires_durable_snapshot_promotion(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    delivered, candidate, expected_sha256, candidate_sha256 = (
        _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    )
    event_id = str(delivered["event_id"])
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    claim_path = tmp_path / "notifications" / ".example.claim.json"

    with pytest.raises(LocalStoreError, match="durably promoted"):
        _run_local_release(delivered, "delivered", tmp_path)
    assert record_path.exists()
    assert claim_path.exists()

    promotion = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=candidate_sha256,
            claim_event_id=event_id,
        ),
        candidate,
        tmp_path,
    )
    assert promotion["action"] == "snapshot_promoted"
    assert _run_local_release(delivered, "delivered", tmp_path)["action"] == (
        "target_claim_released"
    )
    assert not record_path.exists()
    assert not claim_path.exists()


def test_local_notification_state_surfaces_delivered_claim_after_promotion(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    delivered, candidate, expected_sha256, candidate_sha256 = (
        _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    )
    event_id = str(delivered["event_id"])

    assert (
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=expected_sha256,
                candidate_sha256=candidate_sha256,
                claim_event_id=event_id,
            ),
            candidate,
            tmp_path,
        )["action"]
        == "snapshot_promoted"
    )

    state = _run_local_state(tmp_path)
    assert state == {
        "protocol_version": 3,
        "action": "notification_state_recovery",
        "target_id": "example",
        "previous_event_id": None,
        "recovery_action": "release_target_claim",
        "target_claim": {
            "version": 2,
            "target_id": "example",
            "event_id": event_id,
            "previous_event_id": None,
            "expected_snapshot_sha256": expected_sha256,
            "sha256": candidate_sha256,
        },
        "notification": delivered,
    }

    assert _run_local_release(delivered, "delivered", tmp_path)["action"] == (
        "target_claim_released"
    )
    assert _run_local_state(tmp_path) == {
        "protocol_version": 3,
        "action": "notification_state_read",
        "target_id": "example",
        "previous_event_id": event_id,
    }
    assert change_action({"status": "unchanged"}) == {"action": "discard_candidate"}

    next_delivered, _, _, _ = _deliver_local_notification(
        tmp_path, "after\n", "newer\n", "newer.txt"
    )
    assert next_delivered["previous_event_id"] == event_id


def test_local_notification_state_surfaces_failed_pending_claim(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    sending = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    sending_record = _record(_run_local_cas(sending, tmp_path))
    failed = notification_step(
        _event_payload(
            notification=sending_record,
            signal="slack_failed",
            error="timeout",
        )
    )
    failed_record = _record(_run_local_cas(failed, tmp_path))

    state = _run_local_state(tmp_path)
    assert state["action"] == "notification_state_recovery"
    assert state["recovery_action"] == "release_target_claim"
    assert state["target_claim"] == json.loads(
        (tmp_path / "notifications" / ".example.claim.json").read_text()
    )
    assert state["notification"] == failed_record
    assert failed_record["status"] == "pending"

    assert _run_local_release(failed_record, "pending", tmp_path)["action"] == (
        "target_claim_released"
    )
    assert _run_local_state(tmp_path)["action"] == "notification_state_read"
    next_start = notification_step(_event_payload(sha256="b" * 64))
    assert _run_local_cas(next_start, tmp_path)["action"] == (
        "compare_and_swap_applied"
    )


def test_local_notification_state_requires_manual_reconciliation_for_sending_claim(
    tmp_path: Path,
) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    sending = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    sending_record = _record(_run_local_cas(sending, tmp_path))
    event_id = str(sending_record["event_id"])
    claim_path = tmp_path / "notifications" / ".example.claim.json"
    record_path = tmp_path / "notifications" / f"{event_id}.json"
    claim_bytes = claim_path.read_bytes()
    record_bytes = record_path.read_bytes()

    state = _run_local_state(tmp_path)
    assert state == {
        "protocol_version": 3,
        "action": "notification_state_recovery",
        "target_id": "example",
        "previous_event_id": None,
        "recovery_action": "manual_reconciliation",
        "target_claim": json.loads(claim_bytes),
        "notification": sending_record,
    }
    assert claim_path.read_bytes() == claim_bytes
    assert record_path.read_bytes() == record_bytes


def test_local_notification_state_requests_unpromoted_delivery(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    delivered, candidate, expected_sha256, candidate_sha256 = (
        _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    )

    state = _run_local_state(tmp_path)
    assert state["action"] == "notification_state_recovery"
    assert state["recovery_action"] == "promote_snapshot"
    assert state["notification"] == delivered

    event_id = str(delivered["event_id"])
    assert (
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=expected_sha256,
                candidate_sha256=candidate_sha256,
                claim_event_id=event_id,
            ),
            candidate,
            tmp_path,
        )["action"]
        == "snapshot_promoted"
    )
    assert _run_local_state(tmp_path)["recovery_action"] == ("release_target_claim")


def test_local_notification_state_rejects_inconsistent_delivered_claim(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    (snapshots_dir / "example.txt").write_text("unexpected\n")

    with pytest.raises(LocalStoreError, match="inconsistent"):
        _run_local_state(tmp_path)


def test_local_snapshot_promote_accepts_readme_cwd_relative_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    snapshots_dir = runtime_dir / "snapshots"
    snapshots_dir.mkdir()
    candidate, candidate_sha256 = _write_candidate(
        runtime_dir, "candidate.txt", "next\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                _local_snapshot_payload(
                    target_id="example",
                    expected_sha256=None,
                    candidate_sha256=candidate_sha256,
                )
            )
        ),
    )

    assert (
        workflow.main([
            "local-snapshot-promote",
            "--runtime-dir",
            ".runtime",
            "--candidate",
            ".runtime/candidates/candidate.txt",
        ])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "action": "snapshot_promoted",
        "candidate_sha256": candidate_sha256,
        "previous_sha256": None,
        "target_id": "example",
        "version": 2,
    }
    assert candidate == runtime_dir / "candidates" / "candidate.txt"


def test_local_snapshot_promote_rejects_cwd_relative_candidate_outside_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / ".runtime"
    (runtime_dir / "candidates").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("next\n")
    candidate_sha256 = hashlib.sha256(b"next\n").hexdigest()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(LocalStoreError, match="inside runtime_dir"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=None,
                candidate_sha256=candidate_sha256,
            ),
            Path("outside.txt"),
            runtime_dir,
        )


def test_local_notification_recurring_transition_sends_after_retirement(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "example.txt").write_text("A\n")
    transitions = [
        ("A\n", "B\n", "a-to-b-first.txt"),
        ("B\n", "A\n", "b-to-a.txt"),
        ("A\n", "B\n", "a-to-b-again.txt"),
    ]
    event_ids: list[str] = []
    send_actions: list[str] = []

    for previous_content, current_content, candidate_name in transitions:
        delivered, candidate, expected_sha256, candidate_sha256 = (
            _deliver_local_notification(
                tmp_path, previous_content, current_content, candidate_name
            )
        )
        event_id = str(delivered["event_id"])
        event_ids.append(event_id)
        send_actions.append("send_slack")

        retry = notification_step(
            _event_payload(
                notification=delivered,
                sha256=candidate_sha256,
                expected_snapshot_sha256=expected_sha256,
                previous_event_id=delivered["previous_event_id"],
            )
        )
        assert retry == {"protocol_version": 3, "action": "promote_snapshot"}

        promotion = _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=expected_sha256,
                candidate_sha256=candidate_sha256,
                claim_event_id=event_id,
            ),
            candidate,
            tmp_path,
        )
        assert promotion["action"] == "snapshot_promoted"
        assert _run_local_release(delivered, "delivered", tmp_path)["action"] == (
            "target_claim_released"
        )
        assert not (tmp_path / "notifications" / f"{event_id}.json").exists()

    assert send_actions == ["send_slack", "send_slack", "send_slack"]
    assert len(set(event_ids)) == 3
    assert _run_local_state(tmp_path)["previous_event_id"] == event_ids[-1]
    assert json.loads(
        (tmp_path / "notifications" / ".example.cursor.json").read_text()
    ) == {
        "version": 1,
        "target_id": "example",
        "last_event_id": event_ids[-1],
    }
    assert (snapshots_dir / "example.txt").read_text() == "B\n"


def test_local_notification_cas_rejects_stale_recreated_transition(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    delivered, candidate, expected_sha256, candidate_sha256 = (
        _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    )
    event_id = str(delivered["event_id"])
    promotion = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=candidate_sha256,
            claim_event_id=event_id,
        ),
        candidate,
        tmp_path,
    )
    assert promotion["action"] == "snapshot_promoted"
    assert _run_local_release(delivered, "delivered", tmp_path)["action"] == (
        "target_claim_released"
    )

    stale = notification_step(
        _event_payload(
            sha256=candidate_sha256,
            expected_snapshot_sha256=expected_sha256,
            previous_event_id=delivered["previous_event_id"],
        )
    )
    assert _run_local_cas(stale, tmp_path) == {
        "protocol_version": 3,
        "action": "notification_cursor_conflict",
        "previous_event_id": delivered["event_id"],
        "notification": None,
    }


def test_local_notification_cas_rejects_legacy_record(
    tmp_path: Path,
) -> None:
    directive = notification_step(_event_payload())
    legacy = dict(_record(directive))
    legacy.pop("previous_event_id")
    legacy.pop("expected_snapshot_sha256")
    legacy["version"] = 1
    legacy["event_id"] = hashlib.sha256(f"example\0{'a' * 64}".encode()).hexdigest()
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    (notifications_dir / f"{legacy['event_id']}.json").write_text(json.dumps(legacy))

    with pytest.raises(LocalStoreError, match="legacy"):
        _run_local_cas(directive, tmp_path)


def test_local_notification_release_recovers_after_cursor_advance_and_retirement(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "before\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    delivered, candidate, expected_sha256, candidate_sha256 = (
        _deliver_local_notification(tmp_path, baseline, "after\n", "after.txt")
    )
    event_id = str(delivered["event_id"])
    _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=candidate_sha256,
            claim_event_id=event_id,
        ),
        candidate,
        tmp_path,
    )

    store = LocalNotificationStore(tmp_path, "example")
    store._advance_cursor(  # pyright: ignore[reportPrivateUsage]
        cast("str | None", delivered["previous_event_id"]), event_id
    )
    store._retire_record(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "notifications" / f"{event_id}.json"
    )

    assert _run_local_state(tmp_path) == {
        "protocol_version": 3,
        "action": "notification_state_recovery",
        "target_id": "example",
        "previous_event_id": event_id,
        "recovery_action": "release_target_claim",
        "target_claim": json.loads(
            (tmp_path / "notifications" / ".example.claim.json").read_text()
        ),
        "notification": None,
    }
    assert _run_local_release(delivered, "delivered", tmp_path)["action"] == (
        "target_claim_released"
    )
    assert _run_local_state(tmp_path)["previous_event_id"] == event_id


@pytest.mark.parametrize(
    "baseline",
    [None, "previous\n"],
    ids=["new-baseline", "non-material-change"],
)
def test_local_snapshot_promote_uses_expected_baseline_cas(
    tmp_path: Path, baseline: str | None
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    if baseline is not None:
        (snapshots_dir / "example.txt").write_text(baseline)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    expected_sha256 = (
        None if baseline is None else hashlib.sha256(baseline.encode()).hexdigest()
    )

    result = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=candidate_sha256,
        ),
        candidate,
        tmp_path,
    )

    assert result == {
        "version": 2,
        "action": "snapshot_promoted",
        "target_id": "example",
        "previous_sha256": expected_sha256,
        "candidate_sha256": candidate_sha256,
    }
    assert (snapshots_dir / "example.txt").read_text() == "next\n"


def test_local_snapshot_promote_blocks_active_claim(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    sending = _create_sending_claim(
        tmp_path,
        "a" * 64,
        hashlib.sha256(baseline.encode()).hexdigest(),
    )
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")

    result = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
            candidate_sha256=candidate_sha256,
        ),
        candidate,
        tmp_path,
    )

    assert result == {
        "version": 2,
        "action": "target_claim_conflict",
        "current_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
        "target_claim": {
            "version": 2,
            "target_id": "example",
            "event_id": sending["event_id"],
            "previous_event_id": sending["previous_event_id"],
            "expected_snapshot_sha256": hashlib.sha256(b"previous\n").hexdigest(),
            "sha256": "a" * 64,
        },
    }
    assert (snapshots_dir / "example.txt").read_text() == baseline


def test_local_snapshot_promote_requires_delivered_claim(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "example.txt").write_text("previous\n")
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    sending = _create_sending_claim(
        tmp_path,
        candidate_sha256,
        hashlib.sha256(b"previous\n").hexdigest(),
    )

    with pytest.raises(LocalStoreError, match="not delivered"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=hashlib.sha256(b"previous\n").hexdigest(),
                candidate_sha256=candidate_sha256,
                claim_event_id=str(sending["event_id"]),
            ),
            candidate,
            tmp_path,
        )


def test_local_snapshot_promote_accepts_delivered_claim_until_release(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    sending = _create_sending_claim(
        tmp_path,
        candidate_sha256,
        hashlib.sha256(baseline.encode()).hexdigest(),
    )
    delivered = notification_step(
        _event_payload(
            notification=sending,
            sha256=candidate_sha256,
            expected_snapshot_sha256=hashlib.sha256(b"previous\n").hexdigest(),
            signal="slack_delivered",
        )
    )
    delivered_result = _run_local_cas(delivered, tmp_path)
    delivered_record = _record(delivered_result)

    result = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
            candidate_sha256=candidate_sha256,
            claim_event_id=str(delivered_record["event_id"]),
        ),
        candidate,
        tmp_path,
    )

    assert result["action"] == "snapshot_promoted"
    assert (snapshots_dir / "example.txt").read_text() == "next\n"
    assert (tmp_path / "notifications" / ".example.claim.json").exists()
    assert _run_local_release(delivered_record, "delivered", tmp_path)["action"] == (
        "target_claim_released"
    )


def test_local_snapshot_promote_rejects_stale_candidate_after_newer_promotion(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    newer, newer_sha256 = _write_candidate(tmp_path, "newer.txt", "newer\n")
    older, older_sha256 = _write_candidate(tmp_path, "older.txt", "older\n")
    expected_sha256 = hashlib.sha256(baseline.encode()).hexdigest()

    first = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=newer_sha256,
        ),
        newer,
        tmp_path,
    )
    second = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=expected_sha256,
            candidate_sha256=older_sha256,
        ),
        older,
        tmp_path,
    )

    assert first["action"] == "snapshot_promoted"
    assert second == {
        "version": 2,
        "action": "snapshot_compare_and_swap_conflict",
        "expected_sha256": expected_sha256,
        "current_sha256": newer_sha256,
    }
    assert (snapshots_dir / "example.txt").read_text() == "newer\n"


def test_local_snapshot_promote_reports_already_promoted_snapshot(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "same\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", baseline)

    result = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(b"previous\n").hexdigest(),
            candidate_sha256=candidate_sha256,
        ),
        candidate,
        tmp_path,
    )

    assert result == {
        "version": 2,
        "action": "snapshot_already_promoted",
        "target_id": "example",
        "previous_sha256": candidate_sha256,
        "candidate_sha256": candidate_sha256,
    }


def test_local_snapshot_promote_rejects_missing_claim_for_delivered_path(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "example.txt").write_text("previous\n")
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")

    result = _run_local_snapshot_promote(
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(b"previous\n").hexdigest(),
            candidate_sha256=candidate_sha256,
            claim_event_id=workflow.notification_event_id(
                "example",
                candidate_sha256,
                hashlib.sha256(b"previous\n").hexdigest(),
                None,
            ),
        ),
        candidate,
        tmp_path,
    )

    assert result == {
        "version": 2,
        "action": "target_claim_conflict",
        "current_sha256": hashlib.sha256(b"previous\n").hexdigest(),
        "target_claim": None,
    }
    assert (snapshots_dir / "example.txt").read_text() == "previous\n"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 1, "version"),
        ("action", "copy_snapshot", "action"),
        ("expected_sha256", "invalid", "expected_sha256"),
        ("candidate_sha256", "invalid", "candidate_sha256"),
        ("claim_event_id", "invalid", "claim_event_id"),
    ],
    ids=["wrong-version", "wrong-action", "bad-expected", "bad-candidate", "bad-claim"],
)
def test_local_snapshot_promote_rejects_invalid_request(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    payload = _local_snapshot_payload(
        target_id="example",
        expected_sha256=None,
        candidate_sha256=candidate_sha256,
    )
    payload[field] = value

    with pytest.raises(WorkflowError, match=message):
        _run_local_snapshot_promote(payload, candidate, tmp_path)


def test_local_snapshot_promote_rejects_unsafe_candidate(tmp_path: Path) -> None:
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("next\n")
    candidate = candidates_dir / "candidate.txt"
    candidate.symlink_to(outside)
    candidate_sha256 = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(LocalStoreError, match="must not be a symlink"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=None,
                candidate_sha256=candidate_sha256,
            ),
            candidate,
            tmp_path,
        )


def test_local_snapshot_promote_rejects_nonregular_candidate(tmp_path: Path) -> None:
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate = candidates_dir / "candidate.txt"
    candidate.mkdir()

    with pytest.raises(LocalStoreError, match="regular file"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=None,
                candidate_sha256="a" * 64,
            ),
            candidate,
            tmp_path,
        )


def test_local_snapshot_promote_rejects_invalid_snapshot_encoding(
    tmp_path: Path,
) -> None:
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "example.txt").write_bytes(b"\xff")

    with pytest.raises(LocalStoreError, match="UTF-8"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=hashlib.sha256(b"\xff").hexdigest(),
                candidate_sha256=candidate_sha256,
            ),
            candidate,
            tmp_path,
        )


def test_local_snapshot_promote_rejects_candidate_hash_mismatch(
    tmp_path: Path,
) -> None:
    candidate, _ = _write_candidate(tmp_path, "candidate.txt", "next\n")

    with pytest.raises(LocalStoreError, match="hash mismatch"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=None,
                candidate_sha256="0" * 64,
            ),
            candidate,
            tmp_path,
        )


def test_local_snapshot_promote_preserves_baseline_on_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    notifications_dir = tmp_path / "notifications"
    notifications_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")

    def fail_fsync(_: int) -> None:
        raise OSError

    monkeypatch.setattr(workflow.os, "fsync", fail_fsync)
    with pytest.raises(LocalStoreError, match="temporary snapshot"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
                candidate_sha256=candidate_sha256,
            ),
            candidate,
            tmp_path,
        )

    assert (snapshots_dir / "example.txt").read_text() == baseline


def test_local_snapshot_promote_keeps_claim_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    candidate, candidate_sha256 = _write_candidate(tmp_path, "candidate.txt", "next\n")
    sending = _create_sending_claim(
        tmp_path,
        candidate_sha256,
        hashlib.sha256(baseline.encode()).hexdigest(),
    )
    delivered = notification_step(
        _event_payload(
            notification=sending,
            sha256=candidate_sha256,
            expected_snapshot_sha256=hashlib.sha256(b"previous\n").hexdigest(),
            signal="slack_delivered",
        )
    )
    delivered_result = _run_local_cas(delivered, tmp_path)
    delivered_record = _record(delivered_result)

    def fail_directory_fsync(_: Path) -> None:
        raise OSError

    monkeypatch.setattr(
        LocalNotificationStore,
        "_fsync_directory",
        staticmethod(fail_directory_fsync),
    )
    with pytest.raises(LocalStoreError, match="durably write snapshot"):
        _run_local_snapshot_promote(
            _local_snapshot_payload(
                target_id="example",
                expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
                candidate_sha256=candidate_sha256,
                claim_event_id=str(delivered_record["event_id"]),
            ),
            candidate,
            tmp_path,
        )

    assert (snapshots_dir / "example.txt").read_text() == "next\n"
    assert (tmp_path / "notifications" / ".example.claim.json").exists()


def test_local_snapshot_promote_serializes_same_baseline_processes(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    baseline = "previous\n"
    (snapshots_dir / "example.txt").write_text(baseline)
    first_candidate, first_sha256 = _write_candidate(tmp_path, "first.txt", "first\n")
    second_candidate, second_sha256 = _write_candidate(
        tmp_path, "second.txt", "second\n"
    )
    payloads = [
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
            candidate_sha256=first_sha256,
        ),
        _local_snapshot_payload(
            target_id="example",
            expected_sha256=hashlib.sha256(baseline.encode()).hexdigest(),
            candidate_sha256=second_sha256,
        ),
    ]
    command = [
        sys.executable,
        str(Path(workflow.__file__).resolve()),
        "local-snapshot-promote",
        "--runtime-dir",
        str(tmp_path),
    ]
    processes = [
        subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            [*command, "--candidate", str(candidate)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
        )
        for candidate in (first_candidate, second_candidate)
    ]
    outputs = [
        process.communicate(json.dumps(payload), timeout=10)
        for process, payload in zip(processes, payloads, strict=True)
    ]

    assert [process.returncode for process in processes] == [0, 0]
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert {result["action"] for result in results} == {
        "snapshot_promoted",
        "snapshot_compare_and_swap_conflict",
    }
    winner = next(
        result for result in results if result["action"] == "snapshot_promoted"
    )
    winner_body = (
        "first\n" if winner["candidate_sha256"] == first_sha256 else "second\n"
    )
    assert (snapshots_dir / "example.txt").read_text() == winner_body


def test_local_notification_cas_serializes_separate_processes(tmp_path: Path) -> None:
    first = notification_step(_event_payload())
    _run_local_cas(first, tmp_path)
    pending = _record(first)
    first_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_claim = notification_step(
        _event_payload(notification=pending, signal="pending_persisted")
    )
    second_replacement = _record(second_claim)
    second_replacement["updated_at"] = "2000-01-01T00:00:00+00:00"
    second_claim["notification"] = second_replacement

    command = [
        sys.executable,
        str(Path(workflow.__file__).resolve()),
        "local-notification-cas",
        "--runtime-dir",
        str(tmp_path),
    ]
    processes = [
        subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [
        process.communicate(json.dumps(_local_cas_payload(directive)), timeout=10)
        for process, directive in zip(
            processes, (first_claim, second_claim), strict=True
        )
    ]
    assert [process.returncode for process in processes] == [0, 0]
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert {result["action"] for result in results} == {
        "compare_and_swap_applied",
        "compare_and_swap_conflict",
    }
    winner = next(
        result for result in results if result["action"] == "compare_and_swap_applied"
    )
    event_id = str(pending["event_id"])
    assert (
        json.loads((tmp_path / "notifications" / f"{event_id}.json").read_text())
        == winner["notification"]
    )


def test_notification_protocol_rejects_wrong_state_or_event() -> None:
    pending = _record(notification_step(_event_payload()))
    with pytest.raises(WorkflowError, match="sending notification"):
        notification_step(
            _event_payload(notification=pending, signal="slack_delivered")
        )

    sending = _record(
        notification_step(
            _event_payload(notification=pending, signal="pending_persisted")
        )
    )
    with pytest.raises(WorkflowError, match="signal is invalid"):
        notification_step(
            _event_payload(notification=sending, signal="sending_persisted")
        )

    bad = dict(pending)
    bad["sha256"] = "b" * 64
    with pytest.raises(WorkflowError, match="sha256"):
        notification_step(_event_payload(notification=bad))

    with pytest.raises(WorkflowError, match="lowercase hexadecimal"):
        notification_step(_event_payload(sha256="A" * 64))


def test_run_rejects_unknown_operation() -> None:
    with pytest.raises(WorkflowError, match="operation"):
        workflow.run("unknown", {})
