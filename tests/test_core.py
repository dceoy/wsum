"""Tests for the Gemini hybrid deterministic core."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import hashlib
import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import core
import pytest
from core import Document, LinkCollection, MonitorError, WsumError

_B64_BAD = "!!!not-base64"


def _doc(
    body: bytes | str,
    content_type: str = "text/html",
    *,
    charset: str | None = None,
    url: str = "https://example.com/base/",
) -> Document:
    raw = body.encode() if isinstance(body, str) else body
    return Document(raw, url, content_type, charset)


def _normalize(
    body: bytes | str,
    content_type: str = "text/html",
    *,
    charset: str | None = None,
    url: str = "https://example.com/base/",
) -> tuple[str, LinkCollection]:
    links = LinkCollection()
    text = core.normalize_document(
        _doc(body, content_type, charset=charset, url=url), links=links
    )
    return text, links


# --- content type, encodings, decoding -------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text/html; charset=UTF-8", ("text/html", "UTF-8")),
        ("Text/Plain", ("text/plain", None)),
        ("", ("text/plain", None)),
    ],
)
def test_parse_content_type(value: str, expected: tuple[str, str | None]) -> None:
    assert core._parse_content_type(value) == expected


def test_parse_content_type_charset_tuple_is_ignored() -> None:
    value = "text/html; charset*=utf-8''utf-8"
    assert core._parse_content_type(value)[1] is None


def test_parse_content_type_wraps_header_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidMessage:
        def __setitem__(self, _name: str, _value: str) -> None:
            raise ValueError("bad header")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

    monkeypatch.setattr(core, "Message", InvalidMessage)
    with pytest.raises(MonitorError, match="content type is invalid"):
        core._parse_content_type("text/html")


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("<rss version='2'>", "application/rss+xml"),
        ("<atom:feed>", "application/rss+xml"),
        ("<?xml version='1.0'?><html>", "text/html"),
        ("<?xml version='1.0'?><!doctype html>", "text/html"),
        ("<?xml version='1.0'?><root>", "application/xml"),
        ("<div>hi</div>", "text/html"),
        ("just words", "text/plain"),
    ],
)
def test_sniff_text_content_type(sample: str, expected: str) -> None:
    assert core._sniff_text_content_type(sample) == expected


@pytest.mark.parametrize(
    ("body", "charset", "expected"),
    [
        (b"%PDF-1.4", None, "application/pdf"),
        (b"<html>", None, "text/html"),
        ("<html>".encode("utf-16"), None, "text/html"),
        ("<html>".encode("utf-16-le"), "utf-16-le", "text/html"),
        (b"<html>", "no-such-codec", "text/html"),
        (b"\xff\xfe\xfa\xd8\xff", None, "text/plain"),
        (b"\xe3\x81", "utf-8", "text/plain"),
        ("<html>あ".encode()[:-1], "utf-8", "text/html"),
    ],
)
def test_sniff_content_type(body: bytes, charset: str | None, expected: str) -> None:
    assert core._sniff_content_type(body, charset=charset) == expected


@pytest.mark.parametrize(
    ("declared", "body", "expected"),
    [
        ("text/plain", b"<div>x</div>", "text/html"),
        ("application/xml", b"<rss>", "application/rss+xml"),
        ("application/xhtml+xml", b"<html>", "text/html"),
        ("application/pdf", b"%PDF-1.7", "application/pdf"),
    ],
)
def test_normalization_content_type_accepts(
    declared: str, body: bytes, expected: str
) -> None:
    assert core._normalization_content_type(_doc(body, declared)) == expected


@pytest.mark.parametrize(
    ("declared", "body", "message"),
    [
        ("image/png", b"x", "unsupported"),
        ("application/pdf", b"<html>", "does not match"),
        ("text/html", b"plain", "does not match"),
    ],
)
def test_normalization_content_type_rejects(
    declared: str, body: bytes, message: str
) -> None:
    with pytest.raises(MonitorError, match=message):
        core._normalization_content_type(_doc(body, declared))


@pytest.mark.parametrize(
    "value",
    ["utf-8", " 'UTF-8' ", "latin1", "sjis", "UTF-16LE"],
)
def test_encoding_name_accepts(value: str) -> None:
    assert core._encoding_name(value) in core._SUPPORTED_TEXT_ENCODINGS


@pytest.mark.parametrize("value", ["", "x" * 65, "no-such-codec", "rot13", "koi8-r"])
def test_encoding_name_rejects(value: str) -> None:
    with pytest.raises(MonitorError, match="document encoding is unsupported"):
        core._encoding_name(value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"\x00\x00\xfe\xffabc", "utf-32"),
        (b"\xef\xbb\xbfabc", "utf-8-sig"),
        (b"\xfe\xffabc", "utf-16"),
        (b"abc", None),
    ],
)
def test_bom_encoding(body: bytes, expected: str | None) -> None:
    assert core._bom_encoding(body) == expected


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (
            b"<?xml version='1.0' encoding='ISO-8859-1'?>",
            "application/xml",
            "ISO-8859-1",
        ),
        (b"<meta charset=shift_jis>", "text/html", "shift_jis"),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=gbk">',
            "text/html",
            "gbk",
        ),
        (b"<p>none</p>", "text/html", None),
        (b"charset=utf-8", "text/plain", None),
    ],
)
def test_in_document_encoding(
    body: bytes, content_type: str, expected: str | None
) -> None:
    assert core._in_document_encoding(body, content_type) == expected


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (_doc(b"\xef\xbb\xbfhi", "text/plain"), "hi"),
        (_doc("é".encode("latin-1"), "text/plain", charset="latin-1"), "é"),
        (_doc("é".encode("cp1252"), "text/html", charset=None), None),
        (
            _doc(b"<meta charset=iso-8859-1>\xe9", "text/html"),
            "<meta charset=iso-8859-1>é",
        ),
        (_doc("plain", "text/plain"), "plain"),
    ],
)
def test_decode_document(document: Document, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(MonitorError, match="could not be decoded"):
            core._decode_document(document)
    else:
        assert core._decode_document(document) == expected


# --- credential detection ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "token",
        "API_Key",
        "x api key",
        "client.secret",
        "X-Amz-Signature",
        "session-token",
    ],
)
def test_sensitive_query_names(name: str) -> None:
    assert core._is_sensitive_query_name(name)


def test_benign_query_name() -> None:
    assert not core._is_sensitive_query_name("page")


@pytest.mark.parametrize(
    ("host", "path", "expected"),
    [
        ("hooks.slack.com.", "/services/T/B/x", True),
        ("hooks.slack.com", "/other", False),
        ("discord.com", "/api/webhooks/1/abc", True),
        ("discordapp.com", "/api/webhooks/1/abc", True),
        ("discord.com", "/channels/1", False),
        ("example.com", "/services/x", False),
    ],
)
def test_webhook_credential_path(host: str, path: str, expected: bool) -> None:
    assert core._is_webhook_credential_path(host, path) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@example.com/",
        "https://user@example.com/",
        "https://:pw@example.com/",
        "https://example.com/?token=abc",
        "https://example.com/?a=1;api_key=x",
        "https://example.com/#access_token=abc",
        "https://hooks.slack.com/services/T/B/x",
        "https://example.com/?next=https%3A%2F%2Fu%3Ap%40h.example%2F",
        "https://example.com/?next=https://h.example/?token=abc",
        "https://example.com/?a=%3Ftoken%3Dabc",
        "https://example.com/?a=%2574oken%3Dabc",
        "https://example.com/?a=%252525252574oken",
        "https://[::1/",
    ],
)
def test_url_has_credentials_true(url: str) -> None:
    assert core.url_has_credentials(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/page?id=1&sort=asc",
        "https://example.com/#section",
        "https://example.com/?q=",
        "https://example.com/?q=plain%20text",
        "https://example.com:bad/",
    ],
)
def test_url_has_credentials_false(url: str) -> None:
    assert not core.url_has_credentials(url)


def test_nested_url_depth_limit() -> None:
    assert core._nested_url_has_credentials("x", depth=6)
    assert core._query_has_credentials("a=b", depth=6)


def test_nested_url_rejects_invalid_netloc_and_hostname() -> None:
    assert core._nested_url_has_credentials("http://[bad", depth=0)


def test_nested_url_clean_and_encoded() -> None:
    assert not core._nested_url_has_credentials("https://h.example/a", depth=0)
    assert not core._nested_url_has_credentials("a%20b", depth=0)
    assert core._nested_url_has_credentials("%2525252525252525", depth=0)


# --- navigation links -------------------------------------------------------


def _marker(url: str) -> str:
    return f"[a:href:sha256:{hashlib.sha256(url.encode()).hexdigest()}]"


@pytest.mark.parametrize(
    "value",
    [
        1,
        "",
        " https://example.com/",
        "https://[::1/",
        "ftp://example.com/",
        "https:///path",
        "https://user:pw@example.com/",
        "https://example.com:0/",
        "https://example.com/" + "a" * 5000,
    ],
)
def test_canonical_navigation_url_rejects(value: object) -> None:
    with pytest.raises(MonitorError, match="navigation-links"):
        core._canonical_navigation_url(value)


def test_canonical_navigation_url_accepts() -> None:
    assert core._canonical_navigation_url("HTTPS://Example.com/a") == (
        "https://Example.com/a"
    )


def test_validate_navigation_links() -> None:
    urls = ["https://a.example/", "https://b.example/", "https://a.example/"]
    assert core._validate_navigation_links(urls) == [
        "https://a.example/",
        "https://b.example/",
    ]
    with pytest.raises(MonitorError, match="JSON array"):
        core._validate_navigation_links("nope")
    with pytest.raises(MonitorError, match="at most 500"):
        core._validate_navigation_links(["https://a.example/"] * 501)


def test_collect_link_variants() -> None:
    links = LinkCollection()
    core._collect_link(links, "mailto:a@example.com")
    core._collect_link(links, "https:///x")
    core._collect_link(links, "https://[bad/")
    core._collect_link(links, "https://example.com:0/x")
    core._collect_link(links, "https://example.com:bad/x")
    assert not links
    core._collect_link(links, "https://example.com/a#frag")
    assert list(links.values()) == ["https://example.com/a"]
    core._collect_link(links, "https://example.com/" + "a" * 5000)
    assert len(links.omitted_hashes) == 1
    plain: dict[str, str] = {}
    core._collect_link(plain, "https://example.com/" + "a" * 5000)
    assert not plain


def test_include_navigation_links() -> None:
    links = LinkCollection()
    assert core._include_navigation_links("t\n", links, []) == "t\n"
    url = "https://example.com/x"
    text = core._include_navigation_links("t\n", links, [url, url])
    assert text == f"t\n{_marker(url)}\n"
    assert core._include_navigation_links(text, links, [url]) == text
    assert list(links.values()) == [url]


def test_navigation_comparison_view_without_markers() -> None:
    view = core._navigation_comparison_view("plain\n")
    assert view == ("plain\n", "plain\n", frozenset(), (), False)


def test_navigation_comparison_view_trailing_manifest() -> None:
    a, b = _marker("https://a.example/"), _marker("https://b.example/")
    body, comparison, identities, placements, is_suffix = (
        core._navigation_comparison_view(f"hello\n{b}\n\n{a}\n")
    )
    assert body == "hello\n"
    assert comparison.startswith("hello\n")
    assert len(identities) == 2
    assert [p[1] for p in placements] == [1, 1]
    assert is_suffix


def test_navigation_comparison_view_inline_markers() -> None:
    a = _marker("https://a.example/")
    *_, is_suffix = core._navigation_comparison_view(f"one\n{a}\ntwo\n")
    assert not is_suffix


def _view(text: str) -> Any:
    return core._navigation_comparison_view(text)


def test_navigation_views_match_rules() -> None:
    a = _marker("https://a.example/")
    suffix = _view(f"body\n{a}\n")
    plain = _view("body\n")
    inline_a = _view(f"one\n{a}\ntwo\n")
    assert core._navigation_comparison_views_match(suffix, suffix)
    assert not core._navigation_comparison_views_match(plain, suffix)
    assert not core._navigation_comparison_views_match(inline_a, inline_a)
    assert core._navigation_comparison_views_match(inline_a, _view(f"one\ntwo\n{a}\n"))
    assert not core._navigation_comparison_views_match(
        inline_a, _view(f"one\ntwo\n{_marker('https://z.example/')}\n")
    )


def test_navigation_comparison_pair() -> None:
    a = _marker("https://a.example/")
    assert core._navigation_comparison_pair("x\n", "y\n") is None
    pair = core._navigation_comparison_pair(f"x\n{a}\n", f"x\n{a}\n")
    assert pair is not None


# --- HTML / feed normalization ---------------------------------------------


def test_html_text_and_destinations() -> None:
    html = (
        "<html><head><script>bad()</script><base href='/root/'></head>"
        "<body><p>Hello<br>there</p><div>x</div>"
        "<a href='page'>l</a><area href='https://o.example/a#f'>"
        "<form action='/go'><button formaction='/b'></button>"
        "<input formaction='/i'></form>"
        "<noscript><a href='/skip'>s</a></noscript></body></html>"
    )
    text, links = _normalize(html)
    assert "bad()" not in text
    assert "Hello\nthere" in text
    assert text.count("[a:href:sha256:") == 1
    assert "[form:action:" in text
    assert "[button:formaction:" in text
    assert "[input:formaction:" in text
    assert sorted(links.values()) == [
        "https://example.com/root/page",
        "https://o.example/a",
    ]


def test_html_base_only_first_and_empty_attrs() -> None:
    html = (
        "<base href='/one/'><base href='/two/'><base><a href=''>e</a>"
        "<a href='x'>x</a><b>bold</b><p>p</p>"
    )
    text, links = _normalize(html)
    assert list(links.values()) == ["https://example.com/one/x"]
    assert "bold" in text


def test_html_destination_with_credentials_raises() -> None:
    with pytest.raises(MonitorError, match="HTML destination contains credentials"):
        _normalize("<a href='https://u:p@example.com/'>x</a>")


def test_html_destination_overflow_is_summarized() -> None:
    count = core._MAX_HTML_DESTINATIONS + 3
    html = "".join(f"<a href='/p{i}'>l</a>" for i in range(count))
    text, links = _normalize(html)
    assert "[omitted-destinations:3:sha256:" in text
    assert links.overflow_count == 3
    assert len(links) == core._MAX_HTML_DESTINATIONS


def test_html_close_is_idempotent_for_omitted_marker() -> None:
    parser = core._TextExtractor("https://example.com/")
    parser.feed("".join(f"<form action='/{i}'>" for i in range(502)))
    parser.close()
    parts = len(parser.parts)
    parser.close()
    assert len(parser.parts) == parts


def test_html_unbalanced_end_tags() -> None:
    text, _ = _normalize("<p></p></script></div>text")
    assert text == "text\n"


def test_embedded_fragment_helpers() -> None:
    assert core._normalize_html_fragment("plain & text", "https://e.example/") == (
        "plain & text"
    )
    assert core._normalize_html_fragment("a < b", "https://e.example/") == "a < b"
    links = LinkCollection()
    out = core._normalize_html_fragment(
        "<p>hi <a href='/x'>x</a></p>", "https://e.example/", links
    )
    assert "hi" in out
    assert list(links.values()) == ["https://e.example/x"]
    assert core._normalize_html_fragment("<b>x</b>", "https://e.example/") == "x"


def test_merge_link_collection() -> None:
    source = LinkCollection()
    source["h"] = "https://a.example/"
    source.omitted_hashes.add("o")
    source.overflow_count = 2
    target = LinkCollection()
    core._merge_link_collection(target, source)
    assert dict(target) == {"h": "https://a.example/"}
    assert target.omitted_hashes == {"o"}
    assert target.overflow_count == 2
    plain: dict[str, str] = {}
    core._merge_link_collection(plain, source)
    assert plain == {"h": "https://a.example/"}


def test_text_and_whitespace_normalization() -> None:
    text, _ = _normalize("  a \t b \r\n\r\n c\r", "text/plain")
    assert text == "a b\nc\n"
    assert core._normalize_whitespace(" \n ") == ""
