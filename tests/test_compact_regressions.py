"""Regression tests for the compact monitor review fixes."""

from __future__ import annotations

import monitor
import pytest
from monitor import Document, compare_text, normalize_document


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


def test_diff_complexity_counts_non_lf_line_boundaries() -> None:
    previous = "old\r" * 2_100
    current = "new\r" * 2_100

    result = compare_text(current, previous, max_diff_lines=20)

    assert result["diff_truncated"] is True
    assert "complexity limit exceeded" in str(result["diff"])


def test_html_served_as_text_plain_still_uses_html_normalization() -> None:
    document = Document(
        b"<html><body><main>Hello</main><script>ignore()</script></body></html>",
        "https://example.com/",
        "text/plain",
    )

    assert normalize_document(document) == "Hello\n"


def test_feed_destination_identity_tracks_xml_base_without_raw_url() -> None:
    previous = normalize_document(
        Document(
            b'<rss xml:base="https://a.example/"><channel><link href="item"/>'
            b"</channel></rss>",
            "https://example.com/feed.xml",
            "application/rss+xml",
        )
    )
    current = normalize_document(
        Document(
            b'<rss xml:base="https://b.example/"><channel><link href="item"/>'
            b"</channel></rss>",
            "https://example.com/feed.xml",
            "application/rss+xml",
        )
    )

    assert previous != current
    assert "https://a.example/item" not in previous
    assert "https://b.example/item" not in current


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.com/item?token=secret",
        "https://example.com/item?key=secret",
        "https://example.com/item?safe=1;token=secret",
        "https://example.com/item?api_token=secret",
        "https://example.com/item?api-token=secret",
        (
            "https://hooks.slack.com/services/"
            "T00000000/B00000000/"
            "XXXXXXXXXXXXXXXXXXXXXXXX"
        ),
    ],
    ids=[
        "query-credential",
        "key-credential",
        "semicolon-credential",
        "underscore-api-token",
        "hyphen-api-token",
        "webhook",
    ],
)
def test_feed_rejects_credential_bearing_destinations(destination: str) -> None:
    body = f'<rss><channel><link href="{destination}"/></channel></rss>'.encode()

    with pytest.raises(monitor.MonitorError, match="credentials"):
        normalize_document(
            Document(body, "https://example.com/feed.xml", "application/rss+xml")
        )


def test_feed_text_link_is_hashed_without_raw_url() -> None:
    destination = "https://example.com/item"
    body = f"<rss><channel><link>{destination}</link></channel></rss>".encode()

    normalized = normalize_document(
        Document(body, "https://example.com/feed.xml", "application/rss+xml")
    )

    assert destination not in normalized
    assert monitor.hashlib.sha256(destination.encode()).hexdigest() in normalized


@pytest.mark.parametrize(
    "destination",
    [
        "https://user:pass@example.com/item",
        "https://example.com/item?token=secret",
        "https://hooks.slack.com/services/T00000000/B00000000/" + "X" * 24,
    ],
    ids=["userinfo", "query-credential", "webhook"],
)
def test_html_rejects_credential_bearing_destinations(destination: str) -> None:
    document = Document(
        f'<main><a href="{destination}">Link</a></main>'.encode(),
        "https://example.com/",
        "text/html",
    )

    with pytest.raises(monitor.MonitorError, match="credentials"):
        normalize_document(document)


@pytest.mark.parametrize(
    ("before_href", "after_href"),
    [("/v1", "/v2"), ("https://example.com/v1", "https://example.com/v2")],
    ids=["relative", "absolute"],
)
def test_feed_embedded_html_destination_change_is_detected(
    before_href: str, after_href: str
) -> None:
    def make_document(href: str) -> Document:
        body = (
            "<rss><channel><item><description><![CDATA["
            f'<a href="{href}">Apply</a>'
            "]]></description></item></channel></rss>"
        ).encode()
        return Document(body, "https://example.com/feed", "application/rss+xml")

    previous = normalize_document(make_document(before_href))
    current = normalize_document(make_document(after_href))

    assert previous != current
    assert before_href not in previous
    assert after_href not in current


def test_feed_embedded_html_rejects_credential_bearing_destination() -> None:
    document = Document(
        b"<rss><channel><item><description><![CDATA["
        b'<a href="https://example.com/?token=secret">Apply</a>'
        b"]]></description></item></channel></rss>",
        "https://example.com/feed",
        "application/rss+xml",
    )

    with pytest.raises(monitor.MonitorError, match="credentials"):
        normalize_document(document)


@pytest.mark.parametrize(
    ("before_href", "after_href"),
    [("/v1", "/v2"), ("https://example.com/v1", "https://example.com/v2")],
    ids=["relative", "absolute"],
)
def test_atom_inline_xhtml_destination_change_is_detected(
    before_href: str, after_href: str
) -> None:
    def make_document(href: str) -> Document:
        body = (
            '<feed><entry><id>entry-1</id><content type="xhtml">'
            '<div xmlns="http://www.w3.org/1999/xhtml">'
            f'<a href="{href}">Apply</a>'
            "</div></content></entry></feed>"
        ).encode()
        return Document(body, "https://example.com/feed", "application/atom+xml")

    previous = normalize_document(make_document(before_href))
    current = normalize_document(make_document(after_href))

    assert previous != current
    assert before_href not in previous
    assert after_href not in current


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            (
                b"<rss><channel><item><guid>2</guid><title>B</title></item>"
                b"<item><guid>1</guid><title>A</title></item></channel></rss>"
            ),
            (
                b"<rss><channel><item><guid>1</guid><title>A</title></item>"
                b"<item><guid>2</guid><title>B</title></item></channel></rss>"
            ),
        ),
        (
            (
                b"<feed><entry><id>2</id><title>B</title></entry>"
                b"<entry><id>1</id><title>A</title></entry></feed>"
            ),
            (
                b"<feed><entry><id>1</id><title>A</title></entry>"
                b"<entry><id>2</id><title>B</title></entry></feed>"
            ),
        ),
    ],
    ids=["rss", "atom"],
)
def test_feed_entry_reordering_is_ignored(before: bytes, after: bytes) -> None:
    previous = normalize_document(
        Document(before, "https://example.com/feed", "application/rss+xml")
    )
    current = normalize_document(
        Document(after, "https://example.com/feed", "application/rss+xml")
    )

    assert current == previous


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><root xml:base="' + b"a" * 4_097 + b'"/>',
        (
            b'<?xml version="1.0"?><root>'
            + b'<node xml:base="x">' * 201
            + b"text"
            + b"</node>" * 201
            + b"</root>"
        ),
    ],
    ids=["base-url-length", "nesting-depth"],
)
def test_xml_base_processing_is_bounded(body: bytes) -> None:
    with pytest.raises(monitor.MonitorError, match=r"base URL|nesting"):
        normalize_document(
            Document(body, "https://example.com/feed", "application/xml")
        )
