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


# --- feeds -------------------------------------------------------------------

_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://feed.example/">
  <title>T</title>
  <entry>
    <id>urn:uuid:2</id>
    <link rel="alternate" href="/two"/>
    <link rel="enclosure" href="/two.mp3"/>
    <summary type="html">&lt;p&gt;second &lt;a href="/in"&gt;in&lt;/a&gt;&lt;/p&gt;</summary>
  </entry>
  <entry xml:base="sub/">
    <id>https://feed.example/one</id>
    <link href="one"/>
    <content>plain &lt; text</content>
  </entry>
</feed>"""

_RSS = """<rss version="2.0"><channel><title>C</title>
<item><guid>g2</guid><link>https://feed.example/b</link><description>b</description></item>
<item><link>https://feed.example/a</link><description>a</description></item>
<item><title>no identity</title></item>
<item><guid></guid><title>empty guid</title></item>
</channel></rss>"""


def test_atom_feed_normalization() -> None:
    text, links = _normalize(_ATOM, "application/atom+xml")
    assert "[entry:start]" in text
    assert "[link:href:sha256:" in text
    assert text.count("[link:href:sha256:") == 3
    assert "[id:sha256:" in text
    assert "urn:uuid:2" not in text
    assert "plain < text" in text
    assert sorted(links.values()) == [
        "https://feed.example/in",
        "https://feed.example/sub/one",
        "https://feed.example/two",
    ]


def test_atom_feed_is_entry_order_independent() -> None:
    head, rest = _ATOM.split("  <entry>", 1)
    first_entry, second = rest.split("  <entry xml:base", 1)
    second_entry = "  <entry xml:base" + second.replace("</feed>", "")
    swapped = f"{head}{second_entry}  <entry>{first_entry}</feed>"
    original = _normalize(_ATOM, "application/atom+xml")[0]
    reordered = _normalize(swapped, "application/atom+xml")[0]
    assert original == reordered
    assert original.count("[entry:start]") == 2


def test_rss_feed_sniffed_from_xml_declaration() -> None:
    text, links = _normalize(_RSS, "application/xml")
    assert "[item:start]" in text
    assert "[guid:start]\ng2\n[guid:end]" in text
    assert len(links) == 2
    assert "empty guid" in text


def test_rss_text_link_destinations() -> None:
    text, links = _normalize(_RSS, "application/rss+xml")
    assert sorted(links.values()) == [
        "https://feed.example/a",
        "https://feed.example/b",
    ]
    assert "[link:href:sha256:" in text


def test_generic_xml_is_not_structured() -> None:
    text, links = _normalize(
        "<?xml version='1.0'?><root><a href='/x'>t</a>  <b>u</b></root>",
        "application/xml",
    )
    assert text == "t u\n"
    assert not links


def test_feed_link_with_relative_non_alternate_is_not_collected() -> None:
    xml = (
        "<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>i</id>"
        "<link rel='self' href='https://feed.example/self'/>"
        "<link rel='alternate' href='https://feed.example/alt'/></entry></feed>"
    )
    text, links = _normalize(xml, "application/atom+xml")
    assert list(links.values()) == ["https://feed.example/alt"]
    assert text.count("[link:href:sha256:") == 2


def test_feed_nested_entry_and_non_entry_data() -> None:
    xml = (
        "<rss><channel>text before<item><item><guid>x</guid></item>inner</item>"
        "tail</channel></rss>"
    )
    text, _ = _normalize(xml, "application/rss+xml")
    assert "text before" in text
    assert "inner" in text
    assert "tail" in text


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<!DOCTYPE rss><rss/>", "DOCTYPE and entity"),
        ("<!entity x 'y'><rss/>", "DOCTYPE and entity"),
        ("<rss><item></rss>", "could not be parsed"),
        (
            "<rss><item><link>https://u:p@x.example/</link></item></rss>",
            "feed destination",
        ),
        (
            "<rss><item><guid>https://u:p@x.example/</guid></item></rss>",
            "feed identity",
        ),
        (
            "<feed><entry><link href='https://u:p@x.example/'/></entry></feed>",
            "feed destination",
        ),
    ],
)
def test_feed_errors(xml: str, message: str) -> None:
    with pytest.raises(MonitorError, match=message):
        core._normalize_feed(
            xml,
            max_chars=10_000,
            preserve_structure=True,
            base_url="https://e.example/",
        )


def test_feed_doctype_handler_rejects_directly() -> None:
    with pytest.raises(MonitorError, match="DOCTYPE"):
        core._FeedTextTarget.doctype("n", "p", "s")


def test_feed_invalid_parser_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeParser:
        def __init__(self, *, target: object) -> None:
            del target

        def feed(self, text: str) -> None:
            del text

        def close(self) -> object:
            return None

    monkeypatch.setattr(core.ET, "XMLParser", FakeParser)
    with pytest.raises(MonitorError, match="invalid result"):
        core._normalize_feed(
            "<a/>", max_chars=100, preserve_structure=False, base_url=""
        )


def test_feed_size_limits() -> None:
    with pytest.raises(MonitorError, match="base URL exceeds"):
        core._FeedTextTarget(10, preserve_structure=True, base_url="h" * 5000)
    with pytest.raises(MonitorError, match="extracted text exceeds"):
        core._normalize_feed(
            "<a>" + "x" * 50 + "</a>",
            max_chars=10,
            preserve_structure=False,
            base_url="",
        )
    with pytest.raises(MonitorError, match="extracted text exceeds"):
        core._normalize_feed(
            "<rss><item><description>" + "x" * 50 + "</description></item></rss>",
            max_chars=40,
            preserve_structure=True,
            base_url="",
        )
    deep = "<a>" * (core._DEFAULT_MAX_XML_DEPTH + 1)
    with pytest.raises(MonitorError, match="nesting exceeds"):
        core._normalize_feed(
            deep, max_chars=10**7, preserve_structure=False, base_url=""
        )


def test_feed_base_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    long_base = "https://e.example/" + "p" * 4100
    with pytest.raises(MonitorError, match="base URL exceeds"):
        core._normalize_feed(
            f"<a xml:base='{long_base}'/>",
            max_chars=10**6,
            preserve_structure=False,
            base_url="",
        )
    with pytest.raises(MonitorError, match="base URL exceeds"):
        core._normalize_feed(
            f"<a xml:base='{'x' * 4090}'/>",
            max_chars=10**6,
            preserve_structure=False,
            base_url="https://e.example/dir/",
        )
    monkeypatch.setattr(core, "_DEFAULT_MAX_XML_BASE_STACK_BYTES", 10)
    with pytest.raises(MonitorError, match="base stack exceeds"):
        core._normalize_feed(
            "<a><b/></a>",
            max_chars=10**6,
            preserve_structure=False,
            base_url="https://e.example/",
        )
    ok = core._normalize_feed(
        "<a xml:base=''>t</a>",
        max_chars=10**6,
        preserve_structure=False,
        base_url="",
    )
    assert ok == "t\n"


def test_feed_identity_token() -> None:
    assert core._feed_identity_token("guid", "plain-id", "https://e.example/") == (
        "plain-id"
    )
    assert core._feed_identity_token("id", "//e.example/p", "https://x/") == (
        f"[id:sha256:{hashlib.sha256(b'https://e.example/p').hexdigest()}]"
    )
    mailto = core._feed_identity_token("id", "mailto:a@b.example", "")
    assert mailto == f"[id:sha256:{hashlib.sha256(b'mailto:a@b.example').hexdigest()}]"
    http = core._feed_identity_token("id", "https://x.example/y", "")
    assert http.startswith("[id:sha256:")
    with pytest.raises(MonitorError, match="feed identity is invalid"):
        core._feed_identity_token("id", "http://[bad", "")


# --- PDF ---------------------------------------------------------------------


def _build_pdf(
    pages: list[dict[str, str]] | None = None,
    extra: dict[int, str] | None = None,
) -> bytes:
    """Build a small valid PDF; page dicts hold raw dictionary entries."""
    pages = pages if pages is not None else [{}]
    objects: dict[int, str] = {}
    page_ids = [10 + i for i in range(len(pages))]
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for page_id, entries in zip(page_ids, pages, strict=True):
        objects[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            + " ".join(f"{k} {v}" for k, v in entries.items())
            + " >>"
        )
    objects.update(extra or {})
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number, body in sorted(objects.items()):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        if number in offsets:
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _stream(data: str, extra: str = "") -> str:
    return f"<< /Length {len(data)} {extra} >>\nstream\n{data}\nendstream"


_CMAP = (
    "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
    "1 begincodespacerange <00> <FF> endcodespacerange\n"
    "1 beginbfchar <41> <0042> endbfchar\nendcmap end end"
)


def _text_pdf(text: str = "Hello PDF", annot: str = "") -> bytes:
    page = {
        "/Contents": "4 0 R",
        "/Resources": "<< /Font << /F1 3 0 R >> >>",
    }
    if annot:
        page["/Annots"] = f"[{annot}]"
    return _build_pdf([page], {4: _stream(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET")})


def test_pdf_text_extraction_and_links() -> None:
    uri = "https://example.org/doc"
    annots = (
        "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
        f"/A << /S /URI /URI ({uri}) >> >>"
    )
    body = _text_pdf(annot=annots)
    text, links = _normalize(body, "application/pdf")
    assert "Hello PDF" in text
    assert f"[link:sha256:{hashlib.sha256(uri.encode()).hexdigest()}]" in text
    assert not links


def test_pdf_fonts_tounicode_and_form_xobjects() -> None:
    page = {
        "/Contents": "4 0 R",
        "/Resources": (
            "<< /Font << /F1 5 0 R /F2 5 0 R /F3 3 0 R >> "
            "/XObject << /X1 6 0 R /X2 6 0 R /Img 8 0 R >> >>"
        ),
    }
    body = _build_pdf(
        [page, dict(page)],
        {
            4: _stream("BT /F1 12 Tf (AAA) Tj ET /X1 Do"),
            5: (
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                "/ToUnicode 7 0 R >>"
            ),
            6: _stream(
                "BT /F1 12 Tf (form) Tj ET",
                "/Type /XObject /Subtype /Form /BBox [0 0 100 100] "
                "/Resources << /XObject << /Inner 6 0 R >> >>",
            ),
            7: _stream(_CMAP),
            8: _stream("", "/Type /XObject /Subtype /Image"),
        },
    )
    text, _ = _normalize(body, "application/pdf")
    assert "BBB" in text


def test_extract_pdf_payload_roundtrip() -> None:
    result = core.extract_pdf_payload({
        "pdf_base64": base64.b64encode(_text_pdf()).decode()
    })
    assert result["normalized"] == "Hello PDF\n"
    assert result["chars"] == 10


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"%PDF-1.4 truncated", "could not be normalized safely"),
    ],
)
def test_pdf_invalid_body(body: bytes, message: str) -> None:
    with pytest.raises(MonitorError, match=message):
        _normalize(body, "application/pdf")


def test_pdf_encrypted_is_rejected() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_text_pdf())))
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(MonitorError, match="encrypted PDFs"):
        _normalize(buffer.getvalue(), "application/pdf")


def test_normalize_document_rejects_nonpositive_pdf_limits() -> None:
    with pytest.raises(MonitorError, match="numeric limits"):
        core.normalize_document(_doc(b"x"), max_pdf_decompressed_bytes=0)
    with pytest.raises(MonitorError, match="numeric limits"):
        core.normalize_document(_doc(b"x"), max_pdf_extracted_chars=0)


def test_pdf_limits_apply() -> None:
    body = _text_pdf("x" * 200)
    document = _doc(body, "application/pdf")
    with pytest.raises(MonitorError, match="extracted text exceeds"):
        core.normalize_document(document, max_pdf_extracted_chars=50)
    with pytest.raises(MonitorError, match="could not be normalized safely"):
        core.normalize_document(document, max_pdf_decompressed_bytes=20)


def test_pdf_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_DEFAULT_MAX_PDF_PAGES", 1)
    with pytest.raises(MonitorError, match="page count exceeds"):
        _normalize(_build_pdf([{}, {}, {}]), "application/pdf")


def test_pdf_missing_pypdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(MonitorError, match="requires pypdf"):
        core._normalize_pdf_content(
            b"x", max_decompressed_bytes=1, max_extracted_chars=1
        )
    with pytest.raises(MonitorError, match="requires pypdf"):
        with core._pypdf_output_limits(1):
            pytest.fail("unreachable")


def test_pypdf_output_limits_guard_flate_recovery() -> None:
    from pypdf import filters

    original = filters.decompress
    with core._pypdf_output_limits(4) as set_limit:
        set_limit(2)
        assert filters.decompress is not original
        with pytest.raises(MonitorError, match="Flate recovery input"):
            filters.decompress(b"12345")
    assert filters.decompress is original


class _Node(dict[str, Any]):
    """Faithful stand-in for a pypdf dictionary object (optionally a stream)."""

    def __init__(self, data: dict[str, Any] | None = None, stream: Any = None) -> None:
        super().__init__(data or {})
        self._stream = stream

    def get_data(self) -> Any:
        return self._stream


class _Ref:
    """Stand-in for an indirect reference."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def get_object(self) -> Any:
        return self._target


class _Reader:
    def __init__(self, root: Any) -> None:
        self.trailer = {"/Root": _Node({"/Pages": root})}


def test_pypdf_flate_recovery_passthrough() -> None:
    import zlib

    from pypdf import filters

    with core._pypdf_output_limits(100):
        assert filters.decompress(zlib.compress(b"abc")) == b"abc"


def test_pdf_values_and_resolve() -> None:
    assert core._resolve_pdf_object(_Ref(5)) == 5
    assert core._resolve_pdf_object(7) == 7
    assert core._pdf_values(None) == []
    assert core._pdf_values(_Ref(None)) == []
    source = [1, 2]
    values = core._pdf_values(source)
    assert values == source
    assert values is not source
    assert core._pdf_values((1, 2)) == [1, 2]
    assert core._pdf_values("one") == ["one"]


def test_consume_pdf_object_limit() -> None:
    counter = [core._DEFAULT_MAX_PDF_OBJECTS]
    with pytest.raises(MonitorError, match="object traversal"):
        core._consume_pdf_object(counter)


def test_preflight_pdf_pages() -> None:
    leaf = _Node({"/Type": "/Page"})
    root = _Node({
        "/Kids": [leaf, _Ref(leaf), "scalar", _Ref(_Node({"/Kids": [leaf]}))]
    })
    assert core._preflight_pdf_pages(_Reader(root)) == [4]
    declared = _Node({"/Count": core._DEFAULT_MAX_PDF_PAGES + 1})
    with pytest.raises(MonitorError, match="page count exceeds"):
        core._preflight_pdf_pages(_Reader(declared))
    assert core._preflight_pdf_pages(_Reader(_Node({"/Count": "many"}))) == [1]


def test_preflight_pdf_pages_counts_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_DEFAULT_MAX_PDF_PAGES", 2)
    kids = [_Node({"/Type": "/Page", "i": i}) for i in range(3)]
    with pytest.raises(MonitorError, match="page count exceeds"):
        core._preflight_pdf_pages(_Reader(_Node({"/Kids": kids})))


def test_preflight_pdf_pages_scalar_kid() -> None:
    odd = _Reader(_Node({"/Kids": [42]}))
    assert core._preflight_pdf_pages(odd) == [2]


def test_pdf_page_loop_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_preflight_pdf_pages", lambda _reader: [0])
    monkeypatch.setattr(core, "_DEFAULT_MAX_PDF_PAGES", 1)
    with pytest.raises(MonitorError, match="page count exceeds"):
        _normalize(_build_pdf([{}, {}]), "application/pdf")


def test_append_form_xobjects() -> None:
    inner = _Node({"/Subtype": "/Form"})
    outer = _Node({"/Subtype": "/Form", "/Resources": {"/XObject": {"i": inner}}})
    image = _Node({"/Subtype": "/Image"})
    streams: list[Any] = []
    seen: set[int] = set()
    resources = {"/XObject": {"a": outer, "b": _Ref(outer), "c": image, "d": 5}}
    core._append_form_xobjects(resources, streams, seen, [0])
    assert streams == [outer, inner]
    core._append_form_xobjects(None, streams, seen, [0])
    core._append_form_xobjects({"/XObject": "bad"}, streams, seen, [0])
    assert len(streams) == 2


def test_page_content_streams() -> None:
    first = _Node(stream=b"a")
    page = _Node({
        "/Contents": [first, _Ref(first), _Node(stream=b"b")],
        "/Resources": {"/XObject": {"f": _Node({"/Subtype": "/Form"})}},
    })
    assert len(core._page_content_streams(page, [0])) == 3
    assert core._page_content_streams(_Node(), [0]) == []


def _fonts_page(fonts: Any) -> _Node:
    return _Node({"/Resources": {"/Font": fonts}})


def _bound_fonts(
    page: Any,
    *,
    used: int = 0,
    maximum: int = 100,
    font_count: list[int] | None = None,
    seen: set[int] | None = None,
    limits: list[int] | None = None,
) -> int:
    recorded = limits if limits is not None else []
    return core._bound_page_fonts(
        page,
        used=used,
        maximum=maximum,
        set_limit=recorded.append,
        object_count=[0],
        font_count=font_count if font_count is not None else [0],
        seen_cmaps=seen if seen is not None else set(),
    )


def test_bound_page_fonts() -> None:
    cmap = _Node(stream=b"12345")
    shared = _Node({"/ToUnicode": cmap})
    fonts = {
        "a": shared,
        "b": _Ref(shared),
        "c": 3,
        "d": _Node(),
        "e": _Node({"/ToUnicode": 3}),
    }
    limits: list[int] = []
    assert _bound_fonts(_fonts_page(fonts), limits=limits) == 5
    assert limits == [100]
    assert _bound_fonts(_Node()) == 0
    assert _bound_fonts(_Node({"/Resources": {"/Font": "bad"}}), used=4) == 4
    assert _bound_fonts(_Node({"/Resources": 5}), used=9) == 9


@pytest.mark.parametrize(
    ("stream", "maximum", "message"),
    [
        (b"123456", 5, "font mappings exceed"),
        ("text", 100, "font mappings exceed"),
    ],
)
def test_bound_page_fonts_rejects(stream: Any, maximum: int, message: str) -> None:
    page = _fonts_page({"a": _Node({"/ToUnicode": _Node(stream=stream)})})
    with pytest.raises(MonitorError, match=message):
        _bound_fonts(page, maximum=maximum)


def test_bound_page_fonts_count_limit() -> None:
    page = _fonts_page({"a": _Node()})
    with pytest.raises(MonitorError, match="font resources exceed"):
        _bound_fonts(page, font_count=[core._DEFAULT_MAX_PDF_FONTS])


def test_bound_page_content() -> None:
    limits: list[int] = []
    page = _Node({"/Contents": [_Node(stream=b"abc"), 5, _Node(stream=b"")]})
    used = core._bound_page_content(
        page, used=0, maximum=10, set_limit=limits.append, object_count=[0]
    )
    assert used == 3
    assert limits == [10, 7]
    exhausted = core._bound_page_content(
        _Node({"/Contents": [_Node(stream=b"")]}),
        used=10,
        maximum=10,
        set_limit=limits.append,
        object_count=[0],
    )
    assert exhausted == 10
    assert limits[-1] == 1


@pytest.mark.parametrize("stream", [b"123456", "text"])
def test_bound_page_content_rejects(stream: Any) -> None:
    page = _Node({"/Contents": [_Node(stream=stream)]})
    with pytest.raises(MonitorError, match="decompressed streams exceed"):
        core._bound_page_content(
            page, used=0, maximum=5, set_limit=lambda _v: None, object_count=[0]
        )


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (" https://a.example/x ", hashlib.sha256(b"https://a.example/x").hexdigest()),
        ("mailto:a@example.com", ""),
    ],
)
def test_pdf_link_destination(uri: str, expected: str) -> None:
    assert core._pdf_link_destination(uri) == expected


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("http://[bad", "destination is invalid"),
        ("relative/path", "relative link destinations"),
        ("https://u:p@a.example/", "contains credentials"),
    ],
)
def test_pdf_link_destination_rejects(uri: str, message: str) -> None:
    with pytest.raises(MonitorError, match=message):
        core._pdf_link_destination(uri)


def test_pdf_annotation_link() -> None:
    def link(**action: Any) -> _Node:
        return _Node({"/Subtype": "/Link", "/A": _Node(action)})

    digest = hashlib.sha256(b"https://a.example/").hexdigest()
    good = link(**{"/S": "/URI", "/URI": _Ref("https://a.example/")})
    assert core._pdf_annotation_link(good, [0]) == digest
    assert core._pdf_annotation_link(5, [0]) == ""
    assert core._pdf_annotation_link(_Node({"/Subtype": "/Text"}), [0]) == ""
    assert core._pdf_annotation_link(_Node({"/Subtype": "/Link"}), [0]) == ""
    assert core._pdf_annotation_link(link(**{"/S": "/GoTo"}), [0]) == ""
    assert core._pdf_annotation_link(link(**{"/S": "/URI", "/URI": 5}), [0]) == ""


def test_page_link_destinations() -> None:
    link = _Node({
        "/Subtype": "/Link",
        "/A": {"/S": "/URI", "/URI": "https://a.example/"},
    })
    plain = _Node({"/Subtype": "/Text"})
    counter = [0]
    lines = core._page_link_destinations(
        _Node({"/Annots": [link, plain, _Ref(link)]}), [0], counter
    )
    assert len(lines) == 2
    assert counter == [3]
    assert core._page_link_destinations(_Node(), [0], [0]) == []
    with pytest.raises(MonitorError, match="invalid structure"):
        core._page_link_destinations(_Node({"/Annots": "bad"}), [0], [0])
    many = _Node({"/Annots": [plain] * 3})
    with pytest.raises(MonitorError, match="annotations exceed"):
        core._page_link_destinations(many, [0], [core._DEFAULT_MAX_PDF_ANNOTATIONS - 2])


def test_extract_page_text_limits() -> None:
    class Page:
        def __init__(self, text: str | None, chunks: list[str]) -> None:
            self._text = text
            self._chunks = chunks

        def extract_text(self, *, visitor_text: Any) -> str | None:
            for chunk in self._chunks:
                visitor_text(chunk, None, None, None, None)
            return self._text

    assert core._extract_page_text(Page("abc", ["abc"]), extracted=0, maximum=10) == (
        "abc",
        3,
    )
    assert core._extract_page_text(Page(None, []), extracted=0, maximum=10) == ("", 0)
    with pytest.raises(MonitorError, match="extracted text exceeds"):
        core._extract_page_text(Page("x", ["abcdef"]), extracted=0, maximum=5)
    with pytest.raises(MonitorError, match="extracted text exceeds"):
        core._extract_page_text(Page("abcdef", []), extracted=0, maximum=5)


def test_normalize_pdf_wraps_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("boom")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

    monkeypatch.setattr(core, "_normalize_pdf_content", boom)
    with pytest.raises(MonitorError, match="could not be normalized safely"):
        core._normalize_pdf(b"", max_decompressed_bytes=1, max_extracted_chars=1)
