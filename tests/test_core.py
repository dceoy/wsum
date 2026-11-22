"""Tests for the Gemini hybrid deterministic core."""

# ruff: file-ignore[any-type, compare-to-empty-string]
# Any: pypdf objects and callbacks are dynamically typed, so test doubles are too.
# compare-to-empty-string: assertions pin the exact "" result (distinct from None).

# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import hashlib
import io
import json
import runpy
import sys
import zlib
from typing import Any

import core
import pytest
from core import Document, LinkCollection, MonitorError, WsumError
from pypdf import PdfReader, PdfWriter, filters

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
</feed>"""  # ruff: ignore[line-too-long]

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

        @staticmethod
        def feed(text: str) -> None:
            del text

        @staticmethod
        def close() -> object:
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
    """Build a small valid PDF; page dicts hold raw dictionary entries.

    Returns:
        The PDF file bytes.
    """
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
    with (
        pytest.raises(MonitorError, match="requires pypdf"),
        core._pypdf_output_limits(1),
    ):
        pytest.fail("unreachable")


def test_pypdf_output_limits_guard_flate_recovery() -> None:
    original = filters.decompress
    with core._pypdf_output_limits(4) as set_limit:
        set_limit(2)
        assert filters.decompress is not original
        with pytest.raises(MonitorError, match="Flate recovery input"):
            filters.decompress(b"12345")
    assert filters.decompress is original


class _Node(dict[str, Any]):  # ruff: ignore[subclass-builtin]
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
    def one_page(_reader: object) -> list[int]:
        return [0]

    monkeypatch.setattr(core, "_preflight_pdf_pages", one_page)
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
        raise RuntimeError("boom")  # ruff: ignore[raw-string-in-exception]

    monkeypatch.setattr(core, "_normalize_pdf_content", boom)
    with pytest.raises(MonitorError, match="could not be normalized safely"):
        core._normalize_pdf(b"", max_decompressed_bytes=1, max_extracted_chars=1)


# --- diff and comparison ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        ("abc", 0, ""),
        ("abc", -1, ""),
        ("abc", 3, "abc"),
        ("abc", 10, "abc"),
        ("abcdef", 4, "abcd"),
        ("aé", 2, "a"),  # 'é' needs two bytes; cut mid-character
        ("aé", 3, "aé"),
        ("日本語", 7, "日本"),
        ("😀", 3, ""),  # nothing decodable fits
    ],
)
def test_utf8_prefix(value: str, limit: int, expected: str) -> None:
    assert core._utf8_prefix(value, limit) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("a\n", 1),
        ("a\nb", 2),
        ("a\r\nb\r\n", 2),
        ("a\n\nb", 3),
        ("a\x0bb\x0cc" + chr(0x2028) + "d", 4),
        ("\n", 1),
    ],
)
def test_line_count_matches_splitlines(value: str, expected: int) -> None:
    assert core._line_count(value) == expected
    assert core._line_count(value) == len(value.splitlines())


def test_compare_text_baseline() -> None:
    result = core.compare_text("hello", None, max_diff_lines=10)
    assert result == {
        "status": "baseline",
        "sha256": hashlib.sha256(b"hello").hexdigest(),
        "previous_sha256": "",
        "diff": "",
        "diff_truncated": False,
    }


def test_compare_text_unchanged() -> None:
    result = core.compare_text("same\ntext", "same\ntext", max_diff_lines=10)
    digest = hashlib.sha256(b"same\ntext").hexdigest()
    assert result["status"] == "unchanged"
    assert result["sha256"] == result["previous_sha256"] == digest
    assert result["diff"] == ""
    assert result["diff_truncated"] is False


def test_compare_text_changed_renders_unified_diff() -> None:
    result = core.compare_text("a\nB\nc", "a\nb\nc", max_diff_lines=50)
    assert result["status"] == "changed"
    assert result["sha256"] == hashlib.sha256(b"a\nB\nc").hexdigest()
    assert result["previous_sha256"] == hashlib.sha256(b"a\nb\nc").hexdigest()
    assert result["diff_truncated"] is False
    assert (
        result["diff"] == "--- previous\n+++ current\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c"
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_diff_lines": 0}, "numeric limits must be positive"),
        ({"max_diff_lines": 1, "max_diff_bytes": 0}, "numeric limits must be positive"),
    ],
)
def test_compare_text_rejects_nonpositive_limits(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(MonitorError, match=message):
        core.compare_text(
            "a",
            "b",
            max_diff_lines=kwargs["max_diff_lines"],
            max_diff_bytes=kwargs.get("max_diff_bytes", 1),
        )


def test_compare_text_truncates_by_lines() -> None:
    current = "\n".join(f"new{i}" for i in range(20))
    previous = "\n".join(f"old{i}" for i in range(20))
    result = core.compare_text(current, previous, max_diff_lines=5)
    assert result["diff_truncated"] is True
    assert str(result["diff"]).splitlines() == [
        "--- previous",
        "+++ current",
        "@@ -1,20 +1,20 @@",
        "-old0",
        "-old1",
    ]


def test_compare_text_truncates_by_bytes_on_character_boundary() -> None:
    # Header + hunk + "-x" occupy 39 bytes; "\n+日本語" then straddles the limit.
    cut = core.compare_text("日本語", "x", max_diff_lines=50, max_diff_bytes=42)
    assert cut["diff_truncated"] is True
    assert cut["diff"] == "--- previous\n+++ current\n@@ -1 +1 @@\n-x\n+"
    one = core.compare_text("日本語", "x", max_diff_lines=50, max_diff_bytes=44)
    assert str(one["diff"]).endswith("\n+日")
    assert one["diff_truncated"] is True


def test_compare_text_untruncated_when_diff_fits_exactly() -> None:
    full = core.compare_text("b", "a", max_diff_lines=50)
    size = len(str(full["diff"]).encode("utf-8"))
    exact = core.compare_text("b", "a", max_diff_lines=50, max_diff_bytes=size)
    assert exact["diff"] == full["diff"]
    assert exact["diff_truncated"] is False
    short = core.compare_text("b", "a", max_diff_lines=50, max_diff_bytes=size - 1)
    assert short["diff_truncated"] is True


def test_compare_text_complexity_limit_omits_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_DEFAULT_MAX_DIFF_COMPLEXITY", 5)
    result = core.compare_text("a\nb\nc", "d\ne\nf", max_diff_lines=50)
    assert result["status"] == "changed"
    assert result["diff_truncated"] is True
    assert result["diff"] == (
        "--- previous\n+++ current\n@@ diff omitted: complexity limit exceeded @@"
    )


def test_bounded_diff_complexity_marker_respects_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_DEFAULT_MAX_DIFF_COMPLEXITY", 0)
    by_lines, flag = core._bounded_diff("a", "b", max_diff_lines=2, max_diff_bytes=999)
    assert (by_lines, flag) == ("--- previous\n+++ current", True)
    by_bytes, flag = core._bounded_diff("a", "b", max_diff_lines=9, max_diff_bytes=8)
    assert (by_bytes, flag) == ("--- prev", True)


def test_compare_text_navigation_marker_order_is_incidental_when_enabled() -> None:
    a, b = _marker("https://example.com/a"), _marker("https://example.com/b")
    previous = f"Body text\n{a}\n{b}"
    current = f"Body text\n{b}\n{a}"
    plain = core.compare_text(current, previous, max_diff_lines=20)
    assert plain["status"] == "changed"
    nav = core.compare_text(
        current, previous, max_diff_lines=20, compare_navigation_markers=True
    )
    assert nav["status"] == "unchanged"
    assert nav["sha256"] == hashlib.sha256(current.encode()).hexdigest()
    assert nav["previous_sha256"] == hashlib.sha256(previous.encode()).hexdigest()


def test_compare_text_navigation_marker_set_change_is_still_changed() -> None:
    a, b = _marker("https://example.com/a"), _marker("https://example.com/b")
    nav = core.compare_text(
        f"Body\n{a}\n{b}",
        f"Body\n{a}",
        max_diff_lines=20,
        compare_navigation_markers=True,
    )
    assert nav["status"] == "changed"
    assert b in str(nav["diff"])


def test_navigation_comparison_view_marker_only_text_is_manifest_suffix() -> None:
    a = _marker("https://example.com/a")
    *_, is_suffix = core._navigation_comparison_view(f"\n{a}\n\n")
    assert is_suffix is True


# --- target validation and grouping ----------------------------------------


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://[::1", "url is invalid"),
        ("ftp://example.com/", "absolute HTTP"),
        ("/relative/path", "absolute HTTP"),
        ("https:///no-host", "absolute HTTP"),
        ("https://example.com/#frag", "must not contain a fragment"),
        ("https://user:pw@example.com/", "must not contain credentials"),
    ],
)
def test_validate_url_rejects(url: str, message: str) -> None:
    with pytest.raises(WsumError, match=message):
        core._validate_url(url)


@pytest.mark.parametrize("url", ["http://example.com", "HTTPS://Example.com/a?b=1"])
def test_validate_url_accepts_unchanged(url: str) -> None:
    assert core._validate_url(url) == url


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("https://Example.COM/page", "example-com"),
        ("https://a_b.example.org/", "a-b-example-org"),
        ("/no/host", "target"),
        ("http://[::1", "target"),
        ("https://" + "a" * 60 + ".com/", "a" * 48),
    ],
)
def test_target_id(url: str, prefix: str) -> None:
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    assert core._target_id(url) == f"{prefix}-{digest}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", True), ("  TRUE ", True), ("false", False), (" False", False)],
)
def test_parse_enabled(value: str, expected: bool) -> None:
    assert core._parse_enabled(value, 2) is expected


def test_parse_enabled_rejects() -> None:
    with pytest.raises(WsumError, match="row 7: enabled must be true or false"):
        core._parse_enabled("yes", 7)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (3.0, "3"),
        (2.5, "2.5"),
        (7, "7"),
        ("text", "text"),
    ],
)
def test_cell_text(value: object, expected: str) -> None:
    assert core._cell_text(value) == expected


@pytest.mark.parametrize("value", [None, ["x"], {"a": 1}])
def test_cell_text_rejects_non_scalars(value: object) -> None:
    with pytest.raises(WsumError, match="cells must be scalar values"):
        core._cell_text(value)


_HEADER = ["name", "url", "publisher", "category", "keywords", "priority", "enabled"]


def test_normalize_target_row_success() -> None:
    result = core._normalize_target_row(
        _HEADER,
        [" Site ", "https://example.com/", "Pub", "News", "ai", 3.0, "false"],
        4,
    )
    assert result == (
        "https://example.com/",
        {
            "name": "Site",
            "publisher": "Pub",
            "category": "News",
            "keywords": "ai",
            "priority": 3,
            "enabled": False,
            "row": 4,
        },
    )


def test_normalize_target_row_short_row_defaults() -> None:
    result = core._normalize_target_row(_HEADER, ["Site", "https://example.com/"], 2)
    assert result is not None
    _, interest = result
    assert interest["priority"] is None
    assert interest["enabled"] is True
    assert interest["publisher"] == ""


def test_normalize_target_row_blank_row_is_skipped() -> None:
    assert core._normalize_target_row(_HEADER, ["", "  ", ""], 5) is None
    assert core._normalize_target_row(_HEADER, [], 6) is None


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["n", "https://e.com/", "", "", "", "", "", "extra"], "row 3: too many"),
        (["", "https://e.com/"], "row 3: name must be non-empty"),
        (["〃", "https://e.com/"], "row 3: name: replace ditto"),
        (["n", "同上"], "row 3: url: replace ditto"),
        (["n", "ftp://e.com/"], "row 3: url must be an absolute HTTP"),
        (["n", "https://e.com/", "", "", "", "abc"], "row 3: priority must be"),
        (["n", "https://e.com/", "", "", "", "-1"], "row 3: priority must be"),
        (["n", "https://e.com/", "", "", "", "0"], "row 3: priority must be"),
        (["n", "https://e.com/", "", "", "", "", "maybe"], "row 3: enabled must be"),
    ],
)
def test_normalize_target_row_rejects(row: list[object], message: str) -> None:
    with pytest.raises(WsumError, match=message):
        core._normalize_target_row(_HEADER, row, 3)


def _interest(*, enabled: bool, name: str = "n", row: int = 2) -> dict[str, object]:
    return {"name": name, "enabled": enabled, "row": row}


def test_group_target_interests_merges_by_url() -> None:
    url = "https://example.com/"
    other = "https://other.example/"
    first = _interest(enabled=False, name="off", row=2)
    second = _interest(enabled=True, name="on", row=3)
    third = _interest(enabled=False, name="solo", row=4)
    targets = core._group_target_interests([
        (url, first),
        (other, third),
        (url, second),
    ])
    assert targets == [
        {
            "target_id": core._target_id(url),
            "name": "on",
            "url": url,
            "enabled": True,
            "action": "monitor",
            "interests": [first, second],
        },
        {
            "target_id": core._target_id(other),
            "name": "solo",
            "url": other,
            "enabled": False,
            "action": "skip_disabled",
            "interests": [third],
        },
    ]


def test_group_target_interests_duplicate_target_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def same_id(_url: str) -> str:
        return "same"

    monkeypatch.setattr(core, "_target_id", same_id)
    with pytest.raises(WsumError, match="duplicate_target_id"):
        core._group_target_interests([
            ("https://a.example/", _interest(enabled=True)),
            ("https://b.example/", _interest(enabled=True)),
        ])


# --- validate payload ------------------------------------------------------

_CSV = "name,url,enabled\nOne,https://one.example/,true\n,,\nTwo,https://two.example/,false\n"


def test_validate_payload_from_csv_text() -> None:
    result = core.validate_payload({"csv": _CSV})
    assert result["ok"] is True
    assert result["errors"] == []
    targets: Any = result["targets"]
    assert [(t["url"], t["action"], t["name"]) for t in targets] == [
        ("https://one.example/", "monitor", "One"),
        ("https://two.example/", "skip_disabled", "Two"),
    ]
    assert targets[1]["interests"][0]["row"] == 4


def test_validate_payload_from_rows_prefers_rows_over_csv() -> None:
    rows = [["name", "url"], ["Row", "https://row.example/"]]
    result = core.validate_payload({"rows": rows, "csv": _CSV})
    targets: Any = result["targets"]
    assert [t["name"] for t in targets] == ["Row"]


def test_validate_payload_csv_too_large() -> None:
    big = "name,url\n" + "x" * core.MAX_CSV_BYTES
    with pytest.raises(WsumError, match="exceeds the 1 MiB limit"):
        core.validate_payload({"csv": big})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "must provide rows"),
        ({"csv": 5}, "must provide rows"),
        ({"rows": "nope"}, "must provide rows"),
        ({"rows": [["name", "url"], "bad"]}, "must provide rows"),
        ({"rows": []}, "no header row"),
        ({"rows": [["name", "name", "url"]]}, "unique header"),
        ({"rows": [["name"]]}, "requires name and url"),
        ({"rows": [["name", "url", "bogus"]]}, "unsupported columns"),
        ({"rows": [["name", "url"], ["", ""]]}, "contains no targets"),
        ({"rows": [["name", "url"]]}, "contains no targets"),
        ({"rows": [["name", "url"], ["n", "ftp://x/"]]}, "row 2: url must"),
    ],
)
def test_validate_payload_rejects(payload: dict[str, object], message: str) -> None:
    with pytest.raises(WsumError, match=message):
        core.validate_payload(payload)


# --- payload handlers ------------------------------------------------------


def _b64(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return base64.b64encode(raw).decode()


def test_links_output_counts_overflow_and_omitted() -> None:
    links = LinkCollection()
    links["b"] = "https://example.com/b"
    links["a"] = "https://example.com/a"
    links.overflow_count = 2
    links.omitted_hashes.add("deadbeef")
    assert core._links_output(links) == {
        "links": ["https://example.com/a", "https://example.com/b"],
        "links_omitted": 3,
    }


def test_normalize_payload_base64_html_collects_links() -> None:
    html = '<html><body><p>Hello   world</p><a href="/next">Next</a></body></html>'
    result = core.normalize_payload({
        "body_base64": _b64(html),
        "content_type": "text/html; charset=utf-8",
        "source_url": "https://example.com/base/",
    })
    normalized = str(result["normalized"])
    assert "Hello world" in normalized
    assert result["sha256"] == hashlib.sha256(normalized.encode()).hexdigest()
    assert result["chars"] == len(normalized)
    assert result["links"] == ["https://example.com/next"]
    assert result["links_omitted"] == 0


def test_normalize_payload_explicit_charset_overrides_header() -> None:
    body = "café".encode("latin-1")
    result = core.normalize_payload({
        "body_base64": _b64(body),
        "content_type": "text/plain; charset=utf-8",
        "charset": "latin-1",
    })
    assert result["normalized"] == "café\n"


def test_normalize_payload_rejects_bad_base64() -> None:
    with pytest.raises(WsumError, match="body_base64 is not valid Base64"):
        core.normalize_payload({"body_base64": _B64_BAD})


def test_normalize_payload_rejects_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_DEFAULT_MAX_BYTES", 4)
    with pytest.raises(MonitorError, match="document exceeds the size limit"):
        core.normalize_payload({"body_base64": _b64(b"12345")})


def test_normalize_payload_text_html_and_links() -> None:
    result = core.normalize_payload({
        "text": '<p>Hi  there</p><a href="page">x</a>',
        "content_type": "text/html",
        "source_url": "https://example.com/dir/",
    })
    assert "Hi there" in str(result["normalized"])
    assert result["links"] == ["https://example.com/dir/page"]


def test_normalize_payload_text_defaults_to_plain_whitespace_normalization() -> None:
    result = core.normalize_payload({"text": "a   b\n\n\n c  "})
    assert result["normalized"] == core._normalize_whitespace("a   b\n\n\n c  ")
    assert result["links"] == []


def test_normalize_payload_text_feed_preserves_structure() -> None:
    feed = (
        '<rss version="2.0"><channel><item><title>T</title>'
        "<link>https://example.com/post</link><guid>g1</guid></item></channel></rss>"
    )
    result = core.normalize_payload({
        "text": feed,
        "content_type": "application/rss+xml",
        "source_url": "https://example.com/feed",
    })
    assert "T" in str(result["normalized"])
    assert "https://example.com/post" in result["links"]  # type: ignore[operator]


def test_normalize_payload_text_pdf_requires_body_base64() -> None:
    with pytest.raises(MonitorError, match="PDF input requires body_base64"):
        core.normalize_payload({"text": "x", "content_type": "application/pdf"})


def test_normalize_payload_text_too_long() -> None:
    with pytest.raises(WsumError, match="text exceeds the 2000000 character limit"):
        core.normalize_payload({"text": "x" * (core.MAX_PAYLOAD_CHARS + 1)})


def test_normalize_payload_requires_a_body() -> None:
    with pytest.raises(WsumError, match="requires body_base64 or text"):
        core.normalize_payload({"text": 5})


def test_normalize_payload_navigation_links_append_markers() -> None:
    url = "https://example.com/nav"
    result = core.normalize_payload({"text": "Body", "navigation_links": [url]})
    digest = hashlib.sha256(url.encode()).hexdigest()
    assert str(result["normalized"]).endswith(f"[a:href:sha256:{digest}]\n")
    assert result["links"] == [url]


def test_normalize_payload_navigation_links_validated() -> None:
    with pytest.raises(MonitorError, match="navigation_links must be a JSON array"):
        core.normalize_payload({"text": "Body", "navigation_links": "x"})


def test_payload_int() -> None:
    assert core._payload_int({"n": 5}, "n", 1) == 5
    assert core._payload_int({}, "n", 9) == 9


@pytest.mark.parametrize("value", [True, "5", 1.5, None])
def test_payload_int_rejects(value: object) -> None:
    with pytest.raises(WsumError, match="n must be an integer"):
        core._payload_int({"n": value}, "n", 1)


def test_compare_payload_baseline_and_changed() -> None:
    baseline = core.compare_payload({"current": "x"})
    assert baseline["status"] == "baseline"
    changed = core.compare_payload({
        "current": "a\nB",
        "previous": "a\nb",
        "max_diff_lines": 3,
        "max_diff_bytes": 10_000,
    })
    assert changed["status"] == "changed"
    assert changed["diff_truncated"] is True
    assert str(changed["diff"]).splitlines() == [
        "--- previous",
        "+++ current",
        "@@ -1,2 +1,2 @@",
    ]


def test_compare_payload_navigation_flag() -> None:
    a, b = _marker("https://example.com/a"), _marker("https://example.com/b")
    payload: dict[str, object] = {
        "current": f"Body\n{b}\n{a}",
        "previous": f"Body\n{a}\n{b}",
    }
    assert core.compare_payload(payload)["status"] == "changed"
    payload["compare_navigation_markers"] = True
    assert core.compare_payload(payload)["status"] == "unchanged"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "compare requires current"),
        ({"current": 1}, "compare requires current"),
        ({"current": "x" * (core.MAX_PAYLOAD_CHARS + 1)}, "current exceeds"),
        ({"current": "x", "previous": 1}, "previous must be normalized text or null"),
        (
            {"current": "x", "previous": "x" * (core.MAX_PAYLOAD_CHARS + 1)},
            "previous exceeds",
        ),
        ({"current": "x", "max_diff_lines": "5"}, "max_diff_lines must be an integer"),
        ({"current": "x", "max_diff_bytes": True}, "max_diff_bytes must be an integer"),
    ],
)
def test_compare_payload_rejects(payload: dict[str, object], message: str) -> None:
    with pytest.raises(WsumError, match=message):
        core.compare_payload(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "requires pdf_base64"),
        ({"pdf_base64": 5}, "requires pdf_base64"),
        ({"pdf_base64": _B64_BAD}, "pdf_base64 is not valid Base64"),
    ],
)
def test_extract_pdf_payload_rejects(payload: dict[str, object], message: str) -> None:
    with pytest.raises(WsumError, match=message):
        core.extract_pdf_payload(payload)


# --- CLI -------------------------------------------------------------------


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    stdin: str,
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = core.main([command])
    return code, json.loads(capsys.readouterr().out)


def test_main_validate_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_main(monkeypatch, capsys, "validate", json.dumps({"csv": _CSV}))
    assert code == 0
    assert out["ok"] is True
    assert len(out["targets"]) == 2


def test_main_normalize_and_compare_roundtrip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, normalized = _run_main(
        monkeypatch, capsys, "normalize", json.dumps({"text": "Hello\nworld"})
    )
    assert code == 0
    assert normalized["normalized"] == "Hello\nworld\n"
    code, compared = _run_main(
        monkeypatch,
        capsys,
        "compare",
        json.dumps({"current": normalized["normalized"], "previous": "Hello\n"}),
    )
    assert code == 0
    assert compared["status"] == "changed"
    assert "+world" in compared["diff"]


def test_main_extract_pdf_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_main(
        monkeypatch,
        capsys,
        "extract-pdf-text",
        json.dumps({"pdf_base64": _b64(_text_pdf("CLI text"))}),
    )
    assert code == 0
    assert out["normalized"] == "CLI text\n"


@pytest.mark.parametrize(
    ("command", "stdin", "message"),
    [
        ("validate", json.dumps({}), "must provide rows"),
        (
            "normalize",
            json.dumps({"text": "x", "content_type": "application/pdf"}),
            "PDF input requires",
        ),
        ("compare", json.dumps({"current": 1}), "compare requires current"),
        ("validate", "not json", "Expecting value"),
        ("validate", json.dumps([1, 2]), "stdin must be a JSON object"),
        (
            "validate",
            json.dumps({"csv": 'name,url\n"unterminated'}),
            "unexpected end of data",
        ),
    ],
)
def test_main_reports_expected_errors_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    stdin: str,
    message: str,
) -> None:
    code, out = _run_main(monkeypatch, capsys, command, stdin)
    assert code == 2
    assert list(out) == ["error"]
    assert message in out["error"]


def test_main_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        core.main(["bogus"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        core.main(["validate", "--version"])
    assert excinfo.value.code == 0
    assert core.VERSION in capsys.readouterr().out


def test_module_entrypoint_runs_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["core.py", "compare"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"current": "x"})))
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(core.__file__, run_name="__main__")
    assert excinfo.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "baseline"


# --- remaining branches ----------------------------------------------------


def test_http_status_error_carries_status_and_source() -> None:
    error = core.HTTPStatusError(503, source_url="https://example.com/x")
    assert isinstance(error, MonitorError)
    assert error.status == 503
    assert error.source_url == "https://example.com/x"
    assert str(error) == "fetch failed: HTTP 503"
    assert core.HTTPStatusError(404).source_url == ""


def _nest_percent(value: str, levels: int) -> str:
    for _ in range(levels):
        value = value.replace("%", "%25")
    return value


def test_query_credential_check_fails_closed_on_deep_percent_nesting() -> None:
    harmless = "q=%41"
    assert core._query_has_credentials(_nest_percent(harmless, 4), depth=0) is False
    assert core._query_has_credentials(_nest_percent(harmless, 5), depth=0) is True
    assert core.url_has_credentials(
        "https://example.com/?" + _nest_percent(harmless, 5)
    )


def test_sniff_content_type_undecodable_prefix_is_plain_text() -> None:
    body = b"<html><body>\xff\xfe</body></html>"
    assert core._sniff_content_type(body, charset="utf-8") == "text/plain"
    assert core._sniff_content_type(body, charset="latin-1") == "text/html"


def test_normalize_document_html_without_link_collection() -> None:
    text = core.normalize_document(_doc("<p>Hi <a href='/x'>link</a></p>"))
    assert text.startswith("Hi\n[a:href:sha256:")
    assert text.endswith("]\nlink\n")


def test_compare_text_navigation_flag_without_comparable_views_keeps_plain_diff() -> (
    None
):
    # No markers in either text: the navigation pair is not comparable.
    result = core.compare_text(
        "one\ntwo", "one\nthree", max_diff_lines=20, compare_navigation_markers=True
    )
    assert result["status"] == "changed"
    assert "-three" in str(result["diff"])
    assert "+two" in str(result["diff"])


def test_feed_entry_keeps_first_identity_for_repeated_link() -> None:
    target = core._FeedTextTarget(
        10_000, preserve_structure=True, base_url="https://example.com/"
    )
    target.start("entry", {})
    target.start("link", {"href": "https://example.com/first"})
    target.end("link")
    target.start("link", {"href": "https://example.com/second"})
    target.end("link")
    target.end("entry")
    assert len(target._entries) == 1
    assert (
        target._entries[0][0]
        == hashlib.sha256(b"https://example.com/first").hexdigest()
    )


def test_feed_blank_text_link_is_ignored() -> None:
    links = LinkCollection()
    feed = "<rss><channel><item><title>T</title><link>   </link></item></channel></rss>"
    text = core._normalize_feed(
        feed,
        max_chars=10_000,
        preserve_structure=True,
        base_url="https://example.com/",
        links=links,
    )
    assert "[link:href:sha256:" not in text
    assert not links


def test_feed_embedded_text_over_limit_is_rejected() -> None:
    feed = (
        "<rss><channel><item><description>"
        + "x" * 200
        + "</description></item></channel></rss>"
    )
    with pytest.raises(MonitorError, match="XML extracted text exceeds the size limit"):
        core._normalize_feed(
            feed,
            max_chars=80,
            preserve_structure=True,
            base_url="https://example.com/",
        )


def test_feed_target_end_without_start_leaves_empty_state_untouched() -> None:
    target = core._FeedTextTarget(
        100, preserve_structure=False, base_url="https://example.com/"
    )
    target.end("stray")
    assert target.close() == ""
    assert target._tag_stack == []
    assert target._base_stack == []
    assert target._base_stack_bytes == 0
