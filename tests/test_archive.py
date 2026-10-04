"""Tests for the optional durable evidence archive in the core finalizer."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import workspace
from workspace import WorkspaceError, check, discard_pending, finalize

if TYPE_CHECKING:
    from collections.abc import Callable

_RUN_ID = "20261001T000000Z-deadbeef"
_URL = "https://vendor.example/updates"
_HEADER = "name,url,publisher,category,keywords,criteria,priority,enabled\n"
_REPORT = "## Vendor\n\nPlans changed.\n"
_DIFF = "--- previous\n+++ current\n-old\n+new"
_DELETE = object()


class _Review:
    def __init__(self, root: Path, target_id: str, revision: str) -> None:
        self.root = root
        self.state = root / ".wsum"
        self.target_id = target_id
        self.revision = revision
        self.ingestion_id = workspace._ingestion_id(target_id, revision)
        self.bundle = root / "evidence" / self.ingestion_id
        self.snapshot = self.state / "snapshots" / f"{target_id}.txt"
        self.intent = self.state / ".pending-recovery" / f"{target_id}.json"
        self.pending = self.state / "pending" / target_id

    def decision(
        self, *, material: bool = True, report: str = _REPORT
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "target_id": self.target_id,
            "revision": self.revision,
            "material": material,
        }
        if material:
            payload["report"] = report
        return payload

    def finalize(self, **changes: object) -> dict[str, object]:
        return finalize(
            self.root, self.decision(**cast("Any", changes)), archive_evidence=True
        )


def _targets(root: Path, rows: str | None = None) -> None:
    root.joinpath("targets.csv").write_text(
        _HEADER
        + (
            rows
            or f"Plans,{_URL},Vendor,Product,plans,Limits,1,true\n"
            f"API,{_URL},Vendor,Product,API,Breaking,2,true\n"
            f"Off,{_URL},,,,,3,false\n"
        ),
        encoding="utf-8",
    )


def _review(
    root: Path,
    *,
    link_review: dict[str, object] | None = None,
    diff: str = _DIFF,
    truncated: bool = False,
) -> _Review:
    _targets(root)
    target = workspace.load_targets(root)[0]
    target_id = str(target["target_id"])
    snapshots = root / ".wsum" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / f"{target_id}.txt").write_text("old\n", encoding="utf-8")
    result: dict[str, object] = {
        "status": "changed",
        "sha256": hashlib.sha256(b"new\n").hexdigest(),
        "previous_sha256": hashlib.sha256(b"old\n").hexdigest(),
        "diff": diff,
        "diff_truncated": truncated,
    }
    if link_review is not None:
        result["link_review"] = link_review
    review = workspace._handle_monitor_result(
        root / ".wsum", target, result, _RUN_ID, candidate_data=b"new\n"
    )
    return _Review(root, target_id, str(review["revision"]))


def _fail_once(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    when: Callable[..., bool] | None = None,
) -> None:
    original = getattr(workspace, name)
    state = {"armed": True}

    def wrapper(*args: object, **kwargs: object) -> object:
        if state["armed"] and (when is None or when(*args, **kwargs)):
            state["armed"] = False
            message = f"injected {name} failure"
            raise WorkspaceError(message)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace, name, wrapper)


def _files(bundle: Path) -> set[str]:
    return {item.name for item in bundle.iterdir()}


def _write_intent(review: _Review, **changes: object) -> None:
    record = json.loads(review.intent.read_text())
    record.update(changes)
    review.intent.write_text(json.dumps(record), encoding="utf-8")
    review.intent.chmod(0o600)


def _prepare_blocked(review: _Review, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave a prepared intent by failing the first snapshot promotion."""
    _fail_once(monkeypatch, "_promote_snapshot")
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert review.intent.exists()


def _conflicting_promotion(_state: Path, **_options: object) -> dict[str, object]:
    return {"action": "snapshot_conflict"}


def _other_bytes(_self: Path) -> bytes:
    return b"other"


def _grown_bytes(_self: Path) -> bytes:
    return b"abcdef"


def _is_receipt_install(_path: Path, _data: bytes, description: str) -> bool:
    return description == "evidence receipt"


def _is_diff_install(path: Path, _data: bytes, _description: str) -> bool:
    return path.name == "diff.txt"


def _is_cleanup_record(_state: Path, record: dict[str, object]) -> bool:
    return record["kind"] == "cleanup"


def _unchanged(
    _state: Path, target: dict[str, object], _run: str, **_options: object
) -> dict[str, object]:
    return {
        "action": "unchanged",
        "target_id": target["target_id"],
        "name": target["name"],
    }


def _tamper(bundle: Path, file: str, path: tuple[str, ...], value: object) -> None:
    """Edit one JSON field; metadata edits keep the receipt digest consistent."""
    target = bundle / file
    record = json.loads(target.read_text())
    node = record
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    data = json.dumps(record)
    target.write_text(data)
    if file == "metadata.json":
        receipt = json.loads((bundle / "committed.json").read_text())
        receipt["manifest_sha256"] = hashlib.sha256(data.encode()).hexdigest()
        (bundle / "committed.json").write_text(json.dumps(receipt))


def test_archive_finalize_commits_bundle(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        link_review={
            "documents": [
                {
                    "url": "https://vendor.example/a",
                    "source_url": "https://vendor.example/a",
                    "text": "child\ntext\n",
                    "truncated": True,
                },
                {"url": "https://vendor.example/b", "error": "boom"},
            ],
            "omitted": 3,
            "incomplete": True,
        },
        truncated=False,
    )

    result = review.finalize()

    assert result == {
        "action": "finalized",
        "evidence_path": str(review.bundle),
        "ingestion_id": review.ingestion_id,
        "material": True,
        "report_path": str(tmp_path / "reports" / f"{_RUN_ID}.md"),
        "target_id": review.target_id,
    }
    assert _files(review.bundle) == {
        "committed.json",
        "diff.txt",
        "links.json",
        "metadata.json",
        "parent.txt",
    }
    assert (review.bundle / "parent.txt").read_text() == "new\n"
    assert (review.bundle / "diff.txt").read_text() == _DIFF
    links = json.loads((review.bundle / "links.json").read_text())
    assert links["available"] is True
    assert links["incomplete"] is True
    assert links["omitted"] == 3
    assert [item["entry_id"] for item in links["documents"]] == ["link-1", "link-2"]
    metadata = json.loads((review.bundle / "metadata.json").read_text())
    assert metadata["url"] == _URL
    assert [item["name"] for item in metadata["interests"]] == ["Plans", "API"]
    assert metadata["diff_truncated"] is True
    assert metadata["provenance"]["source_fetched_at"] is None
    assert metadata["payloads"]["links.json"]["incomplete"] is True
    receipt = json.loads((review.bundle / "committed.json").read_text())
    assert receipt["ingestion_id"] == review.ingestion_id
    assert receipt["material"] is True
    assert review.snapshot.read_text() == "new\n"
    assert "Plans changed." in (tmp_path / "reports" / f"{_RUN_ID}.md").read_text()
    assert not review.pending.exists()
    assert not review.intent.exists()
    assert workspace._read_receipt(tmp_path, review.ingestion_id) is not None


def test_archive_records_unavailable_links(tmp_path: Path) -> None:
    review = _review(tmp_path)

    review.finalize()

    links = json.loads((review.bundle / "links.json").read_text())
    assert links["available"] is False
    assert links["documents"] == []
    metadata = json.loads((review.bundle / "metadata.json").read_text())
    assert metadata["payloads"]["links.json"]["available"] is False


def test_ordinary_finalize_is_unchanged(tmp_path: Path) -> None:
    review = _review(tmp_path)

    result = finalize(tmp_path, review.decision())

    assert result == {
        "action": "finalized",
        "material": True,
        "report_path": str(tmp_path / "reports" / f"{_RUN_ID}.md"),
        "target_id": review.target_id,
    }
    assert not (tmp_path / "evidence").exists()


def test_archive_flag_skips_non_material_decisions(tmp_path: Path) -> None:
    review = _review(tmp_path)

    result = review.finalize(material=False)

    assert result == {
        "action": "finalized",
        "material": False,
        "target_id": review.target_id,
    }
    assert not (tmp_path / "evidence").exists()
    assert review.snapshot.read_text() == "new\n"


def test_archive_skips_manual_review_for_truncated_non_material(
    tmp_path: Path,
) -> None:
    review = _review(tmp_path, truncated=True)

    assert review.finalize(material=False) == {
        "action": "manual_review_required",
        "target_id": review.target_id,
    }
    assert not (tmp_path / "evidence").exists()


def test_crash_before_promotion_is_not_consumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)

    assert review.snapshot.read_text() == "old\n"
    assert workspace._read_receipt(tmp_path, review.ingestion_id) is None
    assert not (review.bundle / "committed.json").exists()
    assert review.pending.exists()


def test_recovery_after_crash_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)

    # The retry omits the archive option; the obligation must still be honored.
    result = finalize(tmp_path, review.decision())

    assert result["ingestion_id"] == review.ingestion_id
    assert review.snapshot.read_text() == "new\n"
    assert (review.bundle / "committed.json").exists()
    assert not review.intent.exists()
    assert not review.pending.exists()


def test_recovery_after_promotion_before_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(monkeypatch, "_write_report")
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert review.snapshot.read_text() == "new\n"
    assert not (review.bundle / "committed.json").exists()

    result = review.finalize()

    assert result["ingestion_id"] == review.ingestion_id
    assert (review.bundle / "committed.json").exists()
    report = (tmp_path / "reports" / f"{_RUN_ID}.md").read_text()
    assert report.count("Plans changed.") == 1


def test_recovery_after_report_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(
        monkeypatch,
        "_install_evidence_file",
        when=_is_receipt_install,
    )
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert not (review.bundle / "committed.json").exists()
    assert workspace._read_receipt(tmp_path, review.ingestion_id) is None

    result = review.finalize()

    assert result["ingestion_id"] == review.ingestion_id
    assert (tmp_path / "reports" / f"{_RUN_ID}.md").read_text().count("Plans") == 1


def test_recovery_after_receipt_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(monkeypatch, "_complete_cleanup_record")
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert (review.bundle / "committed.json").exists()
    assert review.pending.exists()

    result = finalize(tmp_path, review.decision())

    assert result["ingestion_id"] == review.ingestion_id
    assert not review.pending.exists()
    assert not review.intent.exists()


def test_receipt_retry_completes_remaining_cleanup_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(monkeypatch, "_complete_cleanup_record")
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    cleanup = workspace._finalize_cleanup_record(
        review.target_id, review.revision, material=True, report=_REPORT, run_id=_RUN_ID
    )
    workspace._write_recovery_record(review.state, cleanup)

    result = finalize(tmp_path, review.decision())

    assert result["ingestion_id"] == review.ingestion_id
    assert not review.pending.exists()
    assert not review.intent.exists()


def test_recovery_via_check_resumes_archive_before_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    monkeypatch.setattr(
        workspace,
        "_monitor_target",
        _unchanged,
    )

    outcome = check(tmp_path)

    assert outcome["targets"]
    assert (review.bundle / "committed.json").exists()
    assert review.snapshot.read_text() == "new\n"


def test_retry_after_cleanup_returns_same_receipt(tmp_path: Path) -> None:
    review = _review(tmp_path)
    first = review.finalize()

    again = finalize(tmp_path, review.decision())

    assert again == first


def test_retry_does_not_touch_newer_pending_revision(tmp_path: Path) -> None:
    review = _review(tmp_path)
    first = review.finalize()
    target = workspace.load_targets(tmp_path)[0]
    newer = workspace._handle_monitor_result(
        review.state,
        target,
        {
            "status": "changed",
            "sha256": hashlib.sha256(b"newer\n").hexdigest(),
            "previous_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff": "+newer",
            "diff_truncated": False,
        },
        _RUN_ID,
        candidate_data=b"newer\n",
    )

    assert finalize(tmp_path, review.decision()) == first

    pending = workspace._read_pending(review.state, review.target_id)
    assert pending["revision"] == newer["revision"]
    assert review.snapshot.read_text() == "new\n"


@pytest.mark.parametrize(
    "decision",
    [
        {"material": False},
        {"report": "## Different\n"},
    ],
    ids=["non-material", "changed-report"],
)
def test_retry_with_changed_decision_fails_closed(
    tmp_path: Path, decision: dict[str, object]
) -> None:
    review = _review(tmp_path)
    review.finalize()
    changed = review.decision(**cast("Any", decision))

    with pytest.raises(WorkspaceError, match="does not match the archived"):
        finalize(tmp_path, changed)


@pytest.mark.parametrize(
    ("file", "content"),
    [
        ("parent.txt", b"tampered\n"),
        ("diff.txt", b"x" * len(_DIFF)),
        ("parent.txt", None),
        ("metadata.json", b"{}"),
        ("metadata.json", b"[]"),
        ("metadata.json", b"\xff"),
        ("metadata.json", None),
        ("committed.json", b"{}"),
        ("committed.json", b"not json"),
    ],
    ids=[
        "parent-bytes",
        "diff-bytes",
        "parent-missing",
        "metadata-empty",
        "metadata-not-object",
        "metadata-not-utf8",
        "metadata-missing",
        "receipt-empty",
        "receipt-not-json",
    ],
)
def test_corrupt_artifacts_fail_closed(
    tmp_path: Path, file: str, content: bytes | None
) -> None:
    review = _review(tmp_path)
    review.finalize()
    if content is None:
        (review.bundle / file).unlink()
    else:
        (review.bundle / file).write_bytes(content)

    with pytest.raises(WorkspaceError):
        finalize(tmp_path, review.decision())


_RECEIPT = "committed.json"
_METADATA = "metadata.json"


@pytest.mark.parametrize(
    ("file", "path", "value"),
    [
        (_RECEIPT, ("schema",), "other"),
        (_RECEIPT, ("material",), False),
        (_RECEIPT, ("manifest_sha256",), "0" * 64),
        (_RECEIPT, ("report_section_sha256",), "0" * 64),
        (_RECEIPT, ("revision",), "b" * 32),
        (_RECEIPT, ("run_id",), "x"),
        (_RECEIPT, ("archived_at",), "x"),
        (_RECEIPT, ("target_id",), "other"),
        (_RECEIPT, ("extra",), 1),
        (_METADATA, ("schema",), "v2"),
        (_METADATA, ("target_id",), 1),
        (_METADATA, ("revision",), 1),
        (_METADATA, ("target_id",), "x"),
        (_METADATA, ("payloads",), []),
        (_METADATA, ("payloads", "diff.txt"), _DELETE),
        (_METADATA, ("payloads", "parent.txt"), 1),
        (_METADATA, ("payloads", "parent.txt", "bytes"), -1),
        (_METADATA, ("payloads", "parent.txt", "bytes"), 10**9),
        (_METADATA, ("payloads", "parent.txt", "sha256"), "zz"),
        (_METADATA, ("decision",), None),
    ],
    ids=[
        "schema",
        "material",
        "manifest",
        "report-digest",
        "revision",
        "run-id",
        "archived-at",
        "target",
        "extra-field",
        "metadata-schema",
        "metadata-target-type",
        "metadata-revision-type",
        "metadata-identity",
        "metadata-payload-type",
        "metadata-payload-missing",
        "metadata-entry-type",
        "metadata-bytes-negative",
        "metadata-bytes-oversize",
        "metadata-sha-invalid",
        "metadata-decision",
    ],
)
def test_receipt_validation_fails_closed(
    tmp_path: Path, file: str, path: tuple[str, ...], value: object
) -> None:
    review = _review(tmp_path)
    review.finalize()
    _tamper(review.bundle, file, path, value)

    with pytest.raises(WorkspaceError):
        finalize(tmp_path, review.decision())


def test_staged_bundle_without_receipt_is_not_an_accepted_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)

    assert (review.bundle / "parent.txt").exists()
    assert not (review.bundle / "committed.json").exists()
    assert workspace._read_receipt(tmp_path, review.ingestion_id) is None


def test_evidence_directory_must_not_be_a_symlink(tmp_path: Path) -> None:
    review = _review(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "evidence").symlink_to(outside)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        finalize(tmp_path, review.decision())


def test_bundle_directory_must_not_be_a_symlink(tmp_path: Path) -> None:
    review = _review(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "evidence").mkdir()
    review.bundle.symlink_to(outside)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        finalize(tmp_path, review.decision())


def test_adopting_archive_after_completed_legacy_finalize_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(monkeypatch, "_complete_cleanup_record")
    with pytest.raises(WorkspaceError, match="injected"):
        finalize(tmp_path, review.decision())

    with pytest.raises(WorkspaceError, match="cannot be adopted"):
        review.finalize()

    assert not (tmp_path / "evidence").exists()


def test_adopting_archive_without_pending_state_is_rejected(tmp_path: Path) -> None:
    review = _review(tmp_path)
    finalize(tmp_path, review.decision())

    with pytest.raises(WorkspaceError, match="cannot be adopted"):
        review.finalize()

    assert not (tmp_path / "evidence").exists()


_INACTIVE_CSV = {
    "disabled": _HEADER + f"Off,{_URL},,,,,1,false\n",
    "removed": _HEADER + "Other,https://other.example/,,,,,1,true\n",
    "invalid": "name\nx\n",
    "missing-csv": None,
}


@pytest.mark.parametrize("scenario", sorted(_INACTIVE_CSV))
def test_inactive_target_or_invalid_configuration_archives_nothing(
    tmp_path: Path, scenario: str
) -> None:
    review = _review(tmp_path)
    text = _INACTIVE_CSV[scenario]
    if text is None:
        (tmp_path / "targets.csv").unlink()
    else:
        (tmp_path / "targets.csv").write_text(text, encoding="utf-8")

    with pytest.raises(WorkspaceError):
        review.finalize()

    assert not (tmp_path / "evidence").exists()
    assert not review.intent.exists()
    assert review.snapshot.read_text() == "old\n"
    assert review.pending.exists()


def test_snapshot_conflict_before_preparation_archives_nothing(
    tmp_path: Path,
) -> None:
    review = _review(tmp_path)
    review.snapshot.write_text("someone else\n", encoding="utf-8")

    assert review.finalize() == {
        "action": "snapshot_conflict",
        "target_id": review.target_id,
    }

    assert not (tmp_path / "evidence").exists()
    assert not review.intent.exists()
    assert review.pending.exists()


def test_snapshot_conflict_after_preparation_preserves_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    review.snapshot.write_text("someone else\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="snapshot conflict"):
        review.finalize()

    assert review.intent.exists()
    assert not (review.bundle / "committed.json").exists()
    assert review.pending.exists()
    assert review.snapshot.read_text() == "someone else\n"


def test_csv_edits_after_preparation_cannot_erase_the_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    (tmp_path / "targets.csv").unlink()

    result = finalize(tmp_path, review.decision())

    metadata = json.loads((review.bundle / "metadata.json").read_text())
    assert result["ingestion_id"] == review.ingestion_id
    assert [item["name"] for item in metadata["interests"]] == ["Plans", "API"]


def test_discard_cannot_cancel_a_prepared_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    _fail_once(monkeypatch, "_promote_snapshot")

    with pytest.raises(WorkspaceError, match="in progress and blocked"):
        discard_pending(tmp_path, review.target_id)

    assert review.intent.exists()
    assert review.pending.exists()

    with pytest.raises(WorkspaceError, match="completed during recovery"):
        discard_pending(tmp_path, review.target_id)

    assert (review.bundle / "committed.json").exists()
    assert review.snapshot.read_text() == "new\n"


def test_blocked_archive_does_not_stop_pending_listing_or_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    monkeypatch.setattr(workspace, "_promote_snapshot", _conflicting_promotion)

    listing = workspace.pending_reviews(tmp_path)
    assert listing["reviews"] == []
    blocked = cast("list[dict[str, str]]", listing["blocked"])
    assert blocked[0]["target_id"] == review.target_id
    assert "snapshot conflict" in blocked[0]["error"]

    with pytest.raises(WorkspaceError, match="snapshot conflict"):
        workspace.pending_reviews(tmp_path, target_id=review.target_id)

    outcome = check(tmp_path)
    targets = cast("list[dict[str, str]]", outcome["targets"])
    assert targets[0]["action"] == "error"
    assert "snapshot conflict" in targets[0]["error"]
    assert review.intent.exists()


def test_non_archive_recovery_failure_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)

    def broken(_state: Path, _target_id: str) -> None:
        message = "plain failure"
        raise WorkspaceError(message)

    monkeypatch.setattr(workspace, "_prepare_pending_for_read", broken)

    with pytest.raises(WorkspaceError, match="plain failure"):
        workspace.pending_reviews(tmp_path)
    assert review.pending.exists()


def test_decision_must_match_prepared_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)

    for decision in (
        review.decision(material=False),
        review.decision(report="## Different\n"),
        {**review.decision(), "revision": "c" * 32},
    ):
        with pytest.raises(WorkspaceError, match="prepared archive transaction"):
            finalize(tmp_path, decision)

    assert review.intent.exists()


def test_pending_replacement_cannot_bypass_archive_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    target = workspace.load_targets(tmp_path)[0]

    outcome = workspace._handle_monitor_result(
        review.state,
        target,
        {
            "status": "changed",
            "sha256": hashlib.sha256(b"newer\n").hexdigest(),
            "previous_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff": "+newer",
            "diff_truncated": False,
        },
        _RUN_ID,
        candidate_data=b"newer\n",
    )

    # Recovery completed the archive transaction before the replacement.
    assert (review.bundle / "committed.json").exists()
    assert outcome["revision"] != review.revision
    assert not review.intent.exists()


def test_resume_requires_pending_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    for path in review.pending.iterdir():
        path.unlink()
    review.pending.rmdir()

    with pytest.raises(WorkspaceError, match="without pending evidence"):
        finalize(tmp_path, review.decision())

    assert review.intent.exists()


def test_resume_rejects_pending_that_differs_from_frozen_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    pending_file = review.pending / "state.json"
    record = json.loads(pending_file.read_text())
    record["link_review"] = {"documents": [], "omitted": 5, "incomplete": True}
    pending_file.write_text(json.dumps(record))

    with pytest.raises(WorkspaceError, match="does not match the archive"):
        finalize(tmp_path, review.decision())

    assert review.intent.exists()


def test_install_reuses_identical_files_and_rejects_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(
        monkeypatch,
        "_install_evidence_file",
        when=_is_diff_install,
    )
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert (review.bundle / "parent.txt").exists()

    # Identical staged files are reused on retry.
    review.finalize()
    assert (review.bundle / "committed.json").exists()


def test_install_conflict_fails_closed(tmp_path: Path) -> None:
    review = _review(tmp_path)
    review.bundle.mkdir(parents=True)
    (review.bundle / "parent.txt").write_text("conflicting\n")

    with pytest.raises(WorkspaceError, match="conflicts with existing evidence"):
        review.finalize()

    assert review.intent.exists()
    assert review.snapshot.read_text() == "old\n"


def test_install_rejects_non_regular_file(tmp_path: Path) -> None:
    review = _review(tmp_path)
    review.bundle.mkdir(parents=True)
    (review.bundle / "parent.txt").mkdir()

    with pytest.raises(WorkspaceError, match="conflicts with existing evidence"):
        review.finalize()


def test_install_reports_replace_and_fsync_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    original = Path.replace

    def failing_replace(self: Path, destination: Path) -> Path:
        if self.name.endswith(".tmp"):
            message = "replace failed"
            raise OSError(message)
        return original(self, destination)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(WorkspaceError, match="cannot install"):
        workspace._install_evidence_file(target, b"data", "evidence file")
    monkeypatch.setattr(Path, "replace", original)
    assert not list(tmp_path.glob(".*tmp"))

    def failing_fsync(_path: Path) -> None:
        message = "fsync failed"
        raise OSError(message)

    monkeypatch.setattr(workspace, "_fsync_directory", failing_fsync)
    with pytest.raises(WorkspaceError, match="cannot fsync"):
        workspace._install_evidence_file(target, b"data", "evidence file")


def test_install_detects_read_back_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    monkeypatch.setattr(Path, "read_bytes", _other_bytes)

    with pytest.raises(WorkspaceError, match="read-back mismatch"):
        workspace._install_evidence_file(target, b"data", "evidence file")


def test_read_helpers_report_io_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"abc")

    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._read_limited(tmp_path / "missing", 10, "thing")
    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._read_limited(path, 2, "thing")
    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._hash_file(tmp_path / "missing", 10, "thing")
    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._hash_file(path, 2, "thing")
    assert workspace._hash_file(path, 10, "thing") == (
        3,
        hashlib.sha256(b"abc").hexdigest(),
    )

    def broken(*_args: object, **_kwargs: object) -> bytes:
        message = "io failed"
        raise OSError(message)

    monkeypatch.setattr(Path, "read_bytes", broken)
    with pytest.raises(WorkspaceError, match="cannot read thing"):
        workspace._read_limited(path, 10, "thing")
    monkeypatch.setattr(Path, "open", broken)
    with pytest.raises(WorkspaceError, match="cannot read thing"):
        workspace._hash_file(path, 10, "thing")


def test_read_helpers_detect_growth_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"abc")
    monkeypatch.setattr(Path, "read_bytes", _grown_bytes)
    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._read_limited(path, 5, "thing")

    real_open = Path.open

    def growing(self: Path, *args: Any, **kwargs: Any) -> Any:  # ruff: ignore[any-type]
        if self == path:
            return io.BytesIO(b"abcdefgh")
        return cast("Any", real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", growing)
    with pytest.raises(WorkspaceError, match="missing or invalid"):
        workspace._hash_file(path, 5, "thing")


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("_ARCHIVE_PAYLOAD_LIMITS", {"parent.txt": 1}, "parent.txt exceeds"),
        (
            "_ARCHIVE_PAYLOAD_LIMITS",
            {"parent.txt": 10**6, "diff.txt": 1, "links.json": 10**6},
            "diff.txt exceeds",
        ),
        ("_MAX_ARCHIVE_REPORT_BYTES", 4, "report exceeds"),
        ("_MAX_ARCHIVE_METADATA_BYTES", 10, "metadata exceeds"),
        ("_MAX_ARCHIVE_INTENT_BYTES", 10, "intent exceeds"),
    ],
    ids=["parent", "diff", "report", "metadata", "intent"],
)
def test_archive_limits_are_enforced_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
    match: str,
) -> None:
    review = _review(tmp_path)
    monkeypatch.setattr(workspace, name, value)

    with pytest.raises(WorkspaceError, match=match):
        review.finalize()

    assert not review.intent.exists()
    assert not (tmp_path / "evidence").exists()
    assert review.pending.exists()
    assert review.snapshot.read_text() == "old\n"


def test_large_but_bounded_diff_is_archived(tmp_path: Path) -> None:
    review = _review(tmp_path, diff="+" + "x" * 200_000)

    review.finalize()

    assert (review.bundle / "diff.txt").stat().st_size == 200_001


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 1},
        {"target_id": "other"},
        {"extra": 1},
        {"report": ""},
        {"report": "tampered"},
        {"metadata": "{}"},
        {"metadata_sha256": "0" * 64},
        {"metadata": "not json"},
        {"revision": "short"},
        {"run_id": "bad"},
        {"ingestion_id": "0" * 64},
        {"candidate_sha256": "bad"},
        {"expected_sha256": "bad"},
        {"archived_at": 1},
        {"diff": 1},
        {"diff": "tampered"},
        {"report": 1},
        {"metadata": 1},
    ],
    ids=[
        "version",
        "target",
        "extra",
        "empty-report",
        "report-digest",
        "metadata-empty",
        "metadata-digest",
        "metadata-not-json",
        "revision",
        "run-id",
        "ingestion",
        "candidate",
        "expected",
        "archived-at",
        "diff-type",
        "diff-digest",
        "report-type",
        "metadata-type",
    ],
)
def test_corrupt_intent_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    _write_intent(review, **changes)

    with pytest.raises(WorkspaceError, match=r"invalid|SHA-256"):
        finalize(tmp_path, review.decision())

    assert review.pending.exists()
    assert review.snapshot.read_text() == "old\n"


def test_intent_with_wrong_target_record_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _prepare_blocked(review, monkeypatch)
    record = json.loads(review.intent.read_text())

    with pytest.raises(WorkspaceError, match="invalid"):
        workspace._validate_recovery_record(record, "another-target")


def test_main_finalize_archive_evidence_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(review.decision())))

    status = workspace.main([
        "--workspace",
        str(tmp_path),
        "finalize",
        "--archive-evidence",
    ])

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["ingestion_id"] == review.ingestion_id
    assert Path(output["evidence_path"]).is_dir()


def test_ingestion_identity_is_transaction_scoped() -> None:
    first = workspace._ingestion_id("target", "a" * 32)

    assert first == workspace._ingestion_id("target", "a" * 32)
    assert first != workspace._ingestion_id("target", "b" * 32)
    assert first != workspace._ingestion_id("other", "a" * 32)


def test_ordinary_finalize_ignores_other_targets_bundles(tmp_path: Path) -> None:
    review = _review(tmp_path)
    (tmp_path / "evidence").mkdir()

    result = finalize(tmp_path, review.decision())

    assert "ingestion_id" not in result
    assert review.snapshot.read_text() == "new\n"


def test_receipt_must_match_the_prepared_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    _fail_once(
        monkeypatch,
        "_write_recovery_record",
        when=_is_cleanup_record,
    )
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert review.intent.exists()
    assert (review.bundle / "committed.json").exists()
    other = "## Other\n"
    _write_intent(
        review,
        report=other,
        report_sha256=hashlib.sha256(other.encode()).hexdigest(),
    )

    with pytest.raises(WorkspaceError, match="receipt does not match"):
        finalize(tmp_path, review.decision(report=other))

    assert review.intent.exists()


def _legacy_review(root: Path) -> _Review:
    # A pending record with only the base fields has no stored diff.
    _targets(root)
    target_id = str(workspace.load_targets(root)[0]["target_id"])
    state = root / ".wsum"
    snapshots = state / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / f"{target_id}.txt").write_text("old\n", encoding="utf-8")
    revision = "d" * 32
    workspace._write_pending_transaction(
        state,
        {
            "target_id": target_id,
            "run_id": _RUN_ID,
            "revision": revision,
            "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "candidate_sha256": hashlib.sha256(b"new\n").hexdigest(),
            "diff_truncated": False,
        },
        b"new\n",
    )
    return _Review(root, target_id, revision)


def test_legacy_pending_record_recovers_after_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _legacy_review(tmp_path)
    _fail_once(monkeypatch, "_write_report")
    with pytest.raises(WorkspaceError, match="injected"):
        review.finalize()
    assert review.snapshot.read_text() == "new\n"
    assert review.intent.exists()

    result = finalize(tmp_path, review.decision())

    assert result["ingestion_id"] == review.ingestion_id
    assert (review.bundle / "diff.txt").read_text().startswith("--- ")
    assert (review.bundle / "committed.json").exists()
    assert not review.pending.exists()
    assert not review.intent.exists()
