"""Regression tests for the compact monitor review fixes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import monitor
from monitor import Document, compare_text, normalize_document

if TYPE_CHECKING:
    import pytest


def test_feed_normalization_preserves_field_boundaries() -> None:
    left = normalize_document(
        Document(
            b'<rss><channel><link href="ab"/><link href="c"/></channel></rss>',
            "https://example.com/feed.xml",
            "application/rss+xml",
        )
    )
    right = normalize_document(
        Document(
            b'<rss><channel><link href="a"/><link href="bc"/></channel></rss>',
            "https://example.com/feed.xml",
            "application/rss+xml",
        )
    )

    assert left != right


def test_diff_complexity_short_circuits_before_unified_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_unified_diff(
        before: list[str],
        after: list[str],
        *,
        fromfile: str,
        tofile: str,
        lineterm: str,
    ) -> None:
        del before, after, fromfile, tofile, lineterm
        message = "unified_diff must not run past the complexity budget"
        raise AssertionError(message)

    monkeypatch.setattr(monitor, "unified_diff", fail_unified_diff)
    previous = "\n".join(f"old-{index}" for index in range(2_100))
    current = "\n".join(f"new-{index}" for index in range(2_100))

    result = compare_text(current, previous, max_diff_lines=20)

    assert result["status"] == "changed"
    assert result["diff_truncated"] is True
    assert "complexity limit exceeded" in str(result["diff"])


def test_html_served_as_text_plain_still_uses_html_normalization() -> None:
    document = Document(
        b"<html><body><main>Hello</main><script>ignore()</script></body></html>",
        "https://example.com/",
        "text/plain",
    )

    assert normalize_document(document) == "Hello\n"
