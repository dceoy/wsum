"""Tests for the agent-facing CSV workflow."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import runpy
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import workspace
from workspace import WorkspaceError, check, finalize, load_targets

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterator


_RUN_ID = "20261001T000000Z-deadbeef"


def _write_targets(path: Path, rows: str) -> None:
    path.write_text(f"name,url,criteria,enabled\n{rows}", encoding="utf-8")


def _changed_result(
    current: str = "new\n", previous: str = "old\n"
) -> dict[str, object]:
    return {
        "status": "changed",
        "sha256": hashlib.sha256(current.encode()).hexdigest(),
        "previous_sha256": hashlib.sha256(previous.encode()).hexdigest(),
        "diff": "--- previous\n+++ current\n-old\n+new",
        "diff_truncated": False,
    }


def _write_review_transaction(
    state: Path,
    *,
    target_id: str = "example",
    revision: str = "a" * 32,
    run_id: str = _RUN_ID,
    diff_truncated: bool = False,
    baseline: str = "old\n",
    candidate: str = "new\n",
) -> tuple[Path, Path, Path]:
    directory = state / "pending" / target_id
    metadata = directory / "state.json"
    candidate_path = directory / "candidate.txt"
    directory.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(candidate, encoding="utf-8")

    snapshots = state / "snapshots"
    snapshots.mkdir(exist_ok=True)
    snapshot = snapshots / f"{target_id}.txt"
    snapshot.write_text(baseline, encoding="utf-8")

    payload: dict[str, object] = {
        "target_id": target_id,
        "run_id": run_id,
        "revision": revision,
        "expected_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "diff_truncated": diff_truncated,
        "name": "Example",
        "url": "https://example.com/",
        "diff": "--- previous\n+++ current\n-old\n+new",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    return metadata, candidate_path, snapshot


def test_load_targets_normalizes_csv_and_generates_stable_ids(tmp_path: Path) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Example,https://example.com/,pricing,true\n"
        "Disabled,https://example.org/,,false\n",
    )

    targets = load_targets(tmp_path / "targets.csv")

    assert [target["action"] for target in targets] == ["monitor", "skip_disabled"]
    assert (
        cast("list[dict[str, object]]", targets[0]["interests"])[0]["criteria"]
        == "pricing"
    )
    assert "fetch_mode" not in targets[0]
    assert str(targets[0]["target_id"]).startswith("example-com-")
    first_id = targets[0]["target_id"]

    _write_targets(
        tmp_path / "targets.csv",
        "Renamed,https://example.com/,pricing,true\n",
    )
    assert load_targets(tmp_path / "targets.csv")[0]["target_id"] == first_id


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        ("name,criteria\n", "Example,pricing\n", "requires name and url"),
        (
            "name,url,watch_focus\n",
            "Example,https://example.com/,pricing\n",
            "unsupported",
        ),
        (
            "name,url,criteria,enabled\n",
            "Example,https://example.com/,,yes\n",
            "enabled must be true or false",
        ),
        (
            "name,url,criteria,enabled\n",
            ",https://example.com/,,true\n",
            "name must be non-empty",
        ),
    ],
    ids=[
        "missing-required-column",
        "removed-watch-focus",
        "invalid-enabled",
        "empty-name",
    ],
)
def test_load_targets_rejects_invalid_csv(
    tmp_path: Path, header: str, rows: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(header + rows, encoding="utf-8")

    with pytest.raises(WorkspaceError, match=message):
        load_targets(tmp_path / "targets.csv")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "https://user:pass@example.com/",
        "https://example.com/?token=secret",
        "https://example.com/#fragment",
    ],
    ids=["non-http-scheme", "credentials", "query-secret", "fragment"],
)
def test_load_targets_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    _write_targets(tmp_path / "targets.csv", f"Example,{url},,true\n")

    with pytest.raises(WorkspaceError):
        load_targets(tmp_path / "targets.csv")


def test_handle_monitor_result_promotes_baseline(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    candidate_dir = state / "pending" / "example"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "candidate.txt"
    content = "baseline\n"
    candidate.write_text(content)
    target = {"target_id": "example", "name": "Example"}

    result = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state,
        target,
        {
            "status": "baseline",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        _RUN_ID,
    )

    assert result["action"] == "baseline_created"
    assert (state / "snapshots" / "example.txt").read_text() == content
    assert not candidate.exists()


def test_handle_monitor_result_records_changed_candidate(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    candidate_dir = state / "pending" / "example"
    snapshot_dir = state / "snapshots"
    candidate_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    candidate = candidate_dir / "candidate.txt"
    candidate.write_text("new\n")
    (snapshot_dir / "example.txt").write_text("old\n")
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }

    result = workspace._handle_monitor_result(state, target, _changed_result(), _RUN_ID)

    assert result["action"] == "review"
    assert len(str(result["revision"])) == 32
    assert (
        cast("list[dict[str, object]]", result["interests"])[0]["criteria"] == "pricing"
    )
    pending = json.loads((state / "pending" / "example" / "state.json").read_text())
    assert pending["target_id"] == "example"
    assert pending["run_id"] == _RUN_ID
    assert pending["revision"] == result["revision"]
    assert pending["name"] == "Example"
    assert pending["url"] == "https://example.com/"
    assert pending["interests"] == result["interests"]
    assert pending["diff"] == _changed_result()["diff"]
    assert candidate.exists()


def test_finalize_material_review_promotes_and_writes_report(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "## Example\n\nPricing changed.\n",
        },
    )

    report = tmp_path / "output" / "report" / f"{_RUN_ID}.md"
    assert result["action"] == "finalized"
    assert result["report_path"] == str(report)
    assert report.exists()
    assert "Pricing changed." in report.read_text()
    assert snapshot.read_text() == "new\n"
    assert not candidate.exists()
    assert not metadata.exists()
    assert not (state / "pending" / "example").exists()


def test_finalize_non_material_truncated_diff_stops(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(
        state, diff_truncated=True
    )

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result == {"action": "manual_review_required", "target_id": "example"}
    assert metadata.exists()
    assert candidate.exists()
    assert snapshot.read_text() == "old\n"


def test_finalize_non_material_review_promotes_without_report(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result == {"action": "finalized", "target_id": "example", "material": False}
    assert snapshot.read_text() == "new\n"
    assert not (tmp_path / "output" / "report").exists()
    assert not metadata.exists()
    assert not candidate.exists()


@pytest.mark.parametrize("failure", ["partial-delete", "parent-fsync"])
def test_finalize_cleanup_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nRetry cleanup.\n",
    }
    failed = False

    if failure == "partial-delete":
        original_rmtree = workspace.shutil.rmtree

        def partially_remove_group(path: Path) -> None:
            nonlocal failed
            if path == candidate.parent and not failed:
                failed = True
                candidate.unlink()
                raise PermissionError
            original_rmtree(path)

        monkeypatch.setattr(workspace.shutil, "rmtree", partially_remove_group)
    else:
        original_fsync = workspace._fsync_directory
        pending_dir = state / "pending"

        def fail_pending_fsync(path: Path) -> None:
            nonlocal failed
            if path == pending_dir and not failed:
                failed = True
                raise OSError
            original_fsync(path)

        monkeypatch.setattr(workspace, "_fsync_directory", fail_pending_fsync)

    with pytest.raises(WorkspaceError, match=r"pending transaction|fsync"):
        finalize(tmp_path, decision)

    assert failed
    assert (state / "recovery" / "example.json").exists()
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    report = tmp_path / "output" / "report" / f"{_RUN_ID}.md"
    assert "Retry cleanup." in report.read_text(encoding="utf-8")

    result = finalize(tmp_path, decision)

    assert result["action"] == "finalized"
    assert not metadata.exists()
    assert not candidate.exists()
    assert not (state / "pending" / "example").exists()
    assert not (state / "recovery" / "example.json").exists()


def test_finalize_rejects_stale_review_revision(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    snapshot_dir = state / "snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "example.txt").write_text("old\n")
    reports = tmp_path / "output" / "report"
    reports.mkdir(parents=True)
    report = reports / f"{_RUN_ID}.md"
    original_report = (
        f"# Web Update Monitor Report\n\nRun: `{_RUN_ID}`\n\ncurrent report\n"
    )
    report.write_text(original_report)
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }

    first = workspace._handle_monitor_result(
        state,
        target,
        _changed_result(current="first\n"),
        _RUN_ID,
        candidate_data=b"first\n",
    )
    first_revision = str(first["revision"])
    second = workspace._handle_monitor_result(
        state,
        target,
        _changed_result(current="second\n"),
        _RUN_ID,
        candidate_data=b"second\n",
    )
    pending_path = state / "pending" / "example" / "state.json"
    candidate_path = state / "pending" / "example" / "candidate.txt"
    pending = pending_path.read_bytes()

    with pytest.raises(WorkspaceError, match="revision"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": first_revision,
                "material": True,
                "report": "# Stale\n",
            },
        )

    assert (snapshot_dir / "example.txt").read_text() == "old\n"
    assert report.read_text() == original_report
    assert candidate_path.read_text() == "second\n"
    assert pending_path.read_bytes() == pending
    assert str(second["revision"]) != first_revision


def test_finalize_material_snapshot_conflict_does_not_write_report(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    reports = tmp_path / "output" / "report"
    reports.mkdir(parents=True)
    report = reports / f"{_RUN_ID}.md"
    original_report = (
        f"# Web Update Monitor Report\n\nRun: `{_RUN_ID}`\n\nprevious report\n"
    )
    report.write_text(original_report)
    snapshot.write_text("external\n")

    result = finalize(
        tmp_path,
        {
            "target_id": "example",
            "revision": "a" * 32,
            "material": True,
            "report": "# Candidate\n",
        },
    )

    assert result == {"action": "snapshot_conflict", "target_id": "example"}
    assert snapshot.read_text() == "external\n"
    assert report.read_text() == original_report
    assert candidate.read_text() == "new\n"
    assert metadata.exists()


def test_finalize_report_failure_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    write_report = workspace._write_report
    write_attempts = 0

    def fail_once(root: Path, run_id: str, target_id: str, report: str) -> Path:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            message = "cannot write report"
            raise WorkspaceError(message)
        return write_report(root, run_id, target_id, report)

    monkeypatch.setattr(workspace, "_write_report", fail_once)
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nPricing changed.\n",
    }

    with pytest.raises(WorkspaceError, match="cannot write report"):
        finalize(tmp_path, decision)

    assert snapshot.read_text() == "new\n"
    assert metadata.exists()
    assert candidate.exists()
    assert not (tmp_path / "output" / "report").exists()

    result = finalize(tmp_path, decision)

    report_path = tmp_path / "output" / "report" / f"{_RUN_ID}.md"
    assert result["report_path"] == str(report_path)
    assert write_attempts == 2
    assert report_path.read_text().count("<!-- wsum:target example:start -->") == 1
    assert not metadata.exists()
    assert not candidate.exists()


def test_check_batches_targets_and_contains_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Good,https://example.com/,,true\n"
        "Bad,https://example.org/,,true\n"
        "Off,https://example.net/,,false\n",
    )

    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)

    def fake_monitor(
        _state: Path,
        target: dict[str, object],
        run_id: str,
        *,
        link_depth: int,
        max_links: int,
    ) -> dict[str, object]:
        assert run_id == _RUN_ID
        assert link_depth == workspace._DEFAULT_LINK_DEPTH
        assert max_links == workspace._MAX_LINKS
        if target["name"] == "Bad":
            raise workspace.monitor.MonitorError
        return {
            "action": "unchanged",
            "target_id": target["target_id"],
            "name": target["name"],
        }

    monkeypatch.setattr(workspace, "_monitor_target", fake_monitor)

    result = check(tmp_path, tmp_path / "targets.csv")
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert result["run_id"] == _RUN_ID
    assert [item["action"] for item in outcomes] == [
        "unchanged",
        "error",
        "skipped",
    ]


@pytest.mark.parametrize(
    ("status", "expected_action"),
    [(403, "agent_fetch_required"), (429, "error")],
    ids=["forbidden", "other-status"],
)
def test_check_routes_only_forbidden_to_agent_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_action: str,
) -> None:
    _write_targets(tmp_path / "targets.csv", "Example,https://example.com/,,true\n")
    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)
    final_url = "https://redirected.example/releases/current"

    def forbidden(
        _state: Path,
        _target: dict[str, object],
        _run_id: str,
        *,
        link_depth: int,
        max_links: int,
    ) -> dict[str, object]:
        del link_depth, max_links
        raise workspace.monitor.HTTPStatusError(
            status, source_url=final_url if status == 403 else ""
        )

    monkeypatch.setattr(workspace, "_monitor_target", forbidden)
    result = check(tmp_path, tmp_path / "targets.csv")
    outcome = cast("list[dict[str, object]]", result["targets"])[0]

    assert outcome["action"] == expected_action
    if status == 403:
        assert outcome == {
            "action": "agent_fetch_required",
            "run_id": _RUN_ID,
            "target_id": load_targets(tmp_path / "targets.csv")[0]["target_id"],
            "name": "Example",
            "url": final_url,
            "status": 403,
            "link_depth": workspace._DEFAULT_LINK_DEPTH,
            "max_links": workspace._MAX_LINKS,
        }
    else:
        assert outcome["error"] == "fetch failed: HTTP 429"


@pytest.mark.parametrize(
    ("source_url", "expected_source_url"),
    [
        (None, "https://example.com/"),
        ("https://redirected.example/current", "https://redirected.example/current"),
    ],
    ids=["configured-url-default", "explicit-final-url"],
)
def test_ingest_agent_fetch_uses_normal_monitor_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_url: str | None,
    expected_source_url: str,
) -> None:
    _write_targets(tmp_path / "targets.csv", "Example,https://example.com/,,true\n")
    target = load_targets(tmp_path / "targets.csv")[0]
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("agent fetched content\n", encoding="utf-8")

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        assert args.input == fetched
        assert args.source_url == expected_source_url
        assert args.content_type == "text/plain"
        Path(args.output).write_text("agent fetched content\n", encoding="utf-8")
        return {
            "source_url": args.source_url,
            "content_type": args.content_type,
            "links": {},
            "status": "baseline",
            "sha256": hashlib.sha256(b"agent fetched content\n").hexdigest(),
            "previous_sha256": "",
            "diff": "",
            "diff_truncated": False,
        }

    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)
    result = workspace.ingest_agent_fetch(
        tmp_path,
        tmp_path / "targets.csv",
        target_id=str(target["target_id"]),
        run_id=_RUN_ID,
        input_path=fetched,
        source_url=source_url,
    )

    assert result["action"] == "baseline_created"
    snapshot = (
        tmp_path / "internal" / "state" / "snapshots" / f"{target['target_id']}.txt"
    )
    assert snapshot.read_text(encoding="utf-8") == "agent fetched content\n"


@pytest.mark.parametrize(
    ("source_url", "error_type", "message"),
    [
        ("file:///tmp/page", WorkspaceError, "absolute"),
        ("https://user:pass@example.com/", WorkspaceError, "credentials"),
        ("https://example.com/?token=secret", WorkspaceError, "credentials"),
        ("https://example.com/#section", WorkspaceError, "fragment"),
        ("http://127.0.0.1/", workspace.monitor.MonitorError, "public IP"),
    ],
    ids=[
        "unsupported-scheme",
        "userinfo",
        "credential-query",
        "fragment",
        "private-ip",
    ],
)
def test_ingest_agent_fetch_rejects_unsafe_source_urls(
    tmp_path: Path,
    source_url: str,
    error_type: type[Exception],
    message: str,
) -> None:
    targets = tmp_path / "targets.csv"
    _write_targets(targets, "Example,https://example.com/,,true\n")
    target = load_targets(targets)[0]
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("content\n", encoding="utf-8")

    with pytest.raises(error_type, match=message):
        workspace.ingest_agent_fetch(
            tmp_path,
            targets,
            target_id=str(target["target_id"]),
            run_id=_RUN_ID,
            input_path=fetched,
            source_url=source_url,
        )


def test_ingest_agent_fetch_follows_links_from_navigation_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = tmp_path / "targets.csv"
    _write_targets(targets, "Example,https://example.com/,,true\n")
    parent_url = "https://example.com/"
    child_url = "https://example.com/release"
    blocked = {"value": False}

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        if url == parent_url:
            if blocked["value"]:
                raise workspace.monitor.HTTPStatusError(403)
            return workspace.monitor.Document(
                b'<p>Old release</p><a href="/old">Old</a>',
                url,
                "text/html",
            )
        if url == child_url:
            return workspace.monitor.Document(b"New release", url, "text/plain")
        raise AssertionError

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline_outcomes = cast(
        "list[dict[str, object]]", check(tmp_path, targets)["targets"]
    )
    assert baseline_outcomes[0]["action"] == "baseline_created"

    blocked["value"] = True
    required_outcomes = cast(
        "list[dict[str, object]]", check(tmp_path, targets)["targets"]
    )
    required = required_outcomes[0]
    assert required["action"] == "agent_fetch_required"
    assert required["link_depth"] == workspace._DEFAULT_LINK_DEPTH

    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("New release notes\n", encoding="utf-8")
    links = tmp_path / "navigation-links.json"
    links.write_text(json.dumps([child_url]), encoding="utf-8")

    review = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=str(required["target_id"]),
        run_id=str(required["run_id"]),
        input_path=fetched,
        content_type="text/plain",
        links_path=links,
        link_depth=cast("int", required["link_depth"]),
        max_links=cast("int", required["max_links"]),
    )

    assert review["action"] == "review"
    link_review = cast("dict[str, object]", review["link_review"])
    documents = cast("list[dict[str, object]]", link_review["documents"])
    assert documents[0]["url"] == child_url
    assert documents[0]["text"] == "New release\n"
    assert link_review["incomplete"] is False


def test_ingest_agent_fetch_uses_redirected_source_for_relative_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = tmp_path / "targets.csv"
    parent_url = "https://example.com/start"
    final_url = "https://cdn.example/releases/current/index.html"
    child_url = "https://cdn.example/releases/current/notes.html"
    _write_targets(targets, f"Example,{parent_url},,true\n")
    blocked = {"value": False}
    fetched_links: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        if url == parent_url:
            if blocked["value"]:
                raise workspace.monitor.HTTPStatusError(403, source_url=final_url)
            return workspace.monitor.Document(
                b"<main><p>Old release</p><a href='/old'>Old</a></main>",
                url,
                "text/html",
            )
        fetched_links.append(url)
        assert url == child_url
        return workspace.monitor.Document(b"New notes", url, "text/plain")

    def accept_public_source(
        _url: str, *, deadline: float | None = None
    ) -> workspace.monitor._ResolvedTarget | None:
        del deadline
        return None

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    monkeypatch.setattr(workspace.monitor, "_resolve_public_url", accept_public_source)
    baseline = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    assert baseline["action"] == "baseline_created"

    blocked["value"] = True
    required = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    assert required["action"] == "agent_fetch_required"
    assert required["url"] == final_url

    fetched = tmp_path / "agent-fetch.html"
    fetched.write_text(
        "<main><p>New release</p>"
        f'<a href="{parent_url}">Configured target</a>'
        f'<a href="{final_url}">Final response</a>'
        '<a href="notes.html">Notes</a></main>',
        encoding="utf-8",
    )
    review = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=str(required["target_id"]),
        run_id=str(required["run_id"]),
        input_path=fetched,
        content_type="text/html",
        source_url=str(required["url"]),
    )

    assert review["action"] == "review"
    assert review["url"] == parent_url
    link_review = cast("dict[str, object]", review["link_review"])
    documents = cast("list[dict[str, object]]", link_review["documents"])
    assert [item["url"] for item in documents] == [child_url]
    assert fetched_links == [child_url]


def test_manifest_unchanged_transition_preserves_raw_baseline_for_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = tmp_path / "targets.csv"
    _write_targets(targets, "Example,https://example.com/,,true\n")
    blocked = {"value": False}

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        assert url == "https://example.com/"
        if blocked["value"]:
            raise workspace.monitor.HTTPStatusError(
                403, source_url="https://example.com/"
            )
        return workspace.monitor.Document(
            b'<main><a href="https://example.com/release#details">'
            b"Release</a> notes</main>",
            url,
            "text/html",
        )

    def accept_public_source(
        _url: str, *, deadline: float | None = None
    ) -> workspace.monitor._ResolvedTarget | None:
        del deadline
        return None

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    monkeypatch.setattr(workspace.monitor, "_resolve_public_url", accept_public_source)
    target_id = str(load_targets(targets)[0]["target_id"])
    assert (
        cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0][
            "action"
        ]
        == "baseline_created"
    )
    baseline_bytes = (
        tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    ).read_bytes()

    blocked["value"] = True
    required = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    assert required["action"] == "agent_fetch_required"
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("Release notes\n", encoding="utf-8")
    manifest = tmp_path / "navigation-links.json"
    manifest.write_text(
        json.dumps(["https://example.com/release#details"]), encoding="utf-8"
    )

    assert (
        workspace.ingest_agent_fetch(
            tmp_path,
            targets,
            target_id=target_id,
            run_id=str(required["run_id"]),
            input_path=fetched,
            source_url=str(required["url"]),
            links_path=manifest,
        )["action"]
        == "unchanged"
    )
    assert (
        tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    ).read_bytes() == baseline_bytes
    assert workspace.pending_reviews(tmp_path)["reviews"] == []

    required = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    fetched.write_text("Updated release notes\n", encoding="utf-8")
    changed = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=target_id,
        run_id=str(required["run_id"]),
        input_path=fetched,
        source_url=str(required["url"]),
        links_path=manifest,
    )

    assert changed["action"] == "review"
    assert (
        workspace._read_pending(tmp_path / "internal" / "state", target_id)[
            "expected_sha256"
        ]
        == hashlib.sha256(baseline_bytes).hexdigest()
    )
    assert (
        finalize(
            tmp_path,
            {
                "target_id": target_id,
                "revision": changed["revision"],
                "material": True,
                "report": "## Example\n\nRelease notes changed.\n",
            },
        )["action"]
        == "finalized"
    )
    assert (
        tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    ).read_bytes() != baseline_bytes


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.com/?token=secret",
        "https://example.com/?safe=1;api%20key=secret",
        "https://example.com/?next=https%3A%2F%2Fhost.example%2F%3Fsignature%3Dsecret",
        "https://example.com/#access_token=secret",
        "https://hooks.slack.com/services/test/placeholder",
        "https://discord.com/api/webhooks/test/placeholder",
    ],
    ids=[
        "token-query",
        "semicolon-normalized-query",
        "encoded-nested-signature",
        "credential-fragment",
        "slack-webhook",
        "discord-webhook",
    ],
)
def test_manifest_credentials_do_not_modify_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    targets = tmp_path / "targets.csv"
    parent_url = "https://example.com/"
    _write_targets(targets, f"Example,{parent_url},,true\n")
    blocked = {"value": False}

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        assert url == parent_url
        if blocked["value"]:
            raise workspace.monitor.HTTPStatusError(403, source_url=parent_url)
        return workspace.monitor.Document(b"<main>Old</main>", url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    target_id = str(load_targets(targets)[0]["target_id"])
    baseline = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    assert baseline["action"] == "baseline_created"
    snapshot = tmp_path / "internal" / "state" / "snapshots" / f"{target_id}.txt"
    baseline_bytes = snapshot.read_bytes()
    blocked["value"] = True
    required = cast("list[dict[str, object]]", check(tmp_path, targets)["targets"])[0]
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("New content\n", encoding="utf-8")
    manifest = tmp_path / "navigation-links.json"
    manifest.write_text(json.dumps([destination]), encoding="utf-8")

    with pytest.raises(workspace.monitor.MonitorError) as captured:
        workspace.ingest_agent_fetch(
            tmp_path,
            targets,
            target_id=target_id,
            run_id=str(required["run_id"]),
            input_path=fetched,
            source_url=parent_url,
            links_path=manifest,
        )

    assert "secret" not in str(captured.value)
    assert destination not in str(captured.value)
    assert snapshot.read_bytes() == baseline_bytes
    assert workspace.pending_reviews(tmp_path)["reviews"] == []


def test_ingest_agent_fetch_without_link_manifest_marks_evidence_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = tmp_path / "targets.csv"
    _write_targets(targets, "Example,https://example.com/,,true\n")
    blocked = {"value": False}

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        if blocked["value"]:
            raise workspace.monitor.HTTPStatusError(403)
        return workspace.monitor.Document(b"<p>Old release</p>", url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline_outcomes = cast(
        "list[dict[str, object]]", check(tmp_path, targets)["targets"]
    )
    assert baseline_outcomes[0]["action"] == "baseline_created"
    blocked["value"] = True
    required_outcomes = cast(
        "list[dict[str, object]]", check(tmp_path, targets)["targets"]
    )
    required = required_outcomes[0]

    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("New release notes\n", encoding="utf-8")
    review = workspace.ingest_agent_fetch(
        tmp_path,
        targets,
        target_id=str(required["target_id"]),
        run_id=str(required["run_id"]),
        input_path=fetched,
        content_type="text/plain",
    )

    assert review["action"] == "review"
    assert review["link_review"] == {
        "documents": [],
        "omitted": 0,
        "incomplete": True,
    }


@pytest.mark.parametrize("mode", ["inactive", "pending"], ids=["inactive", "pending"])
def test_ingest_agent_fetch_rejects_invalid_state(tmp_path: Path, mode: str) -> None:
    enabled = mode != "inactive"
    _write_targets(
        tmp_path / "targets.csv",
        f"Example,https://example.com/,,{str(enabled).lower()}\n",
    )
    target = load_targets(tmp_path / "targets.csv")[0]
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("content\n", encoding="utf-8")
    if mode == "pending":
        state = tmp_path / "internal" / "state"
        state.mkdir(parents=True)
        _write_review_transaction(state, target_id=str(target["target_id"]))

    with pytest.raises(WorkspaceError, match=r"active|pending"):
        workspace.ingest_agent_fetch(
            tmp_path,
            tmp_path / "targets.csv",
            target_id=str(target["target_id"]),
            run_id=_RUN_ID,
            input_path=fetched,
        )


@pytest.mark.parametrize(
    ("link_depth", "max_links", "message"),
    [
        (-1, workspace._MAX_LINKS, "link_depth"),
        (cast("Any", "1"), workspace._MAX_LINKS, "link_depth"),
        (workspace._DEFAULT_LINK_DEPTH, 0, "max_links"),
        (workspace._DEFAULT_LINK_DEPTH, workspace._MAX_LINKS + 1, "max_links"),
        (workspace._DEFAULT_LINK_DEPTH, cast("Any", "1"), "max_links"),
    ],
)
def test_check_rejects_invalid_link_options(
    tmp_path: Path, link_depth: int, max_links: int, message: str
) -> None:
    _write_targets(tmp_path / "targets.csv", "Example,https://example.com/,,true\n")
    with pytest.raises(WorkspaceError, match=message):
        check(
            tmp_path,
            tmp_path / "targets.csv",
            link_depth=link_depth,
            max_links=max_links,
        )


def test_check_reuses_pending_target_and_fetches_other_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv",
        "Example,https://example.com/,pricing,true\nOther,https://example.org/,,true\n",
    )
    targets = load_targets(tmp_path / "targets.csv")
    target_id = str(targets[0]["target_id"])
    other_id = str(targets[1]["target_id"])
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(
        state, target_id=target_id
    )
    calls = 0

    def fake_monitor(_args: argparse.Namespace) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "status": "unchanged",
            "sha256": "b" * 64,
            "previous_sha256": "b" * 64,
            "diff": "",
            "diff_truncated": False,
        }

    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    result = check(tmp_path, tmp_path / "targets.csv")
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert calls == 1
    assert outcomes[0] == {
        "action": "review",
        "run_id": _RUN_ID,
        "target_id": target_id,
        "revision": "a" * 32,
        "name": "Example",
        "diff_truncated": False,
    }
    assert outcomes[1] == {
        "action": "unchanged",
        "target_id": other_id,
        "name": "Other",
    }
    assert metadata.exists()
    assert candidate.read_text(encoding="utf-8") == "new\n"
    assert snapshot.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    ("targets_csv", "expected_action", "expected_monitor_calls"),
    [
        ("Example,https://example.com/,pricing,false\n", "skipped", 0),
        ("Other,https://example.org/,,true\n", "unchanged", 1),
    ],
    ids=["disabled", "removed"],
)
def test_check_discards_stale_pending_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets_csv: str,
    expected_action: str,
    expected_monitor_calls: int,
) -> None:
    _write_targets(tmp_path / "targets.csv", targets_csv)
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    calls = 0

    def fake_monitor(_args: argparse.Namespace) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "status": "unchanged",
            "sha256": "b" * 64,
            "previous_sha256": "b" * 64,
            "diff": "",
            "diff_truncated": False,
        }

    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    result = check(tmp_path, tmp_path / "targets.csv")
    outcomes = cast("list[dict[str, object]]", result["targets"])

    assert calls == expected_monitor_calls
    assert [str(item["action"]) for item in outcomes] == [expected_action]
    assert not metadata.exists()
    assert not candidate.exists()
    assert snapshot.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    "failure_point",
    ["staged-output-fsync", "pending-candidate-fsync"],
    ids=["staged-output-fsync", "pending-candidate-fsync"],
)
def test_monitor_update_failure_keeps_previous_review_finalizable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    failure_path: Path | None = None
    failed = False
    failure_message = "injected directory fsync failure"

    def fail_once(path: Path) -> None:
        nonlocal failed
        if path == failure_path and not failed:
            failed = True
            raise OSError(failure_message)
        original_fsync(path)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        nonlocal failure_path
        staged = Path(args.output)
        replacement = staged.with_suffix(".replacement")
        replacement.write_text("third\n", encoding="utf-8")
        replacement.replace(staged)
        failure_path = (
            staged.parent
            if failure_point == "staged-output-fsync"
            else candidate.parent
        )
        workspace._fsync_directory(  # pyright: ignore[reportPrivateUsage]
            staged.parent
        )
        return _changed_result(current="third\n")

    monkeypatch.setattr(workspace, "_fsync_directory", fail_once)
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    with pytest.raises((OSError, WorkspaceError), match="fsync"):
        workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]

    assert failed
    assert metadata.exists()
    assert candidate.read_text(encoding="utf-8") == "new\n"

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert not metadata.exists()
    assert not candidate.exists()


def test_staging_cleanup_failure_does_not_commit_monitor_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    original_metadata = metadata.read_bytes()
    cleanup_error = "injected staging cleanup failure"
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }

    class FailingTemporaryDirectory:
        def __init__(self, *, prefix: str, **kwargs: str | Path) -> None:
            self.path = Path(kwargs["dir"]) / f"{prefix}cleanup-failure"
            self.path.mkdir(mode=0o700)

        def __enter__(self) -> str:
            return str(self.path)

        def __exit__(
            self, _exc_type: object, _exc_value: object, _traceback: object
        ) -> None:
            workspace.shutil.rmtree(self.path)
            raise OSError(cleanup_error)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("third\n", encoding="utf-8")
        return _changed_result(current="third\n")

    monkeypatch.setattr(
        workspace.tempfile, "TemporaryDirectory", FailingTemporaryDirectory
    )
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    with pytest.raises(OSError, match=cleanup_error):
        workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]

    assert metadata.read_bytes() == original_metadata
    assert candidate.read_text(encoding="utf-8") == "new\n"
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"


def test_failed_commit_marker_keeps_undo_available_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, _ = _write_review_transaction(state)
    recovery_dir = state / "recovery"
    commit_marker = recovery_dir / "example.json.commit"
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    original_write = workspace._write_temporary_file  # pyright: ignore[reportPrivateUsage]
    commit_sync_failed = False
    rollback_write_failed = False
    commit_sync_error = "injected commit marker fsync failure"
    rollback_write_error = "injected rollback file creation failure"

    def fail_commit_sync(path: Path) -> None:
        nonlocal commit_sync_failed
        if path == recovery_dir and commit_marker.exists() and not commit_sync_failed:
            commit_sync_failed = True
            raise OSError(commit_sync_error)
        original_fsync(path)

    def fail_rollback_write(destination: Path, data: bytes, description: str) -> Path:
        nonlocal rollback_write_failed
        if (
            commit_sync_failed
            and destination == candidate
            and data == b"new\n"
            and not rollback_write_failed
        ):
            rollback_write_failed = True
            raise OSError(rollback_write_error)
        return original_write(destination, data, description)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_commit_sync)
    monkeypatch.setattr(workspace, "_write_temporary_file", fail_rollback_write)

    payload = _pending_payload(
        revision="b" * 32,
        expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
        candidate_sha256=hashlib.sha256(b"third\n").hexdigest(),
    )
    with pytest.raises(WorkspaceError, match="rollback could not be completed"):
        workspace._write_pending_transaction(  # pyright: ignore[reportPrivateUsage]
            state, payload, b"third\n"
        )

    assert commit_sync_failed
    assert rollback_write_failed
    assert metadata.exists()
    assert commit_marker.exists() is False
    assert (recovery_dir / "example.json").exists()

    monkeypatch.undo()
    workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    assert json.loads(metadata.read_text(encoding="utf-8"))["revision"] == "a" * 32
    assert candidate.read_text(encoding="utf-8") == "new\n"
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"


def _leave_ambiguous_replacement(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _pending_payload(
        revision="b" * 32,
        expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
        candidate_sha256=hashlib.sha256(b"third\n").hexdigest(),
    )
    retirement_error = "injected undo retirement failure"

    def fail_undo_retirement(_state: Path, _target_id: str) -> None:
        raise WorkspaceError(retirement_error)

    monkeypatch.setattr(workspace, "_retire_recovery_record", fail_undo_retirement)
    workspace._write_pending_transaction(  # pyright: ignore[reportPrivateUsage]
        state, payload, b"third\n"
    )


@pytest.mark.parametrize(
    ("revision", "expected_snapshot"),
    [("a" * 32, "new\n"), ("b" * 32, "third\n")],
    ids=["retain-previous-review", "accept-committed-review"],
)
def test_finalize_resolves_ambiguous_commit_by_requested_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
    expected_snapshot: str,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _, _, snapshot = _write_review_transaction(state)
    _leave_ambiguous_replacement(state, monkeypatch)
    assert (state / "recovery" / "example.json").exists()
    assert (state / "recovery" / "example.json.commit").exists()
    with pytest.raises(WorkspaceError, match="requires a decision revision"):
        workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )

    monkeypatch.undo()
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": revision, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == expected_snapshot


def test_finalize_unrelated_revision_preserves_ambiguous_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    _leave_ambiguous_replacement(state, monkeypatch)
    monkeypatch.undo()
    original_metadata = metadata.read_bytes()
    original_candidate = candidate.read_bytes()
    recovery_dir = state / "recovery"

    with pytest.raises(WorkspaceError, match="does not match pending replacement"):
        finalize(
            tmp_path,
            {"target_id": "example", "revision": "c" * 32, "material": False},
        )

    assert snapshot.read_text(encoding="utf-8") == "old\n"
    assert metadata.read_bytes() == original_metadata
    assert candidate.read_bytes() == original_candidate
    assert (recovery_dir / "example.json").exists()
    assert (recovery_dir / "example.json.commit").exists()

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "b" * 32, "material": False},
    )
    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "third\n"


def test_finalize_rejects_symlinked_pending_ancestor_with_ambiguous_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_review_transaction(state)
    _leave_ambiguous_replacement(state, monkeypatch)
    monkeypatch.undo()

    target_dir = state / "pending" / "example"
    moved_target = state / "pending" / "example-moved"
    target_dir.replace(moved_target)
    target_dir.symlink_to(moved_target, target_is_directory=True)
    recovery_dir = state / "recovery"

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        finalize(
            tmp_path,
            {"target_id": "example", "revision": "b" * 32, "material": False},
        )

    assert (recovery_dir / "example.json").exists()
    assert (recovery_dir / "example.json.commit").exists()
    target_dir.unlink()
    moved_target.replace(target_dir)
    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )
    assert result["action"] == "finalized"


def test_finalize_recovers_interrupted_pending_replacement(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)
    undo = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, undo
    )
    candidate.write_text("uncommitted replacement\n", encoding="utf-8")

    result = finalize(
        tmp_path,
        {"target_id": "example", "revision": "a" * 32, "material": False},
    )

    assert result["action"] == "finalized"
    assert snapshot.read_text(encoding="utf-8") == "new\n"
    assert not metadata.exists()
    assert not candidate.exists()
    assert not (state / "recovery" / "example.json").exists()


def test_recovery_removes_temporaries_from_interrupted_initial_write(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    undo = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, undo
    )
    target_dir = state / "pending" / "example"
    target_dir.mkdir()
    (target_dir / ".candidate.txt.crash.tmp").write_text("partial", encoding="utf-8")
    (target_dir / ".state.json.crash.tmp").write_text("partial", encoding="utf-8")

    workspace._recover_pending(state, "example")  # pyright: ignore[reportPrivateUsage]

    assert not target_dir.exists()
    assert not (state / "recovery" / "example.json").exists()


@pytest.mark.parametrize(
    ("unsafe_record", "message"),
    [
        ("symlink", "non-symlink"),
        ("malformed", "invalid"),
        ("permissive-mode", "private"),
    ],
    ids=["symlink", "malformed", "permissive-mode"],
)
def test_pending_recovery_rejects_unsafe_record(
    tmp_path: Path, unsafe_record: str, message: str
) -> None:
    state = tmp_path / "internal" / "state"
    recovery_dir = state / "recovery"
    recovery_dir.mkdir(parents=True, mode=0o700)
    record = recovery_dir / "example.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    if unsafe_record == "symlink":
        record.symlink_to(outside)
    elif unsafe_record == "malformed":
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o600)
    else:
        record.write_text("{}", encoding="utf-8")
        record.chmod(0o644)

    with pytest.raises(WorkspaceError, match=message):
        workspace._recover_pending(  # pyright: ignore[reportPrivateUsage]
            state, "example"
        )


def test_finalize_rejects_decision_mismatch_for_pending_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, _ = _write_review_transaction(state)
    decision = {
        "target_id": "example",
        "revision": "a" * 32,
        "material": True,
        "report": "## Example\n\nOriginal decision.\n",
    }

    error_message = "injected cleanup failure"

    def fail_cleanup(_state: Path, _target_id: str) -> None:
        raise WorkspaceError(error_message)

    monkeypatch.setattr(workspace, "_remove_pending", fail_cleanup)
    with pytest.raises(WorkspaceError, match=error_message):
        finalize(tmp_path, decision)

    with pytest.raises(WorkspaceError, match="does not match pending cleanup"):
        finalize(
            tmp_path,
            {
                "target_id": "example",
                "revision": "a" * 32,
                "material": False,
            },
        )

    assert metadata.exists()
    assert candidate.exists()
    assert (state / "recovery" / "example.json").exists()

    monkeypatch.undo()
    result = finalize(tmp_path, decision)

    assert result["action"] == "finalized"
    assert not metadata.exists()
    assert not candidate.exists()


def test_check_syncs_new_state_and_grouped_pending_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,pricing,true\n"
    )
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    fsynced: list[Path] = []

    def record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync(path)

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("new\n", encoding="utf-8")
        return _changed_result(current="new\n")

    monkeypatch.setattr(workspace, "_fsync_directory", record_fsync)
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    result = check(tmp_path, tmp_path / "targets.csv")

    target_id = str(load_targets(tmp_path / "targets.csv")[0]["target_id"])
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    assert result["targets"][0]["action"] == "review"  # type: ignore[index]
    assert tmp_path in fsynced
    assert state in fsynced
    assert pending in fsynced
    assert pending / target_id in fsynced
    assert state / "recovery" in fsynced
    assert (pending / target_id / "state.json").exists()


def test_check_compact_returns_handles_and_pending_returns_full_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,pricing,true\n"
    )
    state = tmp_path / "internal" / "state"
    snapshots = state / "snapshots"
    snapshots.mkdir(parents=True)
    target_id = str(load_targets(tmp_path / "targets.csv")[0]["target_id"])
    (snapshots / f"{target_id}.txt").write_text("old\n", encoding="utf-8")

    def fake_monitor(args: argparse.Namespace) -> dict[str, object]:
        Path(args.output).write_text("new\n", encoding="utf-8")
        return _changed_result()

    monkeypatch.setattr(workspace, "_new_run_id", lambda: _RUN_ID)
    monkeypatch.setattr(workspace.monitor, "run", fake_monitor)

    result = check(tmp_path, tmp_path / "targets.csv", compact=True)
    reviews = [
        item
        for item in cast("list[dict[str, object]]", result["targets"])
        if item["action"] == "review"
    ]
    assert reviews == [
        {
            "action": "review",
            "run_id": _RUN_ID,
            "target_id": target_id,
            "revision": reviews[0]["revision"],
            "name": "Example",
            "diff_truncated": False,
        }
    ]
    assert "diff" not in reviews[0]

    pending = workspace.pending_reviews(
        tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
    )
    full = cast("list[dict[str, object]]", pending["reviews"])[0]
    assert full["run_id"] == _RUN_ID
    assert full["target_id"] == target_id
    assert full["revision"] == reviews[0]["revision"]
    assert full["name"] == "Example"
    assert full["url"] == "https://example.com/"
    assert full["diff"] == _changed_result()["diff"]

    _write_targets(
        tmp_path / "targets.csv",
        "Renamed,https://example.com/,security,true\n",
    )

    resumed = check(tmp_path, tmp_path / "targets.csv", compact=True)
    resumed_reviews = [
        item
        for item in cast("list[dict[str, object]]", resumed["targets"])
        if item["action"] == "review"
    ]
    assert resumed_reviews == [
        {
            "action": "review",
            "run_id": _RUN_ID,
            "target_id": target_id,
            "revision": reviews[0]["revision"],
            "name": "Renamed",
            "diff_truncated": False,
        }
    ]

    refreshed = workspace.pending_reviews(
        tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
    )
    refreshed_full = cast("list[dict[str, object]]", refreshed["reviews"])[0]
    assert refreshed_full["revision"] == reviews[0]["revision"]
    assert refreshed_full["run_id"] == _RUN_ID
    assert refreshed_full["name"] == "Renamed"
    assert refreshed_full["url"] == "https://example.com/"
    assert refreshed_full["diff"] == _changed_result()["diff"]


def test_pending_reviews_recovers_partial_replacement_before_listing(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    pending = state / "pending"
    pending.mkdir()
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state,
        _replace_record(),
    )
    group = pending / "example"
    group.mkdir()
    (group / "candidate.txt").write_text("partial\n", encoding="utf-8")

    assert workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv") == {
        "reviews": []
    }
    assert not group.exists()
    assert (
        workspace._read_recovery_record(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )
        is None
    )


def test_discard_pending_clears_conflicted_review(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, candidate, snapshot = _write_review_transaction(state)

    result = workspace.discard_pending(tmp_path, "example")

    assert result == {
        "action": "discarded",
        "run_id": _RUN_ID,
        "target_id": "example",
    }
    assert not metadata.exists()
    assert not candidate.exists()
    assert snapshot.read_text(encoding="utf-8") == "old\n"
    assert workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv") == {
        "reviews": []
    }


def test_pending_target_listing_rejects_hidden_unsafe_entry(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    pending.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (pending / ".unsafe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="unsafe entry"):
        workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")


def test_handle_monitor_result_rejects_non_string_diff(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    target = {
        "target_id": "example",
        "name": "Example",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
    }
    result = _changed_result()
    result["diff"] = 1

    with pytest.raises(WorkspaceError, match="invalid diff"):
        workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
            state,
            target,
            result,
            _RUN_ID,
            candidate_data=b"new\n",
        )


@pytest.mark.parametrize(
    "fault",
    [
        "pending-stat",
        "pending-symlink",
        "list",
        "entry-stat",
        "hidden-temp",
        "unexpected-file",
    ],
)
def test_pending_listing_filesystem_edges(  # ruff: ignore[complex-structure]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    pending = state / "pending"
    original_lstat = Path.lstat
    original_iterdir = Path.iterdir

    if fault == "pending-symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        pending.symlink_to(outside, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="non-symlink"):
            workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")
        return

    pending.mkdir()
    if fault == "pending-stat":

        def fail_pending(path: Path) -> os.stat_result:
            if path == pending:
                message = "injected"
                raise PermissionError(message)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_pending)
        with pytest.raises(WorkspaceError, match="pending directory is unavailable"):
            workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")
        return

    if fault == "list":

        def fail_list(path: Path) -> Any:  # ruff: ignore[any-type]
            if path == pending:
                message = "injected"
                raise PermissionError(message)
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_list)
        with pytest.raises(WorkspaceError, match="cannot list pending directory"):
            workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")
        return

    entry = pending / (".orphan.tmp" if fault == "hidden-temp" else "unexpected.txt")
    entry.write_text("x", encoding="utf-8")
    if fault == "entry-stat":

        def fail_entry(path: Path) -> os.stat_result:
            if path == entry:
                message = "injected"
                raise PermissionError(message)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_entry)
        with pytest.raises(WorkspaceError, match="cannot stat pending entry"):
            workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")
        return
    if fault == "hidden-temp":
        assert workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv"
        ) == {"reviews": []}
        return
    with pytest.raises(WorkspaceError, match="unsupported entry"):
        workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv")


def test_pending_reviews_grouped_without_targets_uses_persisted_context(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    metadata, _, _ = _write_review_transaction(state)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload.update({
        "name": "Persisted",
        "url": "https://example.com/",
        "interests": [
            {
                "name": "Persisted",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
        "diff": "persisted diff",
    })
    metadata.write_text(json.dumps(payload), encoding="utf-8")

    listed = workspace.pending_reviews(tmp_path)
    handle = cast("list[dict[str, object]]", listed["reviews"])[0]
    assert handle["name"] == "Persisted"

    result = workspace.pending_reviews(tmp_path, target_id="example")
    review = cast("list[dict[str, object]]", result["reviews"])[0]
    assert review["name"] == "Persisted"
    assert review["url"] == "https://example.com/"
    assert (
        cast("list[dict[str, object]]", review["interests"])[0]["criteria"] == "pricing"
    )
    assert review["diff"] == "persisted diff"


def test_pending_review_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _, candidate, _ = _write_review_transaction(state)
    candidate.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="candidate_sha256"):
        workspace.pending_reviews(tmp_path, target_id="example")


def test_pending_reviews_rejects_unknown_target_id(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="no valid pending"):
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id="example"
        )


def test_main_discard_command_emits_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_review_transaction(state)

    assert (
        workspace.main([
            "--workspace",
            str(tmp_path),
            "discard",
            "--target-id",
            "example",
        ])
        == 0
    )
    captured = capsys.readouterr()
    assert '"action": "discarded"' in captured.out
    assert not captured.err


def test_recovery_directory_parent_fsync_retries_after_creation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_review_transaction(state)
    record = workspace._capture_pending_replacement(  # pyright: ignore[reportPrivateUsage]
        state, "example"
    )
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]
    state_fsyncs = 0
    parent_fsync_error = "injected recovery parent fsync failure"

    def fail_once(path: Path) -> None:
        nonlocal state_fsyncs
        if path == state:
            state_fsyncs += 1
            if state_fsyncs == 1:
                raise OSError(parent_fsync_error)
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_once)
    with pytest.raises(WorkspaceError, match="fsync parent directory"):
        workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
            state, record
        )

    recovery_dir = state / "recovery"
    assert recovery_dir.is_dir()
    assert not (recovery_dir / "example.json").exists()
    workspace._write_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, record
    )

    assert state_fsyncs == 2
    assert (recovery_dir / "example.json").is_file()


def test_main_requires_targets_and_reports_invalid_workspace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert workspace.main(["--workspace", "/missing", "check"]) == 2
    assert json.loads(capsys.readouterr().err) == {
        "error": "--targets is required for check"
    }

    assert (
        workspace.main([
            "--workspace",
            "/missing",
            "--targets",
            "/missing/targets.csv",
            "check",
        ])
        == 2
    )
    assert "workspace must be an existing directory" in capsys.readouterr().err


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    return state


def _candidate(state: Path, content: str = "next\n") -> tuple[Path, str]:
    directory = state / "pending" / "example"
    directory.mkdir(parents=True)
    path = directory / "candidate.txt"
    path.write_text(content)
    return path, hashlib.sha256(content.encode()).hexdigest()


def _promote(
    state: Path,
    digest: str,
    *,
    expected: str | None = None,
) -> dict[str, object]:
    return workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256=expected,
        candidate_sha256=digest,
    )


def test_write_report_creates_private_run_report(tmp_path: Path) -> None:
    report = "## Example\n\nA material update.\n"

    destination = workspace._write_report(tmp_path, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]

    reports = tmp_path / "output" / "report"
    content = destination.read_text()
    assert destination == reports / f"{_RUN_ID}.md"
    assert f"Run: `{_RUN_ID}`" in content
    assert report.strip() in content
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_report_aggregates_targets_into_one_run_file(tmp_path: Path) -> None:
    workspace._write_report(tmp_path, _RUN_ID, "one", "## One\n\nFirst.\n")  # pyright: ignore[reportPrivateUsage]
    workspace._write_report(tmp_path, _RUN_ID, "two", "## Two\n\nSecond.\n")  # pyright: ignore[reportPrivateUsage]

    reports = list((tmp_path / "output" / "report").glob("*.md"))
    assert reports == [tmp_path / "output" / "report" / f"{_RUN_ID}.md"]
    content = reports[0].read_text()
    assert "## One" in content
    assert "## Two" in content


def test_write_report_replaces_existing_target_section(tmp_path: Path) -> None:
    destination = workspace._write_report(  # pyright: ignore[reportPrivateUsage]
        tmp_path, _RUN_ID, "example", "## Example\n\nOld report.\n"
    )

    workspace._write_report(tmp_path, _RUN_ID, "example", "## Example\n\nNew report.\n")  # pyright: ignore[reportPrivateUsage]

    content = destination.read_text()
    assert "Old report." not in content
    assert content.count("New report.") == 1
    assert content.count("<!-- wsum:target example:start -->") == 1


@pytest.mark.parametrize(
    "target_id",
    ["../escape", "/absolute", "nested/path"],
    ids=["traversal", "absolute", "separator"],
)
def test_write_report_rejects_invalid_target_id(tmp_path: Path, target_id: str) -> None:
    with pytest.raises(WorkspaceError, match="invalid_target_id"):
        workspace._write_report(tmp_path, _RUN_ID, target_id, "update\n")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "20260101T000000Z-DEADBEEF", "not-a-run"],
    ids=["traversal", "uppercase-suffix", "malformed"],
)
def test_write_report_rejects_invalid_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(WorkspaceError, match="invalid_run_id"):
        workspace._write_report(tmp_path, run_id, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "output" / "report").exists()


def test_write_report_rejects_reserved_section_markers(tmp_path: Path) -> None:
    report = "<!-- wsum:target other:start -->\nInjected section\n"

    with pytest.raises(WorkspaceError, match="reserved marker"):
        workspace._write_report(tmp_path, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "output" / "report" / f"{_RUN_ID}.md").exists()


def _symlink_workspace_root_for_report(
    tmp_path: Path, outside: Path
) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace-link"
    workspace_root.symlink_to(outside, target_is_directory=True)
    return workspace_root, outside / "output" / "report" / f"{_RUN_ID}.md"


def _symlink_reports_directory_for_report(
    tmp_path: Path, outside: Path
) -> tuple[Path, Path]:
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report").symlink_to(outside, target_is_directory=True)
    return tmp_path, outside / f"{_RUN_ID}.md"


@pytest.mark.parametrize(
    "setup",
    [_symlink_workspace_root_for_report, _symlink_reports_directory_for_report],
    ids=["workspace-root", "reports-directory"],
)
def test_write_report_rejects_symlinked_workspace_paths(
    tmp_path: Path,
    setup: Callable[[Path, Path], tuple[Path, Path]],
) -> None:
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    workspace_root, outside_report = setup(tmp_path, outside)

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        workspace._write_report(workspace_root, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert not outside_report.exists()


def test_write_report_rejects_symlinked_destination(tmp_path: Path) -> None:
    reports = tmp_path / "output" / "report"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("original\n")
    destination = reports / f"{_RUN_ID}.md"
    destination.symlink_to(outside)

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert destination.is_symlink()
    assert outside.read_text() == "original\n"


def test_write_report_preserves_destination_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "output" / "report"
    reports.mkdir(parents=True)
    destination = reports / f"{_RUN_ID}.md"
    destination.write_text("old report\n")

    def fail_replace(_: Path, __: Path) -> Path:
        raise OSError

    monkeypatch.setattr(workspace.Path, "replace", fail_replace)
    with pytest.raises(WorkspaceError, match="cannot write report"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "new report\n")  # pyright: ignore[reportPrivateUsage]

    assert destination.read_text() == "old report\n"
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_write_report_fsyncs_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    assert fsynced == [
        tmp_path,
        tmp_path / "output",
        tmp_path / "output" / "report",
    ]


def test_write_report_reports_fsync_failure_after_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "output" / "report"
    reports.mkdir(parents=True)

    def fail(path: Path) -> None:
        if path == reports:
            raise OSError

    monkeypatch.setattr(workspace, "_fsync_directory", fail)
    with pytest.raises(WorkspaceError, match="cannot fsync report directory"):
        workspace._write_report(tmp_path, _RUN_ID, "example", "update\n")  # pyright: ignore[reportPrivateUsage]

    content = (reports / f"{_RUN_ID}.md").read_text()
    assert f"Run: `{_RUN_ID}`" in content
    assert "update" in content
    assert not list(reports.glob(f".{_RUN_ID}.md.*.tmp"))


def test_promote_snapshot_rejects_symlinked_state_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    internal = tmp_path / "internal"
    internal.mkdir()
    state = internal / "state"
    state.symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, digest)

    assert not (outside / "pending").exists()
    assert not (outside / "snapshots").exists()


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        (None, None),
        ("old\n", hashlib.sha256(b"old\n").hexdigest()),
    ],
    ids=["create-baseline", "replace-matching-baseline"],
)
def test_promote_snapshot_creates_or_replaces_baseline(
    tmp_path: Path, baseline: str | None, expected: str | None
) -> None:
    state = _state(tmp_path)
    if baseline is not None:
        snapshots = state / "snapshots"
        snapshots.mkdir()
        (snapshots / "example.txt").write_text(baseline, encoding="utf-8")
    _, digest = _candidate(state)

    result = _promote(state, digest, expected=expected)

    snapshot = state / "snapshots" / "example.txt"
    assert result["action"] == "snapshot_promoted"
    assert result["applied"] is True
    assert result["sha256"] == digest
    assert snapshot.read_text() == "next\n"


def test_promote_snapshot_reports_stale_baseline(tmp_path: Path) -> None:
    state = _state(tmp_path)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    current = b"current\n"
    (snapshots / "example.txt").write_bytes(current)
    _, digest = _candidate(state)

    result = _promote(state, digest, expected="0" * 64)

    assert result == {
        "action": "snapshot_conflict",
        "applied": False,
        "current_sha256": hashlib.sha256(current).hexdigest(),
    }
    assert (snapshots / "example.txt").read_bytes() == current


def test_promote_snapshot_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _candidate(state)

    with pytest.raises(WorkspaceError, match="does not match"):
        _promote(state, "0" * 64)


def test_promote_snapshot_requires_pending_candidate(tmp_path: Path) -> None:
    state = _state(tmp_path)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="pending decision"):
        _promote(state, digest)


def test_promote_snapshot_rejects_symlink_candidate(tmp_path: Path) -> None:
    state = _state(tmp_path)
    pending = state / "pending" / "example"
    pending.mkdir(parents=True)
    target = tmp_path / "outside-candidate.txt"
    target.write_text("next\n")
    (pending / "candidate.txt").symlink_to(target)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        _promote(state, digest)


def _symlink_pending_directory(state: Path, outside: Path) -> None:
    (state / "pending").symlink_to(outside, target_is_directory=True)


def _symlink_snapshot_directory(state: Path, outside: Path) -> None:
    _candidate(state)
    (state / "snapshots").symlink_to(outside, target_is_directory=True)


@pytest.mark.parametrize(
    ("setup", "outside_name"),
    [
        (_symlink_pending_directory, "outside-pending"),
        (_symlink_snapshot_directory, "outside-snapshots"),
    ],
    ids=["pending", "snapshots"],
)
def test_promote_snapshot_rejects_symlinked_state_directories(
    tmp_path: Path, setup: Callable[[Path, Path], None], outside_name: str
) -> None:
    state = _state(tmp_path)
    outside = tmp_path / outside_name
    outside.mkdir()
    setup(state, outside)
    digest = hashlib.sha256(b"next\n").hexdigest()

    with pytest.raises(WorkspaceError, match="non-symlink directory"):
        _promote(state, digest)
    assert not (outside / "example.txt").exists()


@pytest.mark.parametrize(
    "snapshot_directory_exists", [True, False], ids=["existing", "created"]
)
def test_promote_snapshot_fsyncs_snapshot_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_directory_exists: bool,
) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)
    if snapshot_directory_exists:
        (state / "snapshots").mkdir()
    fsynced: list[Path] = []
    monkeypatch.setattr(workspace, "_fsync_directory", fsynced.append)

    result = _promote(state, digest)

    assert result["action"] == "snapshot_promoted"
    expected = [state, state, state / "snapshots"]
    assert fsynced == expected


def test_promote_snapshot_reports_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)
    snapshots = state / "snapshots"
    snapshots.mkdir()
    original_fsync = workspace._fsync_directory  # pyright: ignore[reportPrivateUsage]

    def fail(path: Path) -> None:
        if path == snapshots:
            raise OSError
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail)
    with pytest.raises(WorkspaceError, match="cannot fsync snapshot directory"):
        _promote(state, digest)
    assert (state / "snapshots" / "example.txt").read_text() == "next\n"


def test_promote_snapshot_retries_idempotently(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _, digest = _candidate(state)

    _promote(state, digest)
    result = _promote(state, digest)

    assert result == {
        "action": "snapshot_promoted",
        "applied": True,
        "already": True,
        "path": str(state / "snapshots" / "example.txt"),
        "sha256": digest,
    }


_REVISION = "a" * 32


def _pending_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_sha256": "b" * 64,
        "diff": "diff",
        "diff_truncated": False,
        "expected_sha256": None,
        "interests": [
            {
                "name": "Example",
                "publisher": "",
                "category": "",
                "keywords": "",
                "criteria": "pricing",
                "priority": None,
                "enabled": True,
            }
        ],
        "name": "Example",
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "target_id": "example",
        "url": "https://example.com/",
    }
    payload.update(changes)
    return payload


def _replace_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "group_dir_existed": False,
        "kind": "replace",
        "old_candidate": None,
        "old_state": None,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


def _commit_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "commit",
        "revision": _REVISION,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


def _cleanup_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "cleanup",
        "material": False,
        "purpose": "finalize",
        "report_sha256": None,
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "target_id": "example",
        "version": 1,
    }
    record.update(changes)
    return record


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "existing directory"),
        ("file", "non-symlink directory"),
        ("symlink", "non-symlink directory"),
    ],
    ids=["missing", "file", "symlink"],
)
def test_workspace_rejects_missing_and_non_directory_roots(
    tmp_path: Path, case: str, message: str
) -> None:
    path = tmp_path / "workspace"
    if case == "file":
        path.write_text("file", encoding="utf-8")
    elif case == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError, match=message):
        workspace._workspace(path)  # pyright: ignore[reportPrivateUsage]


def test_directory_fsync_is_skipped_when_platform_has_no_directory_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(workspace.os, "O_DIRECTORY", raising=False)
    workspace._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure",
    ["mkdir", "restat", "parent-fsync", "lstat"],
    ids=["mkdir", "restat", "parent-fsync", "lstat"],
)
def test_ensure_directory_wraps_filesystem_failures(  # ruff: ignore[complex-structure]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "state"
    if failure == "mkdir":
        original_lstat = Path.lstat

        def missing(target: Path) -> os.stat_result:
            if target == path:
                raise FileNotFoundError
            return original_lstat(target)

        def fail_mkdir(_target: Path, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
            raise OSError("mkdir failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        monkeypatch.setattr(Path, "lstat", missing)
        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    elif failure == "restat":
        original_lstat = Path.lstat
        calls = 0

        def fail_restat(target: Path) -> os.stat_result:
            nonlocal calls
            if target == path:
                calls += 1
                if calls == 1:
                    raise FileNotFoundError
                raise OSError("restat failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_restat)
        original_mkdir = Path.mkdir

        def create(target: Path, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
            if target == path:
                original_mkdir(target, *args, **kwargs)
            else:
                original_mkdir(target, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", create)
    elif failure == "parent-fsync":
        path.mkdir()
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _target: (_ for _ in ()).throw(OSError("sync failed")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_lstat = Path.lstat

        def fail_lstat(target: Path) -> os.stat_result:
            if target == path:
                raise PermissionError("cannot stat")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(WorkspaceError, match="unavailable|fsync parent"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
        workspace._ensure_directory(  # pyright: ignore[reportPrivateUsage]
            path, "test directory", sync_parent=failure == "parent-fsync"
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "missing"),
        (b"", "size"),
        (b"\xff", "UTF-8"),
        (b"x" * (1024 * 1024 + 1), "size"),
    ],
    ids=["missing", "empty", "invalid-utf8", "oversized"],
)
def test_read_csv_rejects_missing_empty_invalid_and_oversized_files(
    tmp_path: Path, contents: bytes | None, message: str
) -> None:
    path = tmp_path / "targets.csv"
    if contents is not None:
        path.write_bytes(contents)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_csv(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("name,name,url\nA,A,https://example.com/\n", "unique header"),
        ("name,url\nA,https://example.com/,extra\n", "too many columns"),
        ("name,url\n,\n", "contains no targets"),
        ("name,url\n\nA,https://example.com/\n", ""),
    ],
    ids=["duplicate-header", "too-many-columns", "no-targets", "blank-row"],
)
def test_load_targets_handles_header_rows_and_empty_records(
    tmp_path: Path, csv_text: str, message: str
) -> None:
    if not message:
        csv_text = "name,url,enabled\n\nA,https://example.com/,\n"
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    if message:
        with pytest.raises(WorkspaceError, match=message):
            workspace.load_targets(tmp_path / "targets.csv")
    else:
        result = workspace.load_targets(tmp_path / "targets.csv")
        assert len(result) == 1
        assert result[0]["enabled"] is True


def test_target_id_falls_back_for_malformed_urlsplit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace,
        "urlsplit",
        lambda _url: (_ for _ in ()).throw(ValueError("bad URL")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    target_id = workspace._target_id("not-a-url")  # pyright: ignore[reportPrivateUsage]
    assert target_id.startswith("target-")


@pytest.mark.parametrize(
    ("value", "validator", "message"),
    [
        (None, "target", "invalid_target_id"),
        ("../x", "target", "invalid_target_id"),
        ("bad", "run", "invalid_run_id"),
        ("A" * 64, "sha", "lowercase hexadecimal"),
    ],
    ids=["non-string-target", "unsafe-target", "run-id", "uppercase-sha"],
)
def test_identifier_validators_reject_malformed_values(
    value: object, validator: str, message: str
) -> None:
    function = {  # pyright: ignore[reportUnknownVariableType]
        "target": workspace._validate_target_id,
        "run": workspace._validate_run_id,
        "sha": lambda item: workspace._validate_sha256(item, "digest"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    }[validator]
    with pytest.raises(WorkspaceError, match=message):
        function(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        ("name,url\n", "A,http://[::1\n", "url is invalid"),
        ("name,url\n", "A,https://example.com/#frag\n", "fragment"),
    ],
    ids=["malformed-url", "url-fragment"],
)
def test_load_targets_rejects_duplicate_and_malformed_rows(
    tmp_path: Path, header: str, rows: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(header + rows, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        workspace.load_targets(tmp_path / "targets.csv")


def test_pending_path_resolver_uses_current_grouped_layout(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    pending.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="no valid pending"):
        workspace._pending_paths(state, "example")

    metadata, candidate = workspace._pending_paths(state, "example", create=True)
    assert metadata == pending / "example" / "state.json"
    assert candidate == pending / "example" / "candidate.txt"
    assert workspace._existing_pending_paths(state, "example") == (metadata, candidate)


def test_pending_target_listing_rejects_removed_flat_layout(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    pending.mkdir(parents=True)
    (pending / "example.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="unsupported entry"):
        workspace._pending_target_ids(state)


@pytest.mark.parametrize(
    "mutate",
    [
        "odd-header",
        "start-only",
        "end-before-start",
        "duplicate-start",
        "duplicate-end",
    ],
    ids=[
        "reserved-marker",
        "unbalanced",
        "reversed",
        "duplicate-start",
        "duplicate-end",
    ],
)
def test_report_renderer_rejects_invalid_managed_sections(mutate: str) -> None:
    marker = "<!-- wsum:target example"
    existing = {
        "odd-header": "old report",
        "start-only": f"{marker}:start -->\nsection\n",
        "end-before-start": f"{marker}:end -->\n{marker}:start -->\n",
        "duplicate-start": (
            f"{marker}:start -->\na\n{marker}:end -->\n{marker}:start -->\n"
        ),
        "duplicate-end": f"{marker}:start -->\na\n{marker}:end -->\n{marker}:end -->\n",
    }[mutate]
    report = f"Injected {marker}" if mutate == "odd-header" else "new report"
    message = "reserved marker" if mutate == "odd-header" else "managed section"
    with pytest.raises(WorkspaceError, match=message):
        workspace._render_run_report(  # pyright: ignore[reportPrivateUsage]
            _RUN_ID, "example", report, existing.encode()
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [(b"\xff", "UTF-8"), (b"", "size"), (b"x" * (40 * 1024 * 1024 + 1), "size")],
    ids=["invalid-utf8", "empty", "oversized"],
)
def test_read_text_bytes_rejects_invalid_content(
    tmp_path: Path, contents: bytes, message: str
) -> None:
    path = tmp_path / "content.txt"
    path.write_bytes(contents)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_text_bytes(path, "test content")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "record",
    [
        None,
        _replace_record(version=True),
        _replace_record(target_id="other"),
        _replace_record(extra="x"),
        _replace_record(group_dir_existed="yes"),
        _replace_record(old_state=1),
        _replace_record(old_state="%%%"),
        _commit_record(extra="x"),
        _commit_record(revision="bad"),
        _cleanup_record(extra="x"),
        _cleanup_record(purpose="unknown"),
        _cleanup_record(purpose="discard", revision=_REVISION),
        _cleanup_record(revision="bad"),
        _cleanup_record(material="yes"),
        _cleanup_record(run_id="bad"),
        _cleanup_record(material=True, report_sha256="bad"),
        _cleanup_record(report_sha256="a" * 64),
        {"kind": "other", "target_id": "example", "version": 1},
    ],
)
def test_recovery_record_validation_rejects_malformed_shapes(record: object) -> None:
    with pytest.raises(WorkspaceError, match="invalid"):
        workspace._validate_recovery_record(record, "example")


def test_recovery_backup_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1)
    encoded = base64.b64encode(b"too long").decode()
    with pytest.raises(WorkspaceError, match="too large"):
        workspace._decode_recovery_backup(encoded)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "record",
    [
        _cleanup_record(
            purpose="discard",
            material=None,
            report_sha256=None,
            revision=None,
            run_id=None,
        ),
        _cleanup_record(material=True, report_sha256="b" * 64),
        _replace_record(old_state=base64.b64encode(b"old").decode()),
        _commit_record(),
    ],
    ids=["valid-discard", "valid-finalize", "valid-replace", "valid-commit"],
)
def test_recovery_record_validation_accepts_supported_records(
    record: dict[str, object],
) -> None:
    assert workspace._validate_recovery_record(record, "example") == record


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("oversized", "size is invalid"),
        ("invalid-json", "is invalid"),
        ("wrong-mode", "private"),
    ],
    ids=["oversized", "invalid-json", "permissive-mode"],
)
def test_read_recovery_file_checks_size_json_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    path = tmp_path / "recovery.json"
    if kind == "oversized":
        path.write_text("x", encoding="utf-8")
        monkeypatch.setattr(workspace, "_MAX_RECOVERY_RECORD_BYTES", 0)
    else:
        path.write_text("{}", encoding="utf-8")
    path.chmod(0o644 if kind == "wrong-mode" else 0o600)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_recovery_file(  # pyright: ignore[reportPrivateUsage]
            path, "example", "recovery record"
        )


@pytest.mark.parametrize("data", [b"bad", b"\xff"], ids=["malformed", "invalid-utf8"])
def test_read_recovery_file_rejects_invalid_json(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "recovery.json"
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(WorkspaceError, match="is invalid"):
        workspace._read_recovery_file(path, "example", "recovery record")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("purpose", ["replace", "commit"], ids=["undo", "commit"])
def test_recovery_record_write_and_retire_roundtrip(
    tmp_path: Path, purpose: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    record = _replace_record() if purpose == "replace" else _commit_record()
    writer = (
        workspace._write_recovery_record
        if purpose == "replace"
        else workspace._write_commit_record
    )
    writer(state, record)  # pyright: ignore[reportPrivateUsage]
    read = (
        workspace._read_recovery_record
        if purpose == "replace"
        else workspace._read_commit_record
    )
    assert read(state, "example") == record  # pyright: ignore[reportPrivateUsage]
    retire = (
        workspace._retire_recovery_record
        if purpose == "replace"
        else workspace._retire_commit_record
    )
    retire(state, "example")  # pyright: ignore[reportPrivateUsage]
    assert read(state, "example") is None  # pyright: ignore[reportPrivateUsage]
    retire(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_retire_recovery_record_ignores_unlink_errors_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    recovery = state / "recovery"
    recovery.mkdir(mode=0o700)
    path = recovery / "example.json"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    workspace._retire_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, "example", ignore_errors=True
    )
    with pytest.raises(WorkspaceError, match="cannot retire"):
        workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_restore_transaction_file_removes_or_replaces_contents(tmp_path: Path) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    path.write_text("old", encoding="utf-8")
    workspace._restore_transaction_file(path, b"new", "pending decision")  # pyright: ignore[reportPrivateUsage]
    assert path.read_bytes() == b"new"
    workspace._restore_transaction_file(path, None, "pending decision")  # pyright: ignore[reportPrivateUsage]
    assert not path.exists()
    workspace._restore_transaction_file(path, None, "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_optional_lstat_wraps_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blocked"
    original_lstat = Path.lstat

    def deny(path: Path) -> os.stat_result:
        if path == target:
            raise PermissionError("denied")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny)
    with pytest.raises(WorkspaceError, match="cannot stat blocked"):
        workspace._optional_lstat(target, "blocked")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_replace_record(), None),
        (
            _replace_record(
                old_state=base64.b64encode(
                    json.dumps(_pending_payload()).encode()
                ).decode()
            ),
            _REVISION,
        ),
    ],
    ids=["no-old-state", "old-revision"],
)
def test_replacement_previous_revision(
    record: dict[str, object], expected: str | None
) -> None:
    assert workspace._replacement_previous_revision(record) == expected  # pyright: ignore[reportPrivateUsage]


def test_main_success_emits_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url,enabled\nExample,https://example.com/,false\n", encoding="utf-8"
    )
    assert (
        workspace.main([
            "--workspace",
            str(tmp_path),
            "--targets",
            str(tmp_path / "targets.csv"),
            "check",
        ])
        == 0
    )
    captured = capsys.readouterr()
    assert '"action": "skipped"' in captured.out
    assert not captured.err

    assert (
        workspace.main([
            "--workspace",
            str(tmp_path),
            "--targets",
            str(tmp_path / "targets.csv"),
            "pending",
        ])
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.strip() == '{"reviews": []}'
    assert not captured.err


def test_main_ingest_dispatches_agent_fetch(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    targets = tmp_path / "targets.csv"
    _write_targets(targets, "Example,https://example.com/,,true\n")
    target = load_targets(targets)[0]
    fetched = tmp_path / "agent-fetch.txt"
    fetched.write_text("content\n", encoding="utf-8")
    links = tmp_path / "navigation-links.json"
    links.write_text("[]", encoding="utf-8")
    received: dict[str, object] = {}

    def fake_ingest(*_args: object, **kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {
            "action": "unchanged",
            "target_id": target["target_id"],
            "name": "Example",
        }

    monkeypatch.setattr(workspace, "ingest_agent_fetch", fake_ingest)
    assert (
        workspace.main([
            "--workspace",
            str(tmp_path),
            "--targets",
            str(targets),
            "ingest",
            "--target-id",
            str(target["target_id"]),
            "--run-id",
            _RUN_ID,
            "--input",
            str(fetched),
            "--source-url",
            "https://redirected.example/current",
            "--links",
            str(links),
        ])
        == 0
    )
    captured = capsys.readouterr()
    assert '"action": "unchanged"' in captured.out
    assert received["source_url"] == "https://redirected.example/current"
    assert received["links_path"] == links
    assert not captured.err


def test_main_finalize_dispatches_decision(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_review_transaction(state)
    payload = json.dumps({
        "target_id": "example",
        "revision": "a" * 32,
        "material": False,
    })
    monkeypatch.setattr(
        workspace.sys,
        "stdin",
        type("Input", (), {"read": lambda _self: payload})(),  # pyright: ignore[reportUnknownLambdaType]
    )

    assert workspace.main(["--workspace", str(tmp_path), "finalize"]) == 0
    captured = capsys.readouterr()
    assert '"action": "finalized"' in captured.out
    assert not captured.err


@pytest.mark.parametrize(
    "payload", ["not json", "[]"], ids=["invalid-json", "not-object"]
)
def test_read_decision_rejects_invalid_stdin(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setattr(
        workspace.sys,
        "stdin",
        type("Input", (), {"read": lambda _self: payload})(),  # pyright: ignore[reportUnknownLambdaType]
    )
    with pytest.raises(WorkspaceError, match="stdin|object"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
        workspace._read_decision()  # pyright: ignore[reportPrivateUsage]


def _grouped_pending(state: Path, payload: dict[str, object], data: bytes) -> Path:
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    (target / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    (target / "candidate.txt").write_bytes(data)
    return target


def _write_recovery(state: Path, record: dict[str, object]) -> None:
    workspace._write_recovery_record(state, record)  # pyright: ignore[reportPrivateUsage]


def _write_commit(state: Path, revision: str = _REVISION) -> None:
    workspace._write_commit_record(  # pyright: ignore[reportPrivateUsage]
        state,
        {
            "kind": "commit",
            "revision": revision,
            "target_id": "example",
            "version": 1,
        },
    )


def test_fsync_directory_handles_optional_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(workspace.os, "O_CLOEXEC", raising=False)
    monkeypatch.delattr(workspace.os, "O_NOFOLLOW", raising=False)
    workspace._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]


def test_ensure_directory_tolerates_creation_race_and_optional_parent_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "state"
    original_lstat = Path.lstat
    original_mkdir = Path.mkdir
    calls = 0

    def racing_lstat(path: Path) -> os.stat_result:
        nonlocal calls
        if path == directory:
            calls += 1
            if calls == 1:
                raise FileNotFoundError
        return original_lstat(path)

    def racing_mkdir(path: Path, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
        original_mkdir(path)
        raise FileExistsError

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    assert (
        workspace._ensure_directory(  # pyright: ignore[reportPrivateUsage]
            directory, "state", sync_parent=False
        )
        == directory
    )


@pytest.mark.parametrize("kind", ["symlink", "fifo"], ids=["symlink", "non-file"])
def test_read_csv_requires_regular_non_symlink_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "targets.csv"
    if kind == "symlink":
        target = tmp_path / "target.csv"
        target.write_text("name,url\nA,https://example.com/\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_csv(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("name,url,unexpected\nA,https://example.com/,x\n", "unsupported"),
        ("name\nA\n", "requires name and url"),
        ("name,url\n,https://example.com/\n", "name must be non-empty"),
        ("name,url,enabled\nA,https://example.com/,no\n", "enabled must be"),
        (
            "name,url\nA,https://user:pass@example.com/\n",
            "credentials",
        ),
    ],
    ids=[
        "unsupported-column",
        "missing-required",
        "empty-name",
        "bad-enabled",
        "credentials",
    ],
)
def test_load_targets_rejects_invalid_schema_and_row_values(
    tmp_path: Path, csv_text: str, message: str
) -> None:
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        workspace.load_targets(tmp_path / "targets.csv")


def test_existing_pending_paths_wraps_target_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    pending.mkdir(parents=True)
    target = pending / "example"
    original_lstat = Path.lstat

    def fail(path: Path) -> os.stat_result:
        if path == target:
            message = "injected"
            raise PermissionError(message)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match="cannot stat pending target"):
        workspace._existing_pending_paths(state, "example")


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_existing_pending_paths_rejects_unsafe_target(
    tmp_path: Path, kind: str
) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    pending.mkdir(parents=True)
    target = pending / "example"
    if kind == "symlink":
        target.symlink_to(tmp_path, target_is_directory=True)
    else:
        target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="pending target must be"):
        workspace._existing_pending_paths(state, "example")


@pytest.mark.parametrize(
    ("kind", "message"),
    [("stat", "cannot stat candidate"), ("symlink", "regular non-symlink")],
    ids=["stat-error", "symlink"],
)
def test_candidate_path_rejects_unavailable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    state = tmp_path / "internal" / "state"
    candidate_dir = state / "pending" / "example"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "state.json").write_text("{}", encoding="utf-8")
    candidate = candidate_dir / "candidate.txt"
    if kind == "symlink":
        candidate.symlink_to(tmp_path / "missing")
    else:
        original_lstat = Path.lstat

        def fail(path: Path) -> os.stat_result:
            if path == candidate:
                message = "injected"
                raise PermissionError(message)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._candidate_path(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [("stat", "cannot stat report"), ("directory", "regular non-symlink")],
    ids=["stat-error", "directory"],
)
def test_report_path_rejects_invalid_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    destination = tmp_path / f"{_RUN_ID}.md"
    if kind == "directory":
        destination.mkdir()
    else:
        original_lstat = Path.lstat

        def fail(path: Path) -> os.stat_result:
            if path == destination:
                message = "injected"
                raise PermissionError(message)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._report_path(tmp_path, _RUN_ID)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("function", ["report", "snapshot"], ids=["report", "snapshot"])
def test_optional_reads_wrap_non_missing_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, function: str
) -> None:
    path = tmp_path / "value.txt"
    original_lstat = Path.lstat

    def fail(target: Path) -> os.stat_result:
        if target == path:
            raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(target)

    monkeypatch.setattr(Path, "lstat", fail)
    reader = (
        workspace._read_optional_report
        if function == "report"
        else workspace._read_snapshot
    )
    with pytest.raises(WorkspaceError, match="cannot stat"):
        reader(path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("stat", "cannot stat test file"),
        ("directory", "regular non-symlink"),
        ("read", "cannot read test file"),
    ],
    ids=["stat-error", "directory", "read-error"],
)
def test_read_text_bytes_wraps_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    path = tmp_path / "value.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "read":
        path.write_text("valid", encoding="utf-8")
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        original_lstat = Path.lstat

        def fail(target: Path) -> os.stat_result:
            if target == path:
                message = "injected"
                raise PermissionError(message)
            return original_lstat(target)

        monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_text_bytes(path, "test file")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure", ["mkstemp", "write"], ids=["create-temp", "write-temp"]
)
def test_temporary_file_creation_and_write_failures_are_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    destination = tmp_path / "target.txt"
    if failure == "mkstemp":
        monkeypatch.setattr(
            workspace.tempfile,
            "mkstemp",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot create temporary"
    else:
        monkeypatch.setattr(
            workspace,
            "_write_file_data",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot write temporary"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_temporary_file(destination, b"data", "test")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["replace", "fsync", "readback"], ids=["replace", "fsync", "readback"]
)
def test_snapshot_promotion_wraps_failures_and_checks_durable_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    data = b"candidate"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(workspace, "_fsync_snapshot_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot promote snapshot"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_snapshot_directory",
            lambda _path: (_ for _ in ()).throw(WorkspaceError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "injected"
    else:
        original_read = workspace._read_snapshot
        calls = 0

        def changed(path: Path) -> bytes | None:
            nonlocal calls
            calls += 1
            return original_read(path) if calls == 1 else b"different"

        monkeypatch.setattr(workspace, "_read_snapshot", changed)
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
            state,
            target_id="example",
            expected_sha256=None,
            candidate_sha256=digest,
            candidate_data=data,
        )


def test_snapshot_promotion_accepts_candidate_source_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    candidate = tmp_path / "candidate.txt"
    candidate.write_bytes(b"candidate")
    digest = __import__("hashlib").sha256(b"candidate").hexdigest()
    monkeypatch.setattr(workspace, "_fsync_snapshot_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    promoted = workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256=None,
        candidate_sha256=digest,
        candidate_source=candidate,
    )
    assert promoted["action"] == "snapshot_promoted"
    conflict = workspace._promote_snapshot(  # pyright: ignore[reportPrivateUsage]
        state,
        target_id="example",
        expected_sha256="c" * 64,
        candidate_sha256=hashlib.sha256(b"another").hexdigest(),
        candidate_data=b"another",
    )
    assert conflict["action"] == "snapshot_conflict"


@pytest.mark.parametrize(
    "fault",
    ["render-size", "replace", "fsync", "readback"],
    ids=["rendered-size", "replace", "fsync", "readback"],
)
def test_report_write_validates_and_persists_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 20)
    if fault == "render-size":
        report = "x" * 19
        expected = "size is invalid"
    else:
        monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1000)
        report = "ok"
        if fault == "replace":
            monkeypatch.setattr(
                Path,
                "replace",
                lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            )
            expected = "cannot write report"
        elif fault == "fsync":
            original_fsync = workspace._fsync_directory

            def fail_report_fsync(path: Path) -> None:
                if path.name == "report":
                    message = "injected"
                    raise OSError(message)
                original_fsync(path)

            monkeypatch.setattr(workspace, "_fsync_directory", fail_report_fsync)
            expected = "cannot fsync report directory"
        else:
            calls = 0

            def mismatch(_path: Path, _description: str) -> bytes:
                nonlocal calls
                calls += 1
                return b"bad"

            monkeypatch.setattr(workspace, "_read_text_bytes", mismatch)
            expected = "report read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_report(workspace_root, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]


def test_monitor_target_skips_candidate_read_for_unchanged_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "snapshots").mkdir()
    monkeypatch.setattr(workspace, "_recover_pending", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace.monitor, "run", lambda _args: {"status": "unchanged"})  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace, "_discard_pending", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    target = {"target_id": "example", "url": "https://example.com/", "name": "Example"}
    result = workspace._monitor_target(state, target, _RUN_ID)  # pyright: ignore[reportPrivateUsage]
    assert result["action"] == "unchanged"


@pytest.mark.parametrize("fault", ["grouped-remove", "fsync"])
def test_remove_pending_wraps_removal_and_sync_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    (target / "state.json").write_text("{}\n", encoding="utf-8")

    def fail_remove(_path: Path) -> None:
        message = "injected"
        raise OSError(message)

    original_fsync = workspace._fsync_directory

    def fail_pending_fsync(path: Path) -> None:
        if path == target.parent:
            fail_remove(path)
        original_fsync(path)

    if fault == "grouped-remove":
        monkeypatch.setattr(workspace.shutil, "rmtree", fail_remove)
    else:
        monkeypatch.setattr(workspace, "_fsync_directory", fail_pending_fsync)

    with pytest.raises(WorkspaceError, match="cannot remove pending transaction"):
        workspace._remove_pending(state, "example")


@pytest.mark.parametrize("fault", ["replace", "fsync"], ids=["replace", "fsync"])
def test_pending_file_wraps_atomic_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    destination = tmp_path / "pending" / "state.json"
    destination.parent.mkdir()
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist pending decision"
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync pending directory"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_pending_file(destination, _pending_payload())  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "permissive"], ids=["missing-dir", "symlink", "mode"]
)
def test_recovery_directory_validates_existing_directory(
    tmp_path: Path, kind: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    recovery = state / "recovery"
    if kind == "symlink":
        recovery.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "permissive":
        recovery.mkdir(mode=0o755)
        recovery.chmod(0o755)
    if kind == "missing":
        assert workspace._recovery_directory(state, create=False) is None  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(WorkspaceError, match="non-symlink|private"):  # ruff: ignore[pytest-raises-ambiguous-pattern]
            workspace._recovery_directory(state, create=False)  # pyright: ignore[reportPrivateUsage]


def test_decode_recovery_backup_rejects_non_ascii_and_wrong_type() -> None:
    for value in (5, "é", "!@@"):
        with pytest.raises(WorkspaceError, match="invalid"):
            workspace._decode_recovery_backup(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "record",
    [
        _replace_record(group_dir_existed=True),
        _cleanup_record(),
    ],
    ids=["existing-replacement", "nonmaterial-finalize"],
)
def test_recovery_record_validator_accepts_supported_optional_shapes(
    record: dict[str, object],
) -> None:
    assert workspace._validate_recovery_record(record, "example") == record


def test_read_commit_record_rejects_non_commit_record(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    recovery = state / "recovery"
    recovery.mkdir(parents=True, mode=0o700)
    path = recovery / "example.json.commit"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(WorkspaceError, match="pending commit record is invalid"):
        workspace._read_commit_record(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["commit-kind", "unsafe-existing", "replace", "fsync"],
    ids=["wrong-record-kind", "unsafe-existing", "replace", "fsync"],
)
def test_recovery_writer_validates_and_wraps_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    record = _replace_record()
    if fault == "commit-kind":
        with pytest.raises(WorkspaceError, match="pending commit record is invalid"):
            workspace._write_commit_record(state, record)  # pyright: ignore[reportPrivateUsage]
        return
    recovery = state / "recovery"
    recovery.mkdir(mode=0o700)
    destination = recovery / "example.json"
    if fault == "unsafe-existing":
        destination.write_text("old", encoding="utf-8")
        destination.chmod(0o644)
        expected = "unsafe"
    elif fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist"
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_recovery_record(state, record)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["unsafe", "unlink", "fsync"], ids=["unsafe", "unlink", "fsync"]
)
def test_recovery_retirement_checks_safety_and_optional_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    recovery = state / "recovery"
    recovery.mkdir(parents=True, mode=0o700)
    path = recovery / "example.json"
    path.write_text(json.dumps(_replace_record()), encoding="utf-8")
    path.chmod(0o644 if fault == "unsafe" else 0o600)
    if fault == "unsafe":
        expected = "unsafe"
        with pytest.raises(WorkspaceError, match=expected):
            workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "unlink":
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    else:
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
    with pytest.raises(WorkspaceError, match="cannot retire"):
        workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]
    workspace._retire_recovery_record(  # pyright: ignore[reportPrivateUsage]
        state, "example", ignore_errors=True
    )


@pytest.mark.parametrize("kind", ["symlink", "directory"], ids=["symlink", "directory"])
def test_transaction_backup_requires_regular_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "backup"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_transaction_backup(path, "backup")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["replace", "fsync", "readback"], ids=["replace", "fsync", "readback"]
)
def test_pending_candidate_persistence_checks_durable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    target = tmp_path / "pending" / "example"
    target.mkdir(parents=True)
    if fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot persist pending candidate"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot fsync pending candidate"
    else:
        monkeypatch.setattr(workspace, "_read_text_bytes", lambda *_args: b"different")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_pending_candidate(target / "candidate.txt", b"candidate")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["missing-unlink", "replace", "fsync", "readback"],
    ids=["missing-unlink", "replace", "fsync", "readback"],
)
def test_transaction_restore_wraps_delete_and_atomic_restore_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    if fault == "missing-unlink":
        path.write_text("old", encoding="utf-8")
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = None
        expected = "cannot restore"
    elif fault == "replace":
        monkeypatch.setattr(
            Path,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = b"new"
        expected = "cannot restore"
    elif fault == "fsync":
        monkeypatch.setattr(
            workspace,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        data = b"new"
        expected = "cannot fsync"
    else:
        monkeypatch.setattr(workspace, "_read_text_bytes", lambda *_args: b"different")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        data = b"new"
        expected = "read-back mismatch"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._restore_transaction_file(path, data, "pending decision")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "kind", ["iterdir", "symlink", "unlink"], ids=["iterdir", "symlink", "unlink"]
)
def test_pending_temporary_cleanup_rejects_invalid_or_unremovable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = tmp_path / "pending"
    directory.mkdir()
    if kind == "iterdir":
        monkeypatch.setattr(
            Path,
            "iterdir",
            lambda _path: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "cannot inspect"
    else:
        temp = directory / ".candidate.txt.x.tmp"
        if kind == "symlink":
            temp.symlink_to(tmp_path / "missing")
            expected = "must be regular"
        else:
            temp.write_text("x", encoding="utf-8")
            monkeypatch.setattr(
                Path,
                "unlink",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            )
            expected = "cannot remove"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._remove_pending_write_temporaries(directory)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("group_existed", [True, False], ids=["existing", "absent"])
def test_restore_pending_replacement_restores_current_layout(
    tmp_path: Path, group_existed: bool
) -> None:
    state = tmp_path / "internal" / "state"
    pending = state / "pending"
    grouped = pending / "example"
    grouped.mkdir(parents=True)
    (grouped / "state.json").write_text("new", encoding="utf-8")
    (grouped / "candidate.txt").write_text("new", encoding="utf-8")

    old_state = b"old state" if group_existed else None
    old_candidate = b"old candidate" if group_existed else None
    record = _replace_record(
        group_dir_existed=group_existed,
        old_state=None if old_state is None else base64.b64encode(old_state).decode(),
        old_candidate=(
            None if old_candidate is None else base64.b64encode(old_candidate).decode()
        ),
    )

    workspace._restore_pending_replacement(state, record)

    if group_existed:
        assert (grouped / "state.json").read_bytes() == old_state
        assert (grouped / "candidate.txt").read_bytes() == old_candidate
    else:
        assert not grouped.exists()


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ({"target_id": "../x", "revision": _REVISION, "material": False}, "target_id"),
        ({"target_id": "example", "revision": "bad", "material": False}, "revision"),
        ({"target_id": "example", "revision": _REVISION, "material": 1}, "boolean"),
        (
            {"target_id": "example", "revision": _REVISION, "material": True},
            "non-empty report",
        ),
        (
            {
                "target_id": "example",
                "revision": _REVISION,
                "material": False,
                "extra": 1,
            },
            "unsupported fields",
        ),
    ],
    ids=["bad-target", "bad-revision", "bad-material", "missing-report", "extra-field"],
)
def test_decision_validation_rejects_invalid_shapes(
    decision: dict[str, object], message: str
) -> None:
    with pytest.raises(WorkspaceError, match=message):
        workspace._validate_decision(decision)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("payload", "result"),
    [
        (None, False),
        ([], False),
        ({"extra": 1}, False),
        ({"target_id": "elsewhere"}, False),
        ({"revision": "c" * 32}, False),
        ({"run_id": "bad"}, False),
        ({"diff_truncated": 1}, False),
        ({"candidate_sha256": "bad"}, False),
        ({"expected_sha256": "bad"}, False),
    ],
    ids=[
        "not-object",
        "list",
        "wrong-fields",
        "wrong-target",
        "wrong-revision",
        "bad-run-id",
        "bad-truncation-flag",
        "bad-candidate-digest",
        "bad-expected-digest",
    ],
)
def test_replacement_matches_commit_returns_false_for_invalid_state(
    tmp_path: Path, payload: object, result: bool
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    data = b"candidate"
    item = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    item.update(payload if isinstance(payload, dict) else {})  # pyright: ignore[reportUnknownArgumentType]
    target = _grouped_pending(state, item, data)
    if payload is None:
        (target / "state.json").write_text("null", encoding="utf-8")
    elif isinstance(payload, list):
        (target / "state.json").write_text("[]", encoding="utf-8")
    assert (
        workspace._replacement_matches_commit(  # pyright: ignore[reportPrivateUsage]
            state, "example", {"revision": _REVISION}
        )
        is result
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing-state",
        "state-file",
        "missing-pending",
        "missing-group",
        "missing-files",
        "invalid-json",
        "invalid-utf8",
        "symlink-state",
        "directory-state",
        "symlink-candidate",
        "directory-candidate",
    ],
    ids=[
        "missing-state",
        "state-file",
        "missing-pending",
        "missing-group",
        "missing-files",
        "invalid-json",
        "invalid-utf8",
        "symlink-state",
        "directory-state",
        "symlink-candidate",
        "directory-candidate",
    ],
)
def test_replacement_matches_commit_handles_missing_malformed_and_unsafe_paths(  # ruff: ignore[too-many-return-statements]
    tmp_path: Path, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    pending = state / "pending"
    target = pending / "example"
    if fault == "state-file":
        state.rmdir()
        state.write_text("not a directory", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="state must be"):
            workspace._replacement_matches_commit(
                state, "example", {"revision": _REVISION}
            )  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-state":
        state.rmdir()
        with pytest.raises(WorkspaceError, match="state must be"):
            workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-pending":
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault == "missing-group":
        pending.mkdir()
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    pending.mkdir()
    target.mkdir()
    state_path = target / "state.json"
    candidate_path = target / "candidate.txt"
    if fault == "missing-files":
        state_path.write_text("{}", encoding="utf-8")
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault in {"invalid-json", "invalid-utf8"}:
        state_path.write_bytes(b"\xff" if fault == "invalid-utf8" else b"{")
        candidate_path.write_bytes(b"candidate")
        assert not workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    if fault in {"symlink-state", "directory-state"}:
        if fault == "symlink-state":
            state_path.symlink_to(tmp_path / "missing")
        else:
            state_path.mkdir()
        candidate_path.write_bytes(b"candidate")
        with pytest.raises(WorkspaceError, match="pending decision must be"):
            workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]
        return
    state_path.write_text("{}", encoding="utf-8")
    if fault == "symlink-candidate":
        candidate_path.symlink_to(tmp_path / "missing")
    else:
        candidate_path.mkdir()
    with pytest.raises(WorkspaceError, match="candidate must be"):
        workspace._replacement_matches_commit(state, "example", {})  # pyright: ignore[reportPrivateUsage]


def test_replacement_matches_commit_accepts_valid_durable_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    data = b"candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    _grouped_pending(state, payload, data)
    assert workspace._replacement_matches_commit(  # pyright: ignore[reportPrivateUsage]
        state, "example", {"revision": _REVISION}
    )


@pytest.mark.parametrize(
    "record",
    [
        _replace_record(old_state=base64.b64encode(b"{").decode()),
        _replace_record(old_state=base64.b64encode(b"\xff").decode()),
        _replace_record(old_state=base64.b64encode(b"[]").decode()),
        _replace_record(
            old_state=base64.b64encode(
                json.dumps(_pending_payload(revision="bad")).encode()
            ).decode()
        ),
    ],
    ids=["invalid-json", "invalid-utf8", "not-object", "invalid-revision"],
)
def test_replacement_previous_revision_rejects_corrupt_backup(
    record: dict[str, object],
) -> None:
    with pytest.raises(WorkspaceError, match="pending recovery record is invalid"):
        workspace._replacement_previous_revision(record)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("mode", ["no-record", "commit-only", "cleanup", "replace"])
def test_recover_pending_completes_current_recovery_records(
    tmp_path: Path, mode: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "pending").mkdir()
    if mode == "no-record":
        assert workspace._recover_pending(state, "example") is None
        return
    if mode == "commit-only":
        _write_commit(state)
        assert workspace._recover_pending(state, "example") is None
        assert workspace._read_commit_record(state, "example") is None
        return
    if mode == "cleanup":
        record = _cleanup_record(
            purpose="discard",
            material=None,
            report_sha256=None,
            revision=None,
            run_id=None,
        )
        _write_recovery(state, record)
        assert workspace._recover_pending(state, "example") == record
        return

    _write_recovery(state, _replace_record())
    assert workspace._recover_pending(state, "example") is None


def test_normal_recovery_file_rejects_commit_record(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    workspace._write_recovery_record_at(state, _commit_record(), commit=False)
    with pytest.raises(WorkspaceError, match="invalid"):
        workspace._read_recovery_record(state, "example")


@pytest.mark.parametrize(
    ("case", "revision", "expected"),
    [
        ("no-revision", None, "decision revision"),
        ("wrong-revision", "c" * 32, "does not match pending replacement"),
        ("committed-incomplete", _REVISION, "committed replacement is incomplete"),
    ],
    ids=["requires-revision", "wrong-revision", "incomplete-commit"],
)
def test_recover_pending_rejects_unresolvable_commit_states(
    tmp_path: Path, case: str, revision: str | None, expected: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "pending").mkdir()
    _write_recovery(state, _replace_record())
    _write_commit(state)
    if case == "committed-incomplete":
        revision = _REVISION
    with pytest.raises(WorkspaceError, match=expected):
        workspace._recover_pending(state, "example", revision=revision)  # pyright: ignore[reportPrivateUsage]


def test_prepare_pending_for_read_preserves_complete_committed_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_recovery(state, _replace_record())
    _write_commit(state)
    data = b"new candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    group = _grouped_pending(state, payload, data)

    workspace._prepare_pending_for_read(  # pyright: ignore[reportPrivateUsage]
        state,
        "example",
    )

    assert group.joinpath("candidate.txt").read_bytes() == data
    assert (
        workspace._read_recovery_record(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )
        is not None
    )
    assert (
        workspace._read_commit_record(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )
        is not None
    )


def test_discard_pending_accepts_complete_committed_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_recovery(state, _replace_record())
    _write_commit(state)
    data = b"new candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    group = _grouped_pending(state, payload, data)

    assert workspace.discard_pending(tmp_path, "example") == {
        "action": "discarded",
        "run_id": _RUN_ID,
        "target_id": "example",
    }
    assert not group.exists()
    assert (
        workspace._read_recovery_record(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )
        is None
    )
    assert (
        workspace._read_commit_record(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )
        is None
    )


def test_prepare_pending_for_read_rejects_incomplete_committed_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "pending").mkdir()
    _write_recovery(state, _replace_record())
    _write_commit(state)

    with pytest.raises(WorkspaceError, match="committed replacement is incomplete"):
        workspace._prepare_pending_for_read(  # pyright: ignore[reportPrivateUsage]
            state,
            "example",
        )


@pytest.mark.parametrize(
    "resolution", ["accept", "rollback"], ids=["accept-commit", "rollback-old"]
)
def test_recover_pending_resolves_committed_replacement_by_revision(
    tmp_path: Path, resolution: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    old_revision = "c" * 32
    old_payload = _pending_payload(
        revision=old_revision,
        candidate_sha256=hashlib.sha256(b"old candidate").hexdigest(),
    )
    undo = _replace_record(
        group_dir_existed=True,
        old_state=base64.b64encode(json.dumps(old_payload).encode()).decode(),
        old_candidate=base64.b64encode(b"old candidate").decode(),
    )
    _write_recovery(state, undo)
    _write_commit(state)
    data = b"new candidate"
    payload = _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())
    group = _grouped_pending(state, payload, data)

    revision = _REVISION if resolution == "accept" else old_revision
    assert workspace._recover_pending(state, "example", revision=revision) is None
    expected = data if resolution == "accept" else b"old candidate"
    assert group.joinpath("candidate.txt").read_bytes() == expected
    assert workspace._read_recovery_record(state, "example") is None
    assert workspace._read_commit_record(state, "example") is None


def test_recover_pending_rejects_conflicting_recovery_and_commit_records(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    _write_recovery(
        state,
        {
            "kind": "cleanup",
            "material": None,
            "purpose": "discard",
            "report_sha256": None,
            "revision": None,
            "run_id": None,
            "target_id": "example",
            "version": 1,
        },
    )
    _write_commit(state)
    with pytest.raises(WorkspaceError, match="records conflict"):
        workspace._recover_pending(state, "example", revision=_REVISION)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["malformed-json", "wrong-type"], ids=["malformed", "wrong-type"]
)
def test_read_pending_rejects_invalid_json_and_wrong_shapes(
    tmp_path: Path, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    path.write_text("{" if fault == "malformed-json" else "[]", encoding="utf-8")
    with pytest.raises(
        WorkspaceError,
        match="no valid pending|pending decision is invalid",  # ruff: ignore[pytest-raises-ambiguous-pattern]
    ):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("field_change", "message"),
    [
        ({"extra": 1}, "pending decision is invalid"),
        ({"run_id": "bad"}, "pending decision is invalid"),
        ({"diff_truncated": "false"}, "pending decision is invalid"),
        ({"diff": 1}, "pending decision is invalid"),
    ],
    ids=[
        "unexpected-field",
        "invalid-run-id",
        "invalid-truncated-flag",
        "invalid-review-context",
    ],
)
def test_read_pending_rejects_extra_fields_and_bad_run_ids(
    tmp_path: Path, field_change: dict[str, object], message: str
) -> None:
    state = tmp_path / "internal" / "state"
    payload = _pending_payload()
    payload.update(field_change)
    _grouped_pending(state, payload, b"candidate")
    with pytest.raises(WorkspaceError, match=message):
        workspace._read_pending(state, "example")


@pytest.mark.parametrize("field", ["run_id", "interests"])
def test_read_pending_rejects_missing_required_context(
    tmp_path: Path, field: str
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    payload = _pending_payload()
    payload.pop(field)
    path = target / "state.json"
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(WorkspaceError, match="pending decision is invalid"):
        workspace._read_pending(state, "example")
    assert path.read_text(encoding="utf-8") == original


def test_load_targets_rejects_non_string_csv_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "targets.csv").write_text("name,url\n", encoding="utf-8")

    def fake_reader(_stream: object, **_options: object) -> Iterator[list[object]]:
        rows: list[list[object]] = [["name", "url"], [object(), "https://example.com/"]]
        return iter(rows)

    monkeypatch.setattr(workspace.csv, "reader", fake_reader)
    with pytest.raises(WorkspaceError, match="invalid CSV value"):
        workspace.load_targets(tmp_path / "targets.csv")


@pytest.mark.parametrize("report", ["", "x" * 2], ids=["empty", "oversized"])
def test_report_write_rejects_empty_or_oversized_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(WorkspaceError, match="report size is invalid"):
        workspace._write_report(root, _RUN_ID, "example", report)  # pyright: ignore[reportPrivateUsage]


def test_recovery_directory_wraps_second_lstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    directory = state / "recovery"
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat
    calls = 0

    def fail_second(path: Path) -> os.stat_result:
        nonlocal calls
        if path == directory:
            calls += 1
            if calls == 2:
                message = "injected"
                raise PermissionError(message)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_second)
    with pytest.raises(WorkspaceError, match="cannot stat pending recovery directory"):
        workspace._recovery_directory(state, create=False)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault", ["size", "no-directory"], ids=["size", "no-directory"]
)
def test_recovery_writer_rejects_size_and_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    if fault == "size":
        monkeypatch.setattr(workspace, "_MAX_RECOVERY_RECORD_BYTES", 0)
        expected = "size is invalid"
    else:
        monkeypatch.setattr(
            workspace,
            "_recovery_directory",
            lambda *_args, **_kwargs: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        expected = "directory is unavailable"
    with pytest.raises(WorkspaceError, match=expected):
        workspace._write_recovery_record(state, _replace_record())  # pyright: ignore[reportPrivateUsage]


def test_recovery_retirement_returns_when_directory_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(
        workspace,
        "_recovery_directory",
        lambda *_args, **_kwargs: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    workspace._retire_recovery_record(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_capture_pending_replacement_rejects_target_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    (target / "state.json").write_text("{}", encoding="utf-8")
    (target / "candidate.txt").write_text("old", encoding="utf-8")
    original_read = workspace._read_transaction_backup

    def replace_group(path: Path, description: str) -> bytes | None:
        data = original_read(path, description)
        if description == "candidate":
            (target / "state.json").unlink()
            (target / "candidate.txt").unlink()
            target.rmdir()
            target.write_text("raced", encoding="utf-8")
        return data

    monkeypatch.setattr(workspace, "_read_transaction_backup", replace_group)
    with pytest.raises(WorkspaceError, match="pending target must be"):
        workspace._capture_pending_replacement(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("data", [None, b"new"], ids=["delete", "replace"])
def test_restore_transaction_file_rejects_symlink_targets(
    tmp_path: Path, data: bytes | None
) -> None:
    path = tmp_path / "target"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._restore_transaction_file(path, data, "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_restore_transaction_file_wraps_restore_directory_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pending" / "state.json"
    path.parent.mkdir()
    original_fsync = workspace._fsync_directory

    def fail_pending(path_arg: Path) -> None:
        if path_arg == path.parent:
            raise OSError("injected")  # ruff: ignore[raw-string-in-exception]
        original_fsync(path_arg)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_pending)
    with pytest.raises(WorkspaceError, match="cannot fsync pending decision directory"):
        workspace._restore_transaction_file(path, b"new", "pending decision")  # pyright: ignore[reportPrivateUsage]


def test_pending_temporary_cleanup_skips_entries_removed_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "pending"
    directory.mkdir()
    temp = directory / ".state.json.race.tmp"
    temp.write_text("x", encoding="utf-8")
    original_optional = workspace._optional_lstat

    def removed(path: Path, description: str) -> os.stat_result | None:
        if path == temp:
            temp.unlink()
            return None
        return original_optional(path, description)

    monkeypatch.setattr(workspace, "_optional_lstat", removed)
    workspace._remove_pending_write_temporaries(directory)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "fault",
    ["grouped-invalid", "unrecognized-file", "fsync"],
    ids=["grouped-symlink", "nonempty-group", "fsync"],
)
def test_restore_pending_replacement_rejects_unsafe_state_or_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    pending = state / "pending"
    pending.mkdir()
    grouped = pending / "example"
    if fault == "grouped-invalid":
        grouped.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(WorkspaceError, match="pending target must be"):
            workspace._restore_pending_replacement(state, _replace_record())
        return
    if fault == "unrecognized-file":
        grouped.mkdir()
        (grouped / "keep.txt").write_text("x", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="cannot restore pending target"):
            workspace._restore_pending_replacement(state, _replace_record())
        return

    original_fsync = workspace._fsync_directory

    def fail_pending(path: Path) -> None:
        if path == pending:
            message = "injected"
            raise OSError(message)
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_pending)
    with pytest.raises(
        WorkspaceError, match="cannot fsync restored pending transaction"
    ):
        workspace._restore_pending_replacement(state, _replace_record())


def _valid_payload(data: bytes = b"new candidate") -> dict[str, object]:
    return _pending_payload(candidate_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize(
    "mismatch", ["candidate", "state"], ids=["candidate", "pending-state"]
)
def test_install_pending_replacement_checks_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "pending").mkdir()
    monkeypatch.setattr(workspace, "_fsync_directory", lambda _path: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    if mismatch == "candidate":
        original_read = workspace._read_text_bytes
        candidate_path = state / "pending" / "example" / "candidate.txt"
        calls = 0

        def mismatch_read(path: Path, description: str) -> bytes:
            nonlocal calls
            if path == candidate_path:
                calls += 1
                if calls == 2:
                    return b"different"
            return original_read(path, description)

        monkeypatch.setattr(workspace, "_read_text_bytes", mismatch_read)
        expected = "pending candidate read-back mismatch"
    else:
        monkeypatch.setattr(workspace, "_read_pending", lambda *_args: {})  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        expected = "pending decision read-back mismatch"

    with pytest.raises(WorkspaceError, match=expected):
        workspace._install_pending_replacement(
            state, _valid_payload(), b"new candidate"
        )


def test_handle_monitor_result_returns_baseline_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(
        workspace,
        "_promote_snapshot",
        lambda *_args, **_kwargs: {"action": "snapshot_conflict"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    target = {"target_id": "example", "name": "Example"}
    result = workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
        state, target, {"status": "baseline", "sha256": "a" * 64}, _RUN_ID
    )
    assert result["action"] == "snapshot_conflict"


def test_handle_monitor_result_rejects_mismatched_candidate_digest(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    target = {"target_id": "example", "name": "Example", "interests": [_interest()]}
    result = {
        "status": "changed",
        "sha256": "a" * 64,
        "previous_sha256": None,
    }
    with pytest.raises(WorkspaceError, match="does not match candidate"):
        workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
            state, target, result, _RUN_ID, candidate_data=b"candidate"
        )


def test_handle_monitor_result_rejects_unknown_status(tmp_path: Path) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    target = {"target_id": "example", "name": "Example"}
    with pytest.raises(WorkspaceError, match="unsupported status"):
        workspace._handle_monitor_result(  # pyright: ignore[reportPrivateUsage]
            state, target, {"status": "unknown"}, _RUN_ID
        )


def test_read_pending_wraps_state_file_lstat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    path.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def fail(value: Path) -> os.stat_result:
        if value == path:
            raise PermissionError("injected")  # ruff: ignore[raw-string-in-exception]
        return original_lstat(value)

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(WorkspaceError, match="no valid pending decision"):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("kind", ["symlink", "directory"], ids=["symlink", "directory"])
def test_read_pending_rejects_nonregular_state_file(tmp_path: Path, kind: str) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    path = target / "state.json"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    with pytest.raises(WorkspaceError, match="regular non-symlink"):
        workspace._read_pending(state, "example")  # pyright: ignore[reportPrivateUsage]


def test_finalize_recovers_discard_cleanup_before_reading_pending_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    (state / "pending").mkdir()
    discard = {
        "kind": "cleanup",
        "material": None,
        "purpose": "discard",
        "report_sha256": None,
        "revision": None,
        "run_id": None,
        "target_id": "example",
        "version": 1,
    }
    _write_recovery(state, discard)  # pyright: ignore[reportArgumentType]
    decision = {"target_id": "example", "revision": _REVISION, "material": False}
    with pytest.raises(WorkspaceError, match="no valid pending decision"):
        workspace.finalize(tmp_path, decision)


def test_finalize_rejects_pending_target_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    state = root / "internal" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(
        workspace,
        "_read_pending",
        lambda *_args: {"target_id": "other"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    decision = {"target_id": "example", "revision": _REVISION, "material": False}
    with pytest.raises(WorkspaceError, match="pending decision target does not match"):
        workspace.finalize(root, decision)


def test_finalize_checks_report_path_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    state = root / "internal" / "state"
    state.mkdir(parents=True)
    pending = {
        "target_id": "example",
        "revision": _REVISION,
        "run_id": _RUN_ID,
        "expected_sha256": None,
        "candidate_sha256": "b" * 64,
        "diff_truncated": False,
    }
    monkeypatch.setattr(workspace, "_read_pending", lambda *_args: pending)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        workspace,
        "_promote_snapshot",
        lambda *_args, **_kwargs: {"action": "snapshot_promoted"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        workspace,
        "_write_report",
        lambda *_args: root / "output" / "report" / "actual.md",  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(workspace, "_write_recovery_record", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(workspace, "_complete_cleanup_record", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        workspace,
        "_finalized_result",
        lambda *_args: {"action": "finalized", "report_path": "wrong"},  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    decision = {
        "target_id": "example",
        "revision": _REVISION,
        "material": True,
        "report": "material update",
    }
    with pytest.raises(WorkspaceError, match="finalized report path mismatch"):
        workspace.finalize(root, decision)


def test_read_decision_returns_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace.sys, "stdin", __import__("io").StringIO('{"material": false}')
    )
    assert workspace._read_decision() == {"material": False}  # pyright: ignore[reportPrivateUsage]


def test_module_entry_point_runs_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url,enabled\nExample,https://example.com/,false\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        workspace.sys,
        "argv",
        [
            "workspace.py",
            "--workspace",
            str(tmp_path),
            "--targets",
            str(tmp_path / "targets.csv"),
            "check",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(workspace.__file__)), run_name="__main__")
    assert exit_info.value.code == 0
    assert '"action": "skipped"' in capsys.readouterr().out


def _link_result(urls: list[str]) -> dict[str, object]:
    return {
        "source_url": "https://example.com/",
        "links": {
            hashlib.sha256(url.encode()).hexdigest(): url.split("#", 1)[0]
            for url in urls
        },
    }


def test_follow_links_skips_existing_self_and_duplicate_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        assert 0 < timeout <= 30
        assert max_bytes <= workspace._MAX_LINK_BYTES
        calls.append(url)
        return workspace.monitor.Document(
            b'<p>New release</p><a href="/grandchild">more</a>', url, "text/html"
        )

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    old_url = "https://example.com/old#section"
    previous = (
        f"[a:href:sha256:{hashlib.sha256(old_url.encode()).hexdigest()}]\n".encode()
    )
    result = workspace._follow_added_links(
        _link_result([
            old_url,
            "https://example.com/old#new",
            "https://example.com/#top",
            "https://example.com/new#first",
            "https://example.com/new#second",
            "https://example.com/original",
        ]),
        previous,
        source_url="https://example.com/original",
    )
    assert calls == ["https://example.com/new"]
    assert result["omitted"] == 0
    assert result["incomplete"] is False
    documents = cast("list[dict[str, object]]", result["documents"])
    assert documents[0]["url"] == documents[0]["source_url"] == calls[0]
    assert "New release" in str(documents[0]["text"])
    assert documents[0]["truncated"] is False


@pytest.mark.parametrize(
    ("link_depth", "max_links", "expected_calls", "omitted", "incomplete"),
    [
        (0, workspace._MAX_LINKS, [], 0, False),
        (1, workspace._MAX_LINKS, ["https://example.com/one"], 0, False),
        (
            2,
            workspace._MAX_LINKS,
            ["https://example.com/one", "https://example.com/two"],
            0,
            False,
        ),
        (
            3,
            2,
            ["https://example.com/one", "https://example.com/two"],
            1,
            True,
        ),
    ],
)
def test_follow_links_supports_configurable_depth_and_max_links(
    monkeypatch: pytest.MonkeyPatch,
    link_depth: int,
    max_links: int,
    expected_calls: list[str],
    omitted: int,
    incomplete: bool,
) -> None:
    pages = {
        "https://example.com/one": b'<a href="/">root</a><a href="/two">two</a>',
        "https://example.com/two": b'<a href="/one">one</a><a href="/three">three</a>',
        "https://example.com/three": b"done",
    }
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        return workspace.monitor.Document(pages[url], url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    result = workspace._follow_added_links(
        _link_result(["https://example.com/one"]),
        b"",
        link_depth=link_depth,
        max_links=max_links,
    )

    assert calls == expected_calls
    assert result["omitted"] == omitted
    assert result["incomplete"] is incomplete


@pytest.mark.parametrize(
    "mode",
    [
        "error",
        "empty",
        "truncated",
        "count",
        "time",
        "bytes",
        "review",
    ],
    ids=["error", "empty", "truncated", "count", "time", "bytes", "review"],
)
def test_follow_links_enforces_budgets_and_preserves_individual_errors(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    urls = [
        f"https://example.com/{index:02}"
        for index in range(workspace._MAX_LINKS + 2 if mode == "count" else 3)
    ]
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        if mode == "error" and url == urls[0]:
            message = "URL must resolve to a public IP"
            raise workspace.monitor.MonitorError(message)
        body = b" " if mode == "empty" else b"readable article"
        if mode in {"truncated", "review"}:
            body = b"x" * (workspace._MAX_LINK_TEXT_BYTES + 1)
        return workspace.monitor.Document(body, url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    if mode == "time":
        ticks = iter([0.0, 1.0, workspace._LINK_TIMEOUT + 1])
        monkeypatch.setattr(workspace, "monotonic", lambda: next(ticks))
    elif mode == "bytes":
        monkeypatch.setattr(
            workspace, "_MAX_LINK_TOTAL_BYTES", len(b"readable article")
        )
    elif mode == "review":
        monkeypatch.setattr(
            workspace, "_MAX_LINK_REVIEW_BYTES", workspace._MAX_LINK_TEXT_BYTES
        )
    result = workspace._follow_added_links(_link_result(urls), b"")
    documents = cast("list[dict[str, object]]", result["documents"])
    assert result["incomplete"] is True
    if mode in {"time", "bytes", "review"}:
        assert len(calls) == 1
        assert result["omitted"] == 2
    elif mode == "count":
        assert len(calls) == workspace._MAX_LINKS
        assert result["omitted"] == 2
    elif mode in {"error", "empty"}:
        assert "error" in documents[0]
        assert len(calls) == 3
    else:
        assert documents[0]["truncated"] is True
        assert len(str(documents[0]["text"]).encode()) == workspace._MAX_LINK_TEXT_BYTES


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("invalid host label"),
        UnicodeError("invalid host label"),
        TimeoutError("DNS resolution deadline exceeded"),
    ],
    ids=["value-error", "unicode-error", "timeout-error"],
)
def test_follow_links_records_fetch_errors_and_continues(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    urls = ["https://example.com/bad", "https://example.com/good"]
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        if url == urls[0]:
            raise failure
        return workspace.monitor.Document(b"good article", url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    result = workspace._follow_added_links(_link_result(urls), b"")

    documents = cast("list[dict[str, object]]", result["documents"])
    assert result["incomplete"] is True
    assert calls == urls
    assert documents[0]["error"] == str(failure)
    assert documents[1]["text"] == "good article\n"


def test_follow_links_records_malformed_child_href_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = ["https://example.com/bad", "https://example.com/good"]
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        body = (
            b'<a href="http://[::1/evil">bad link</a>'
            if url == urls[0]
            else b"good article"
        )
        return workspace.monitor.Document(body, url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    result = workspace._follow_added_links(_link_result(urls), b"")

    documents = cast("list[dict[str, object]]", result["documents"])
    assert result["incomplete"] is True
    assert calls == urls
    assert "Invalid IPv6 URL" in str(documents[0]["error"])
    assert documents[1]["text"] == "good article\n"


def test_check_can_disable_link_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,releases,true\n"
    )
    parent = b'<p>News</p><a href="/old">old</a>'
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        return workspace.monitor.Document(parent, url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert baseline["action"] == "baseline_created"
    parent += b'<a href="/new">new</a>'
    outcome = cast(
        "list[dict[str, object]]",
        check(tmp_path, tmp_path / "targets.csv", link_depth=0)["targets"],
    )[0]

    assert outcome["action"] == "review"
    assert "link_review" not in outcome
    assert calls == ["https://example.com/", "https://example.com/"]


@pytest.mark.parametrize(
    "relation",
    ["nofollow", "noopener noreferrer"],
    ids=["nofollow", "noopener-noreferrer"],
)
def test_check_follows_new_atom_xhtml_anchors_with_html_relations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relation: str
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/feed,releases,true\n"
    )
    feed = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>release</id>'
        b"</entry></feed>"
    )
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        if url == "https://example.com/feed":
            return workspace.monitor.Document(feed, url, "application/atom+xml")
        return workspace.monitor.Document(b"Release details", url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert baseline["action"] == "baseline_created"
    assert calls == ["https://example.com/feed"]

    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>release</id>'
        '<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'
        f'<a href="/release" rel="{relation}">Details</a>'
        "</div></content></entry></feed>"
    ).encode()
    outcome = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]

    assert outcome["action"] == "review"
    assert calls == [
        "https://example.com/feed",
        "https://example.com/feed",
        "https://example.com/release",
    ]
    link_review = cast("dict[str, object]", outcome["link_review"])
    documents = cast("list[dict[str, object]]", link_review["documents"])
    assert documents[0]["url"] == "https://example.com/release"
    assert documents[0]["text"] == "Release details\n"


def test_check_marks_omitted_form_destination_change_as_incomplete_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,releases,true\n"
    )
    monkeypatch.setattr(workspace.monitor, "_MAX_HTML_DESTINATIONS", 1)
    page = b'<a href="/one">one</a><form action="/old"></form>'

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        return workspace.monitor.Document(page, url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert baseline["action"] == "baseline_created"
    page = b'<a href="/one">one</a><form action="/new"></form>'

    outcome = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]

    assert outcome["action"] == "review"
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path,
            targets=tmp_path / "targets.csv",
            target_id=str(outcome["target_id"]),
        )["reviews"],
    )[0]
    link_review = cast("dict[str, object]", review["link_review"])
    assert link_review["incomplete"] is True
    assert (
        finalize(
            tmp_path,
            {
                "target_id": review["target_id"],
                "revision": review["revision"],
                "material": False,
            },
        )["action"]
        == "manual_review_required"
    )


def test_check_records_new_oversized_link_as_incomplete_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,releases,true\n"
    )
    parent = b"<p>News</p>"
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        return workspace.monitor.Document(parent, url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert baseline["action"] == "baseline_created"
    parent += b'<a href="https://example.com/' + b"x" * 4096 + b'">long</a>'

    outcome = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]

    assert outcome["action"] == "review"
    assert calls == ["https://example.com/", "https://example.com/"]
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path,
            targets=tmp_path / "targets.csv",
            target_id=str(outcome["target_id"]),
        )["reviews"],
    )[0]
    link_review = cast("dict[str, object]", review["link_review"])
    assert link_review == {"documents": [], "omitted": 1, "incomplete": True}
    assert (
        finalize(
            tmp_path,
            {
                "target_id": review["target_id"],
                "revision": review["revision"],
                "material": False,
            },
        )["action"]
        == "manual_review_required"
    )


def test_follow_links_marks_oversized_child_destinations_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = "https://example.com/one"
    oversized = "x" * 4096

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        body = f'<a href="{oversized}">deep link</a>'.encode()
        return workspace.monitor.Document(body, url, "text/html")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    result = workspace._follow_added_links(_link_result([root]), b"", link_depth=2)

    assert result["incomplete"] is True
    assert result["omitted"] == 1
    documents = cast("list[dict[str, object]]", result["documents"])
    assert len(documents) == 1
    assert "error" not in documents[0]


@pytest.mark.parametrize(
    ("failed", "material"),
    [
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ],
    ids=[
        "complete-nonmaterial",
        "complete-material",
        "incomplete-nonmaterial-refused",
        "incomplete-material",
    ],
)
def test_link_review_transaction_survives_resume_and_promotes_identity_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool, material: bool
) -> None:
    _write_targets(
        tmp_path / "targets.csv", "Example,https://example.com/,releases,true\n"
    )
    parent = b'<p>News</p><a href="/old">old</a>'
    calls: list[str] = []

    def fetch(
        url: str, *, timeout: float, max_bytes: int
    ) -> workspace.monitor.Document:
        del timeout, max_bytes
        calls.append(url)
        if url == "https://example.com/":
            return workspace.monitor.Document(parent, url, "text/html")
        if failed:
            message = "blocked private IP"
            raise workspace.monitor.MonitorError(message)
        return workspace.monitor.Document(b"Release details", url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    baseline = check(tmp_path, tmp_path / "targets.csv")
    assert (
        cast("list[dict[str, object]]", baseline["targets"])[0]["action"]
        == "baseline_created"
    )
    assert calls == ["https://example.com/"]
    parent += b'<a href="/new">new</a>'
    outcome = cast(
        "list[dict[str, object]]",
        check(tmp_path, tmp_path / "targets.csv", compact=True)["targets"],
    )[0]
    assert "link_review" not in outcome
    assert calls == [
        "https://example.com/",
        "https://example.com/",
        "https://example.com/new",
    ]
    target_id = str(outcome["target_id"])
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    context = cast("dict[str, object]", review["link_review"])
    assert context["incomplete"] is failed
    assert (
        "link_review"
        not in cast(
            "list[dict[str, object]]",
            check(tmp_path, tmp_path / "targets.csv")["targets"],
        )[0]
    )
    assert len(calls) == 3
    decision: dict[str, object] = {
        "target_id": target_id,
        "revision": review["revision"],
        "material": material,
    }
    if material:
        decision["report"] = "## Release\n\nRelease details.\n"
    expected_action = (
        "manual_review_required" if failed and not material else "finalized"
    )
    assert finalize(tmp_path, decision)["action"] == expected_action
    if expected_action == "manual_review_required":
        decision.update({
            "material": True,
            "report": "## Release\n\nRelease details.\n",
        })
        assert finalize(tmp_path, decision)["action"] == "finalized"
    if not failed and not material:
        assert workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv"
        ) == {"reviews": []}
        assert not list((tmp_path / "output" / "report").glob("*.md"))
    assert (
        cast(
            "list[dict[str, object]]",
            check(tmp_path, tmp_path / "targets.csv")["targets"],
        )[0]["action"]
        == "unchanged"
    )
    parent += b"<p>Minor page edit</p>"
    next_review = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert next_review["link_review"] == {
        "documents": [],
        "omitted": 0,
        "incomplete": False,
    }
    assert calls.count("https://example.com/new") == 1


@pytest.mark.parametrize(
    "value",
    cast(
        "list[object]",
        [
            None,
            {},
            {"documents": None, "omitted": 0, "incomplete": False},
            {
                "documents": [{}] * (workspace._MAX_LINKS + 1),
                "omitted": 0,
                "incomplete": False,
            },
            {"documents": [], "omitted": False, "incomplete": False},
            {"documents": [], "omitted": -1, "incomplete": False},
            {"documents": [], "omitted": 0, "incomplete": 0},
            {"documents": [None], "omitted": 0, "incomplete": True},
            {"documents": [{}], "omitted": 0, "incomplete": True},
            {
                "documents": [{"url": "https://example.com/", "error": 1}],
                "omitted": 0,
                "incomplete": True,
            },
            {
                "documents": [
                    {
                        "url": "https://example.com/",
                        "source_url": "https://example.com/",
                        "text": "x",
                        "truncated": 1,
                    }
                ],
                "omitted": 0,
                "incomplete": True,
            },
        ],
    ),
)
def test_link_review_validation_rejects_invalid_persisted_context(
    value: object,
) -> None:
    with pytest.raises(WorkspaceError, match="pending link review is invalid"):
        workspace._validate_link_review(value)


_ENRICHED_HEADER = "name,url,publisher,category,keywords,criteria,priority,enabled\n"


def _interest(name: object = "Example", **changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "publisher": "",
        "category": "",
        "keywords": "",
        "criteria": "",
        "priority": None,
        "enabled": True,
    }
    result.update(changes)
    return result


def _create_interest_review(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    (tmp_path / "targets.csv").write_text(
        _ENRICHED_HEADER
        + "First,https://vendor.example/updates,Vendor,Product,plans,Limits,01,true\n"
        + "Second,https://vendor.example/updates,Vendor,Product,API,"
        "Breaking changes,2,true\n"
        + "Disabled,https://vendor.example/updates,,,,,3,false\n",
        encoding="utf-8",
    )
    target = load_targets(tmp_path / "targets.csv")[0]
    target_id = str(target["target_id"])
    state = tmp_path / "internal" / "state"
    snapshots = state / "snapshots"
    snapshots.mkdir(parents=True)
    snapshot = snapshots / f"{target_id}.txt"
    snapshot.write_text("old\n", encoding="utf-8")
    workspace._handle_monitor_result(
        state,
        target,
        {
            **_changed_result(),
            "link_review": {"documents": [], "omitted": 0, "incomplete": False},
        },
        _RUN_ID,
        candidate_data=b"new\n",
    )
    metadata, candidate = workspace._pending_paths(state, target_id)
    return target_id, metadata, candidate, snapshot


@pytest.mark.parametrize(
    ("csv_text", "expected"),
    [
        ("name,url\n Example , https://example.com/ \n", _interest()),
        (
            (
                "url,enabled,name,criteria\n"
                "https://example.com/, TrUe ,Example, pricing \n"
            ),
            _interest(criteria="pricing"),
        ),
        (
            (
                "\ufeffname,url,criteria,priority,enabled\n"
                "Example,https://example.com/, pricing ,0002, FALSE \n"
            ),
            _interest(criteria="pricing", priority=2, enabled=False),
        ),
        (_ENRICHED_HEADER + "Example,https://example.com/\n", _interest()),
        (
            (
                "name,url,publisher,category,keywords,criteria,priority\n"
                "Example,https://example.com/, Publisher , Category ,"
                '"a, b / c","first\nsecond",1\n'
            ),
            _interest(
                publisher="Publisher",
                category="Category",
                keywords="a, b / c",
                criteria="first\nsecond",
                priority=1,
            ),
        ),
        (
            (
                "name,url,keywords,criteria\n"
                "\n,,,\nExample,https://example.com/,,mentions 同上 and 〃\n"
            ),
            _interest(criteria="mentions 同上 and 〃"),
        ),
    ],
    ids=[
        "minimal",
        "reordered",
        "bom-case-priority",
        "short-optional",
        "quoted",
        "blank-and-embedded",
    ],
)
def test_interest_csv_normalization(
    tmp_path: Path, csv_text: str, expected: dict[str, object]
) -> None:
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    target = load_targets(tmp_path / "targets.csv")[0]
    assert target["interests"] == [expected]
    assert target["target_id"] == workspace._target_id("https://example.com/")
    assert target["enabled"] is expected["enabled"]


@pytest.mark.parametrize(
    "priority", ["0", "00", "-1", "+1", "1.0", "1e2", "one", "\u0661", "\uff11"]
)
def test_invalid_priority_in_later_disabled_row_prevents_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, priority: str
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    before = [path.read_bytes() for path in (metadata, candidate, snapshot)]
    (tmp_path / "targets.csv").write_text(
        "name,url,priority,enabled\nOther,https://other.example/,1,true\n"
        f"Disabled,https://vendor.example/updates,{priority},false\n",
        encoding="utf-8",
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid configuration must not fetch or discard")

    monkeypatch.setattr(workspace.monitor, "run", forbidden)
    monkeypatch.setattr(workspace, "discard_pending", forbidden)
    with pytest.raises(WorkspaceError, match="row 3: priority"):
        check(tmp_path, tmp_path / "targets.csv")
    assert [path.read_bytes() for path in (metadata, candidate, snapshot)] == before
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    assert [
        item["name"] for item in cast("list[dict[str, object]]", review["interests"])
    ] == ["First", "Second"]


@pytest.mark.parametrize(
    "field", ["name", "url", "publisher", "category", "keywords", "criteria"]
)
@pytest.mark.parametrize("token", ['"', "〃", "同上", "同左"])
def test_ditto_cells_rejected_with_record_and_field(
    tmp_path: Path, field: str, token: str
) -> None:
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["name", "url", field] if field not in {"name", "url"} else ["name", "url"]
    writer.writerow(header)
    writer.writerow([])
    values = {"name": "Example", "url": "https://example.com/", field: f" {token} "}
    writer.writerow([values[key] for key in header])
    (tmp_path / "targets.csv").write_text(output.getvalue(), encoding="utf-8")
    with pytest.raises(WorkspaceError, match=f"row 3: {field}: replace ditto"):
        load_targets(tmp_path / "targets.csv")


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("name,url,watch_focus\nExample,https://example.com/,pricing\n", "unsupported"),
        ("name,url,target_id\nExample,https://example.com/,custom\n", "unsupported"),
        ("name,url\nExample\n", "row 2: url"),
        ('name,url,keywords\nExample,https://example.com/,"unterminated', "CSV record"),
        ("name,url\n\n\n", "no targets"),
        ("name,url\n", "no targets"),
    ],
    ids=[
        "removed-column",
        "supplied-id",
        "missing-url",
        "malformed-quote",
        "blank",
        "header-only",
    ],
)
def test_enriched_csv_errors(tmp_path: Path, csv_text: str, message: str) -> None:
    (tmp_path / "targets.csv").write_text(csv_text, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=message):
        load_targets(tmp_path / "targets.csv")


@pytest.mark.parametrize("field", ["keywords", "criteria"])
def test_large_csv_text_and_serialized_expansion(tmp_path: Path, field: str) -> None:
    text = "〃x" * 160_000
    (tmp_path / "targets.csv").write_text(
        f"name,url,{field}\nExample,https://example.com/,{text}\n",
        encoding="utf-8",
    )
    previous_limit = workspace.csv.field_size_limit()
    target = load_targets(tmp_path / "targets.csv")[0]
    assert workspace.csv.field_size_limit() == previous_limit
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    workspace._handle_monitor_result(
        state, target, _changed_result(), _RUN_ID, candidate_data=b"new\n"
    )
    metadata = state / "pending" / str(target["target_id"]) / "state.json"
    assert metadata.stat().st_size > 1024 * 1024
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path,
            targets=tmp_path / "targets.csv",
            target_id=str(target["target_id"]),
        )["reviews"],
    )[0]
    assert cast("list[dict[str, object]]", review["interests"])[0][field] == text


def test_priority_exceeding_python_decimal_limit_round_trips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digits = "1" + "0" * 5000
    (tmp_path / "targets.csv").write_text(
        f"name,url,priority\nExample,https://example.com/,{digits}\n",
        encoding="utf-8",
    )
    target = load_targets(tmp_path / "targets.csv")[0]
    state = tmp_path / "internal" / "state"
    state.mkdir(parents=True)
    workspace._handle_monitor_result(
        state, target, _changed_result(), _RUN_ID, candidate_data=b"new\n"
    )
    pending = workspace._read_pending(state, str(target["target_id"]))
    assert (
        cast("list[dict[str, object]]", pending["interests"])[0]["priority"] == 10**5000
    )
    undo = workspace._capture_pending_replacement(state, str(target["target_id"]))
    assert workspace._replacement_previous_revision(undo) == pending["revision"]
    assert (
        workspace.main([
            "--workspace",
            str(tmp_path),
            "--targets",
            str(tmp_path / "targets.csv"),
            "pending",
            "--target-id",
            str(target["target_id"]),
        ])
        == 0
    )
    assert digits in capsys.readouterr().out


@pytest.mark.parametrize("enabled", [True, False])
def test_repeated_urls_preserve_order_and_display(
    tmp_path: Path, enabled: bool
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url,criteria,enabled\nFirst,https://example.com/,first,false\n"
        "Other,https://other.example/,,false\n"
        f"Second, https://example.com/ ,second,{str(enabled).lower()}\n"
        f"Second,https://example.com/,second,{str(enabled).lower()}\n",
        encoding="utf-8",
    )
    targets = load_targets(tmp_path / "targets.csv")
    assert [item["url"] for item in targets] == [
        "https://example.com/",
        "https://other.example/",
    ]
    assert targets[0]["interests"] == [
        _interest("First", criteria="first", enabled=False),
        _interest("Second", criteria="second", enabled=enabled),
        _interest("Second", criteria="second", enabled=enabled),
    ]
    assert targets[0]["name"] == ("Second" if enabled else "First")
    assert targets[0]["enabled"] is enabled


def test_distinct_urls_with_colliding_ids_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "targets.csv").write_text(
        "name,url\nA,https://a.example/\nB,https://b.example/\n", encoding="utf-8"
    )
    monkeypatch.setattr(workspace, "_target_id", lambda _url: "collision")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(WorkspaceError, match="duplicate_target_id"):
        check(tmp_path, tmp_path / "targets.csv")


@pytest.mark.parametrize(
    ("rows", "expected_names"),
    [
        (
            (
                "First,https://vendor.example/updates,,,,New rules,5,true\n"
                "Second,https://vendor.example/updates,,,,API,2,true\nThird,https://vendor.example/updates,,,,Third,3,true\n"
            ),
            ["First", "Second", "Third"],
        ),
        (
            "Edited,https://vendor.example/updates,New,Other,new hints,Edited,4,true\n",
            ["Edited"],
        ),
        ("Second,https://vendor.example/updates,,,,API,2,true\n", ["Second"]),
        (
            (
                "First,https://vendor.example/updates,,,,First,1,false\n"
                "Second,https://vendor.example/updates,,,,API,2,true\n"
            ),
            ["Second"],
        ),
        (
            (
                "Second,https://vendor.example/updates,,,,API,2,true\n"
                "First,https://vendor.example/updates,,,,First,1,true\n"
            ),
            ["Second", "First"],
        ),
    ],
    ids=["add", "edit", "remove", "disable", "reorder"],
)
def test_metadata_changes_replace_context_without_rewriting_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: str,
    expected_names: list[str],
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    before = [path.read_bytes() for path in (metadata, candidate, snapshot)]
    stored = workspace._read_pending(tmp_path / "internal" / "state", target_id)
    (tmp_path / "targets.csv").write_text(_ENRICHED_HEADER + rows, encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pending targets must not be refetched")

    monkeypatch.setattr(workspace.monitor, "run", forbidden)
    handle = cast(
        "list[dict[str, object]]",
        check(tmp_path, tmp_path / "targets.csv", compact=True)["targets"],
    )[0]
    assert set(handle) == {
        "action",
        "run_id",
        "target_id",
        "revision",
        "name",
        "diff_truncated",
    }
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    interests = cast("list[dict[str, object]]", review["interests"])
    assert [item["name"] for item in interests] == expected_names
    assert interests == workspace._enabled_interests(
        load_targets(tmp_path / "targets.csv")[0]
    )
    assert review["name"] == expected_names[0]
    for key in ("revision", "run_id", "diff", "diff_truncated", "link_review"):
        assert review[key] == stored[key]
    assert [path.read_bytes() for path in (metadata, candidate, snapshot)] == before


@pytest.mark.parametrize(
    "rows",
    [
        "Other,https://other.example/,,,,,,false\n",
        "Disabled,https://vendor.example/updates,,,,,,false\n",
    ],
    ids=["removed", "all-disabled"],
)
def test_valid_inactive_configuration_has_no_interests_and_check_discards(
    tmp_path: Path, rows: str
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    (tmp_path / "targets.csv").write_text(_ENRICHED_HEADER + rows, encoding="utf-8")
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    assert review["interests"] == []
    assert metadata.exists()
    check(tmp_path, tmp_path / "targets.csv")
    assert not metadata.exists()
    assert not candidate.exists()
    assert snapshot.read_bytes() == b"old\n"


@pytest.mark.parametrize("configuration", [None, "name,url,unknown\n"])
def test_unavailable_configuration_keeps_saved_review_and_direct_finalize(
    tmp_path: Path, configuration: str | None
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    before = [path.read_bytes() for path in (metadata, candidate, snapshot)]
    path = tmp_path / "targets.csv"
    if configuration is None:
        path.unlink()
    else:
        path.write_text(configuration, encoding="utf-8")
    with pytest.raises(WorkspaceError):
        check(tmp_path, tmp_path / "targets.csv")
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    assert [
        item["name"] for item in cast("list[dict[str, object]]", review["interests"])
    ] == ["First", "Second"]
    assert [path.read_bytes() for path in (metadata, candidate, snapshot)] == before
    assert (
        finalize(
            tmp_path,
            {"target_id": target_id, "revision": review["revision"], "material": False},
        )["action"]
        == "finalized"
    )


@pytest.mark.parametrize(
    "interests",
    [
        None,
        {},
        [None],
        [{}],
        [_interest(extra="x")],
        [_interest(name=1)],
        [_interest(name="")],
        [_interest(publisher=None)],
        [_interest(criteria=" untrimmed ")],
        [_interest(enabled=1)],
        [_interest(priority=True)],
        [_interest(priority=0)],
        [_interest(priority=-1)],
        [_interest(priority="1")],
        [_interest(priority=1.5)],
    ],
    ids=[
        "null",
        "object",
        "null-item",
        "missing-keys",
        "extra-keys",
        "name-type",
        "empty-name",
        "text-type",
        "untrimmed",
        "enabled-type",
        "boolean-priority",
        "zero-priority",
        "negative-priority",
        "string-priority",
        "float-priority",
    ],
)
def test_malformed_interests_fail_reads_and_committed_recovery(
    tmp_path: Path, interests: object
) -> None:
    target_id, metadata, _, _ = _create_interest_review(tmp_path)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["interests"] = interests
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="interests are invalid"):
        workspace._read_pending(tmp_path / "internal" / "state", target_id)
    assert not workspace._replacement_matches_commit(
        tmp_path / "internal" / "state", target_id, {"revision": payload["revision"]}
    )


@pytest.mark.parametrize("links", [True, False])
def test_current_pending_context_and_replacement_matching(
    tmp_path: Path, links: bool
) -> None:
    target_id, metadata, _, _ = _create_interest_review(tmp_path)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if not links:
        payload.pop("link_review")
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    pending = workspace._read_pending(tmp_path / "internal" / "state", target_id)
    assert pending["interests"] == payload["interests"]
    assert workspace._replacement_matches_commit(
        tmp_path / "internal" / "state", target_id, {"revision": pending["revision"]}
    )


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_pending_serialized_size_boundary_preserves_existing_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_bytes: int
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    payload = workspace._read_pending(tmp_path / "internal" / "state", target_id)
    size = len(workspace._serialize_pending(payload))
    before = [path.read_bytes() for path in (metadata, candidate, snapshot)]
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT_BYTES", size - extra_bytes)
    if extra_bytes:
        with pytest.raises(WorkspaceError, match="pending decision size"):
            workspace._write_pending_transaction(
                tmp_path / "internal" / "state", payload, b"new\n"
            )
        with pytest.raises(WorkspaceError, match="pending decision size"):
            workspace._read_pending(tmp_path / "internal" / "state", target_id)
        assert not workspace._replacement_matches_commit(
            tmp_path / "internal" / "state",
            target_id,
            {"revision": payload["revision"]},
        )
        assert [path.read_bytes() for path in (metadata, candidate, snapshot)] == before
    else:
        assert workspace._serialize_pending(payload) == metadata.read_bytes()
        assert (
            workspace._read_pending(tmp_path / "internal" / "state", target_id)
            == payload
        )


@pytest.mark.parametrize(
    ("limit", "reader"),
    [("_MAX_SNAPSHOT_BYTES", "_read_pending_json"), ("_MAX_CSV_BYTES", "_read_csv")],
    ids=["pending", "csv"],
)
def test_read_bounds_actual_bytes_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, reader: str
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"x" * 101)
    monkeypatch.setattr(workspace, limit, 100)
    original_lstat = Path.lstat

    def small_stat(self: Path) -> os.stat_result:
        info = list(original_lstat(self))
        info[6] = 1
        return os.stat_result(info)

    monkeypatch.setattr(Path, "lstat", small_stat)
    with pytest.raises(WorkspaceError, match="size is invalid"):
        getattr(workspace, reader)(path)


def test_shared_url_fetch_and_finalization_use_one_transaction_and_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "targets.csv").write_text(
        _ENRICHED_HEADER
        + "Plans,https://vendor.example/updates,Vendor,Product,plans,Limits,1,true\n"
        + "API,https://vendor.example/updates,Vendor,Product,API,Breaking,2,true\n"
        + "Disabled,https://vendor.example/updates,,,,,,false\n"
        + "Other,https://other.example/,,,,,,false\n",
        encoding="utf-8",
    )
    parent = b"old\n"
    calls: list[str] = []

    def fetch(url: str, **_kwargs: object) -> workspace.monitor.Document:
        calls.append(url)
        return workspace.monitor.Document(parent, url, "text/plain")

    monkeypatch.setattr(workspace.monitor, "fetch_document", fetch)
    assert (
        cast(
            "list[dict[str, object]]",
            check(tmp_path, tmp_path / "targets.csv")["targets"],
        )[0]["action"]
        == "baseline_created"
    )
    parent = b"Breaking API change\n"
    review = cast(
        "list[dict[str, object]]", check(tmp_path, tmp_path / "targets.csv")["targets"]
    )[0]
    assert calls == ["https://vendor.example/updates"] * 2
    assert review["interests"] == [
        _interest(
            "Plans",
            publisher="Vendor",
            category="Product",
            keywords="plans",
            criteria="Limits",
            priority=1,
        ),
        _interest(
            "API",
            publisher="Vendor",
            category="Product",
            keywords="API",
            criteria="Breaking",
            priority=2,
        ),
    ]
    assert len(list((tmp_path / "internal" / "state" / "pending").iterdir())) == 1
    decision = {
        "target_id": review["target_id"],
        "revision": review["revision"],
        "material": True,
        "report": "## API\n\nBreaking API change affects the API interest.\n",
    }
    result = finalize(tmp_path, decision)
    report = Path(str(result["report_path"])).read_text(encoding="utf-8")
    assert report.count("## API") == 1
    assert report.count(":start -->") == 1
    assert workspace.pending_reviews(tmp_path, targets=tmp_path / "targets.csv") == {
        "reviews": []
    }


@pytest.mark.parametrize(
    "choose_previous", [False, True], ids=["committed", "previous"]
)
def test_enriched_replacement_recovery_keeps_both_revision_choices(
    tmp_path: Path, choose_previous: bool
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    state = tmp_path / "internal" / "state"
    previous = workspace._read_pending(state, target_id)
    undo = workspace._capture_pending_replacement(state, target_id)
    replacement = {
        **previous,
        "revision": "b" * 32,
        "name": "Replacement",
        "interests": [_interest("Replacement", criteria="new rules")],
    }
    workspace._write_recovery_record(state, undo)
    workspace._write_pending_file(metadata, replacement)
    workspace._write_commit_record(
        state, _commit_record(target_id=target_id, revision=replacement["revision"])
    )
    (tmp_path / "targets.csv").unlink()
    review = cast(
        "list[dict[str, object]]",
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )["reviews"],
    )[0]
    assert review["interests"] == replacement["interests"]
    assert (state / "recovery" / f"{target_id}.json").exists()
    chosen = previous if choose_previous else replacement
    assert (
        finalize(
            tmp_path,
            {"target_id": target_id, "revision": chosen["revision"], "material": False},
        )["action"]
        == "finalized"
    )
    assert snapshot.read_bytes() == b"new\n"
    assert not candidate.exists()
    assert not metadata.exists()
    assert not list((state / "recovery").iterdir())


@pytest.mark.parametrize("corruption", ["interests", "link_review", "diff"])
def test_enriched_committed_recovery_rejects_malformed_review_metadata(
    tmp_path: Path, corruption: str
) -> None:
    target_id, metadata, candidate, snapshot = _create_interest_review(tmp_path)
    state = tmp_path / "internal" / "state"
    payload = workspace._read_pending(state, target_id)
    payload[corruption] = False
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    before = [path.read_bytes() for path in (metadata, candidate, snapshot)]
    workspace._write_recovery_record(state, _replace_record(target_id=target_id))
    workspace._write_commit_record(
        state, _commit_record(target_id=target_id, revision=payload["revision"])
    )
    with pytest.raises(WorkspaceError, match="committed replacement is incomplete"):
        workspace.pending_reviews(
            tmp_path, targets=tmp_path / "targets.csv", target_id=target_id
        )
    with pytest.raises(WorkspaceError, match="committed replacement is incomplete"):
        finalize(
            tmp_path,
            {
                "target_id": target_id,
                "revision": payload["revision"],
                "material": False,
            },
        )
    assert [path.read_bytes() for path in (metadata, candidate, snapshot)] == before


def test_check_reconciliation_uses_one_configuration_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    _write_review_transaction(state)
    _write_targets(tmp_path / "targets.csv", "Other,https://other.example/,,false\n")
    original = workspace.load_targets
    calls = 0

    def load_once(path: str | Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        assert calls == 1
        return original(path)

    monkeypatch.setattr(workspace, "load_targets", load_once)
    assert (
        cast(
            "list[dict[str, object]]",
            check(tmp_path, tmp_path / "targets.csv")["targets"],
        )[0]["action"]
        == "skipped"
    )
    assert calls == 1


@pytest.mark.parametrize("entry", ["file", "symlink"])
def test_remove_pending_rejects_unsafe_target(tmp_path: Path, entry: str) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.parent.mkdir(parents=True)
    if entry == "file":
        target.write_text("preserve", encoding="utf-8")
    else:
        target.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="pending target must be"):
        workspace._remove_pending(state, "example")
    assert target.exists()


def test_restore_existing_pending_group_preserves_undo_on_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "internal" / "state"
    target = state / "pending" / "example"
    target.mkdir(parents=True)
    original_fsync = workspace._fsync_directory

    def fail_group_sync(path: Path) -> None:
        if path == target:
            message = "injected"
            raise OSError(message)
        original_fsync(path)

    monkeypatch.setattr(workspace, "_fsync_directory", fail_group_sync)
    record = _replace_record(group_dir_existed=True)
    workspace._write_recovery_record(state, record)
    with pytest.raises(
        WorkspaceError, match="cannot fsync restored pending transaction"
    ):
        workspace._recover_pending(state, "example")
    assert workspace._read_recovery_record(state, "example") == record
