"""Tests for the compact monitor helper."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from monitor import (
    Document,
    MonitorError,
    _validate_public_url,
    compare_text,
    normalize_document,
    run,
)


def test_normalize_html_removes_markup_and_scripts() -> None:
    document = Document(
        b"<html><body><h1>Hello</h1><script>ignore()</script>"
        b"<p>World</p></body></html>",
        "https://example.com/",
        "text/html",
    )

    assert normalize_document(document) == "Hello\nWorld\n"


def test_compare_text_reports_baseline_unchanged_and_change() -> None:
    baseline = compare_text("alpha\n", None, max_diff_lines=20)
    unchanged = compare_text("alpha\n", "alpha\n", max_diff_lines=20)
    changed = compare_text("beta\n", "alpha\n", max_diff_lines=20)

    assert baseline["status"] == "baseline"
    assert unchanged["status"] == "unchanged"
    assert changed["status"] == "changed"
    assert "-alpha" in str(changed["diff"])
    assert "+beta" in str(changed["diff"])


def test_compare_text_bounds_diff() -> None:
    previous = "\n".join(f"old-{index}" for index in range(20))
    current = "\n".join(f"new-{index}" for index in range(20))

    result = compare_text(current, previous, max_diff_lines=4)

    assert result["diff_truncated"] is True
    assert len(str(result["diff"]).splitlines()) == 4


def test_private_literal_ip_is_rejected() -> None:
    with pytest.raises(MonitorError, match="public IP"):
        _validate_public_url("http://127.0.0.1/")


def test_run_local_file_writes_normalized_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    output = tmp_path / "snapshot.txt"
    source.write_text("<main> Alpha   Beta </main>")
    args = Namespace(
        url=None,
        input=source,
        source_url="https://example.com/",
        content_type="text/html",
        previous=None,
        output=output,
        timeout=30.0,
        max_bytes=1024,
        max_diff_lines=20,
    )

    result = run(args)

    assert result["status"] == "baseline"
    assert output.read_text() == "Alpha Beta\n"
