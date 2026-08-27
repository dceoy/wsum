"""Regression tests for resolved pull-request review findings."""

from __future__ import annotations

import pytest
from monitor import Document, compare_text, normalize_document


@pytest.mark.parametrize(
    ("old_html", "new_html"),
    [
        (
            b'<a href="/v1">Download</a>',
            b'<a href="/v2">Download</a>',
        ),
        (
            b'<form action="/v1"><button>Submit</button></form>',
            b'<form action="/v2"><button>Submit</button></form>',
        ),
    ],
    ids=["link", "form"],
)
def test_html_destination_only_change_is_detected(
    old_html: bytes, new_html: bytes
) -> None:
    previous = normalize_document(
        Document(old_html, "https://example.com/", "text/html")
    )
    current = normalize_document(
        Document(new_html, "https://example.com/", "text/html")
    )

    assert previous != current
    assert "/v1" not in previous
    assert "/v2" not in current
    result = compare_text(current, previous, max_diff_lines=20)
    assert result["status"] == "changed"
