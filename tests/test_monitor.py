"""Tests for the compact monitor helper."""

from __future__ import annotations

import os
from argparse import Namespace
from io import BytesIO
from time import sleep
from typing import TYPE_CHECKING, Any, ClassVar

import monitor
import pytest
from monitor import (
    Document,
    MonitorError,
    compare_text,
    fetch_document,
    normalize_document,
    run,
    validate_public_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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


@pytest.mark.parametrize(
    ("previous", "current", "max_bytes"),
    [
        ("old\n" * 100, "new\n" * 100, 64),
        ("あ" * 100, "い" * 100, 64),
        ("old-value", "new-value", 7),
    ],
    ids=["ascii-lines", "multibyte-line", "long-line"],
)
def test_compare_text_bounds_utf8_diff_bytes(
    previous: str, current: str, max_bytes: int
) -> None:
    result = compare_text(
        current,
        previous,
        max_diff_lines=200,
        max_diff_bytes=max_bytes,
    )

    assert len(str(result["diff"]).encode("utf-8")) <= max_bytes
    assert result["diff_truncated"] is True


def test_compare_text_exact_byte_bound_is_not_truncated() -> None:
    full = compare_text("new\n", "old\n", max_diff_lines=20, max_diff_bytes=1024)
    exact = compare_text(
        "new\n",
        "old\n",
        max_diff_lines=20,
        max_diff_bytes=len(str(full["diff"]).encode("utf-8")),
    )

    assert exact["diff"] == full["diff"]
    assert exact["diff_truncated"] is False


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".pdf", "application/pdf"),
        (".html", "text/html"),
        (".htm", "text/html"),
        (".rss", "application/rss+xml"),
        (".atom", "application/atom+xml"),
        (".xml", "application/xml"),
        (".txt", "text/plain"),
    ],
)
def test_guess_content_type_by_suffix(
    tmp_path: Path, suffix: str, expected: str
) -> None:
    assert (
        monitor._guess_content_type(  # pyright: ignore[reportPrivateUsage]
            tmp_path / f"document{suffix}"
        )
        == expected
    )


def test_request_target_and_host_header_formatting() -> None:
    target = monitor._ResolvedTarget(  # pyright: ignore[reportPrivateUsage]
        "https://[2001:db8::1]:8443/path?x=1", "https", "2001:db8::1", 8443, ()
    )

    assert monitor._request_target(target.url) == "/path?x=1"  # pyright: ignore[reportPrivateUsage]
    assert monitor._host_header(target) == "[2001:db8::1]:8443"  # pyright: ignore[reportPrivateUsage]


def test_read_response_rejects_invalid_readers_and_chunks() -> None:
    class NoReader:
        pass

    class TextReader:
        @staticmethod
        def read(_size: int) -> str:
            return "not bytes"

    with pytest.raises(MonitorError, match="not readable"):
        monitor._read_response_limited(NoReader(), 10)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="did not return bytes"):
        monitor._read_response_limited(TextReader(), 10)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="max-bytes"):
        monitor._read_response_limited(_FakeResponse(200, b"012345"), 3)  # pyright: ignore[reportPrivateUsage]


def test_parse_content_type_extracts_charset() -> None:
    assert monitor._parse_content_type(  # pyright: ignore[reportPrivateUsage]
        "text/html; charset=utf-8"
    ) == ("text/html", "utf-8")


def test_resolver_pool_propagates_errors_and_deadlines() -> None:
    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]
    assert pool.resolve(lambda: 42, (), {}, 1.0) == 42

    def fail() -> None:
        message = "resolver failed"
        raise ValueError(message)

    with pytest.raises(ValueError, match="resolver failed"):
        pool.resolve(fail, (), {}, 1.0)
    with pytest.raises(TimeoutError, match="deadline"):
        pool.resolve(lambda: 42, (), {}, 0)


@pytest.mark.parametrize("value", ["x" * 65, "not-a-real-encoding"])
def test_encoding_name_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(MonitorError, match="unsupported"):
        monitor._encoding_name(value)  # pyright: ignore[reportPrivateUsage]


def test_normalization_rejects_unsupported_and_mismatched_types() -> None:
    with pytest.raises(MonitorError, match="unsupported"):
        normalize_document(Document(b"hello", "https://example.com/", "image/png"))
    with pytest.raises(MonitorError, match="does not match"):
        normalize_document(
            Document(
                b"<html><body>hello</body></html>",
                "https://example.com/",
                "application/xml",
            )
        )


def test_destination_validation_fails_closed_for_malformed_url() -> None:
    assert monitor._destination_has_credentials(  # pyright: ignore[reportPrivateUsage]
        "https://[::1"
    )


@pytest.mark.parametrize("result", [[], "gaierror"], ids=["empty", "lookup-error"])
def test_address_resolution_rejects_empty_or_failed_lookup(
    monkeypatch: pytest.MonkeyPatch, result: list[tuple[object, ...]] | str
) -> None:
    def getaddrinfo(_host: str, _port: int) -> list[tuple[object, ...]]:
        if result == "gaierror":
            message = "lookup failed"
            raise monitor.socket.gaierror(message)
        assert isinstance(result, list)
        return result

    monkeypatch.setattr(monitor.socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(MonitorError, match=r"hostname resolution failed|no addresses"):
        monitor._resolve_addresses("example.com", 80, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.0.8"])
def test_non_public_literal_ip_is_rejected(host: str) -> None:
    with pytest.raises(MonitorError, match="public IP"):
        validate_public_url(f"http://{host}/")


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/?token=secret",
        "http://93.184.216.34/?api%20key=secret",
        "http://93.184.216.34/?key=secret",
        "http://93.184.216.34/?api_token=secret",
        "http://93.184.216.34/?api-token=secret",
        "http://93.184.216.34/?safe=1;token=secret",
        "http://93.184.216.34/?next=https%253A%252F%252Fexample.com%252F%253Ftoken%253Dsecret",
        "http://93.184.216.34/?next=https%3A%2F%2F%5B%3A%3Abad%5D%2F%3Ftoken%3Dsecret",
        "http://93.184.216.34/#access_token=secret",
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://[64:ff9b::1]/",
        "http://[2002::1]/",
        "http://[fec0::1]/",
        "http://[4000::1]/",
        "http://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
    ],
    ids=[
        "token-query",
        "encoded-query-name",
        "generic-key-query",
        "underscore-api-token-query",
        "hyphen-api-token-query",
        "semicolon-query",
        "nested-token-query",
        "malformed-nested-url",
        "credential-fragment",
        "ipv4-multicast",
        "ipv6-multicast",
        "nat64-transition",
        "6to4-transition",
        "site-local",
        "reserved",
        "webhook",
    ],
)
def test_credential_bearing_public_urls_are_rejected(url: str) -> None:
    with pytest.raises(MonitorError, match=r"credential|fragment|public"):
        validate_public_url(url)


def test_public_url_rejects_explicit_zero_port() -> None:
    with pytest.raises(MonitorError, match="port must not be zero"):
        validate_public_url("http://93.184.216.34:0/")


@pytest.mark.parametrize(
    ("body", "content_type", "charset", "expected"),
    [
        (b"\xef\xbb\xbfHello", "text/plain", None, "Hello\n"),
        ("Hello".encode("utf-16"), "text/plain", None, "Hello\n"),
        (
            b'<meta charset="windows-1252"><p>caf\xe9</p>',
            "text/html",
            None,
            "café\n",
        ),
        (
            "Hello".encode("utf-16-le"),
            "text/plain",
            "utf-16-le",
            "Hello\n",
        ),
        (
            "<html><body>Hello</body></html>".encode("utf-16-le"),
            "text/html",
            "utf-16-le",
            "Hello\n",
        ),
        (
            '<?xml version="1.0"?><root><value>Hello</value></root>'.encode(
                "utf-32-le"
            ),
            "application/xml",
            "utf-32-le",
            "Hello\n",
        ),
        (
            "<html><body>価格</body></html>".encode("cp932"),
            "text/html",
            "cp932",
            "価格\n",
        ),
    ],
    ids=[
        "utf8-bom",
        "utf16-bom",
        "html-meta",
        "http-charset",
        "utf16-html-no-bom",
        "utf32-xml-no-bom",
        "cp932",
    ],
)
def test_normalize_document_detects_supported_encodings(
    body: bytes,
    content_type: str,
    charset: str | None,
    expected: str,
) -> None:
    document = Document(body, "https://example.com/", content_type, charset)

    assert normalize_document(document) == expected


def test_normalize_xml_declaration_detects_encoding() -> None:
    body = (
        b'<?xml version="1.0" encoding="shift_jis"?><root>'
        + "こんにちは".encode("shift_jis")
        + b"</root>"
    )

    assert (
        normalize_document(Document(body, "https://example.com/", "application/xml"))
        == "こんにちは\n"
    )


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ("<html><body>hello</body></html>".encode("utf-16"), "text/html", "hello\n"),
        (
            "<rss><channel><title>hello</title></channel></rss>".encode("utf-32"),
            "application/rss+xml",
            "hello",
        ),
    ],
    ids=["utf16-html", "utf32-feed"],
)
def test_normalize_structured_documents_detects_bom_encoding(
    body: bytes, content_type: str, expected: str
) -> None:
    normalized = normalize_document(
        Document(body, "https://example.com/", content_type)
    )

    assert expected in normalized


def test_normalize_html_preserves_inline_text_continuity() -> None:
    plain = normalize_document(
        Document(b"<p>Hello world</p>", "https://example.com/", "text/html")
    )
    marked = normalize_document(
        Document(
            b"<p>Hello <strong>world</strong></p>",
            "https://example.com/",
            "text/html",
        )
    )

    assert marked == plain


def test_normalize_html_preserves_line_break_elements() -> None:
    normalized = normalize_document(
        Document(b"<p>Hello<br>world</p>", "https://example.com/", "text/html")
    )

    assert normalized == "Hello\nworld\n"


def test_normalize_html_preserves_safe_fragment_destinations() -> None:
    normalized = normalize_document(
        Document(
            b'<a href="#install">Install</a>',
            "https://example.com/",
            "text/html",
        )
    )

    assert "Install" in normalized
    assert (
        monitor.hashlib.sha256(b"https://example.com/#install").hexdigest()
        in normalized
    )


@pytest.mark.parametrize(
    ("body", "charset"),
    [(b"\xff", None), (b"\xfe", None), (b"hello", "x-unknown")],
    ids=["invalid-utf8-a", "invalid-utf8-b", "unsupported-declaration"],
)
def test_normalize_document_rejects_lossy_or_unsupported_encoding(
    body: bytes, charset: str | None
) -> None:
    document = Document(body, "https://example.com/", "text/plain", charset)

    with pytest.raises(MonitorError, match=r"decoded|encoding"):
        normalize_document(document)


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        (
            "application/rss+xml",
            (
                b"<rss><channel><description><![CDATA[Price 10]]></description>"
                b"<item><enclosure url='https://example.com/file'/></item>"
                b"</channel></rss>"
            ),
            ("Price 10", "https://example.com/file"),
        ),
        (
            "application/atom+xml",
            (
                b"<feed><entry><link href='https://example.com/new'/><title>Update"
                b"</title></entry></feed>"
            ),
            ("Update", "https://example.com/new"),
        ),
    ],
    ids=["rss-cdata-and-enclosure", "atom-link"],
)
def test_normalize_feed_preserves_text_and_link_attributes(
    content_type: str, body: bytes, expected: tuple[str, str]
) -> None:
    normalized = normalize_document(
        Document(body, "https://example.com/", content_type)
    )

    text, destination = expected
    assert text in normalized
    assert destination not in normalized
    assert monitor.hashlib.sha256(destination.encode()).hexdigest() in normalized


@pytest.mark.parametrize(
    ("content_type", "body", "identity"),
    [
        (
            "application/rss+xml",
            (
                b"<rss><channel><item><guid>https://example.com/item</guid>"
                b"<title>Update</title></item></channel></rss>"
            ),
            "https://example.com/item",
        ),
        (
            "application/atom+xml",
            (
                b"<feed><entry><id>https://example.com/item</id>"
                b"<title>Update</title></entry></feed>"
            ),
            "https://example.com/item",
        ),
    ],
    ids=["rss-uri-guid", "atom-uri-id"],
)
def test_normalize_feed_redacts_safe_uri_identity_values(
    content_type: str, body: bytes, identity: str
) -> None:

    normalized = normalize_document(
        Document(body, "https://example.com/", content_type)
    )

    assert identity not in normalized
    assert monitor.hashlib.sha256(identity.encode()).hexdigest() in normalized


def test_normalize_feed_rejects_credential_bearing_uri_identity() -> None:
    body = (
        b"<rss><channel><item><guid>https://example.com/item?token=secret"
        b"</guid><title>Update</title></item></channel></rss>"
    )

    with pytest.raises(MonitorError, match="feed identity contains credentials"):
        normalize_document(
            Document(body, "https://example.com/", "application/rss+xml")
        )


@pytest.mark.parametrize(
    "body",
    [
        b"<rss><channel><item></channel></rss>",
        b"<!DOCTYPE rss [<!ENTITY x 'unsafe'>]><rss>&x;</rss>",
    ],
    ids=["malformed", "doctype-entity"],
)
def test_normalize_feed_rejects_unsafe_xml(body: bytes) -> None:
    with pytest.raises(MonitorError, match=r"XML|DOCTYPE"):
        normalize_document(Document(body, "https://example.com/", "application/xml"))


class _FakeSocket:
    def __init__(self, address: str) -> None:
        self.address = address
        self.timeouts: list[float] = []
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return self.address, 80

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"hello",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._headers = headers or {"Content-Type": "text/plain"}
        self._closed = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)

    def read1(self, size: int) -> bytes:
        if not self._body:
            self._closed = True
            return b""
        chunk = self._body[:size]
        self._body = self._body[len(chunk) :]
        return chunk

    def isclosed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


class _FakeConnection:
    instances: ClassVar[list[_FakeConnection]] = []
    responses: ClassVar[list[_FakeResponse]] = []

    def __init__(self, host: str, *, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: _FakeSocket | None = None
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.instances.append(self)

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> _FakeResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()


def _install_fake_http(
    monkeypatch: pytest.MonkeyPatch,
    resolver: Callable[[str, int], list[tuple[object, ...]]],
    responses: list[_FakeResponse],
    connected: list[tuple[str, int]],
) -> None:
    _FakeConnection.instances = []
    _FakeConnection.responses = responses
    monkeypatch.setattr(monitor.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(monitor.http.client, "HTTPConnection", _FakeConnection)

    def connect(address: str, port: int, *, deadline: float) -> _FakeSocket:
        del deadline
        connected.append((address, port))
        return _FakeSocket(address)

    monkeypatch.setattr(monitor, "_connect_pinned_socket", connect)


def test_fetch_uses_one_validated_address_and_preserves_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls: list[tuple[str, int]] = []
    connected: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        resolver_calls.append((host, port))
        return [
            (
                monitor.socket.AF_INET,
                monitor.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", port),
            )
        ]

    _install_fake_http(monkeypatch, resolver, [_FakeResponse(200)], connected)

    document = fetch_document(
        "http://example.com/path?query=1", timeout=5.0, max_bytes=1024
    )

    assert document.body == b"hello"
    assert resolver_calls == [("example.com", 80)]
    assert connected == [("93.184.216.34", 80)]
    assert _FakeConnection.instances[0].requests[0][2]["Host"] == "example.com"


def test_fetch_revalidates_each_redirect_without_re_resolving_a_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls: list[str] = []
    connected: list[tuple[str, int]] = []
    addresses = {"example.com": "93.184.216.34", "other.example": "93.184.216.35"}

    def resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        resolver_calls.append(host)
        return [
            (
                monitor.socket.AF_INET,
                monitor.socket.SOCK_STREAM,
                6,
                "",
                (addresses[host], port),
            )
        ]

    _install_fake_http(
        monkeypatch,
        resolver,
        [
            _FakeResponse(302, headers={"Location": "http://other.example/new"}),
            _FakeResponse(200, body=b"updated"),
        ],
        connected,
    )

    document = fetch_document("http://example.com/old", timeout=5.0, max_bytes=1024)

    assert document.source_url == "http://other.example/new"
    assert document.body == b"updated"
    assert resolver_calls == ["example.com", "other.example"]
    assert connected == [("93.184.216.34", 80), ("93.184.216.35", 80)]


@pytest.mark.parametrize(
    ("body", "declared_length"),
    [(b"short", "6"), (b"hello", "-1")],
    ids=["truncated-body", "negative-length"],
)
def test_fetch_rejects_invalid_response_body_length(
    monkeypatch: pytest.MonkeyPatch, body: bytes, declared_length: str
) -> None:
    connected: list[tuple[str, int]] = []
    _install_fake_http(
        monkeypatch,
        lambda _host, _port: [],
        [
            _FakeResponse(
                200,
                body=body,
                headers={
                    "Content-Type": "text/plain",
                    "Content-Length": declared_length,
                },
            )
        ],
        connected,
    )

    with pytest.raises(MonitorError, match=r"Content-Length"):
        fetch_document("http://93.184.216.34/", timeout=5.0, max_bytes=1024)


def test_fetch_accepts_exact_response_body_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[str, int]] = []
    _install_fake_http(
        monkeypatch,
        lambda _host, _port: [],
        [
            _FakeResponse(
                200,
                body=b"hello",
                headers={"Content-Type": "text/plain", "Content-Length": "5"},
            )
        ],
        connected,
    )

    document = fetch_document("http://93.184.216.34/", timeout=5.0, max_bytes=1024)

    assert document.body == b"hello"


@pytest.mark.parametrize("redirect", [False, True], ids=["initial", "redirect"])
def test_fetch_rejects_mixed_public_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch, redirect: bool
) -> None:
    connected: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        if redirect and host == "example.com":
            addresses = ["93.184.216.34"]
        else:
            addresses = ["93.184.216.34", "10.0.0.1"]
        return [
            (
                monitor.socket.AF_INET,
                monitor.socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
            for address in addresses
        ]

    responses = (
        [
            _FakeResponse(302, headers={"Location": "http://other.example/new"}),
            _FakeResponse(200),
        ]
        if redirect
        else [_FakeResponse(200)]
    )
    _install_fake_http(monkeypatch, resolver, responses, connected)

    with pytest.raises(MonitorError, match="public IP"):
        fetch_document("http://example.com/old", timeout=5.0, max_bytes=1024)

    assert connected == ([("93.184.216.34", 80)] if redirect else [])


def test_fetch_deadline_covers_trickled_body(monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[tuple[str, int]] = []

    class SlowResponse(_FakeResponse):
        def read1(self, size: int) -> bytes:
            sleep(0.08)
            return super().read1(size)

    _install_fake_http(
        monkeypatch,
        lambda _host, _port: [],
        [SlowResponse(200)],
        connected,
    )

    with pytest.raises(MonitorError, match="TimeoutError"):
        fetch_document("http://93.184.216.34/", timeout=0.03, max_bytes=1024)


def _make_pdf(content: bytes, *, compressed: bool) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import (  # ruff: ignore[import-outside-top-level]
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        })
    })
    stream = DecodedStreamObject()
    stream.set_data(content)
    if compressed:
        stream = stream.flate_encode()
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _make_pdf_with_link(content: bytes, *, uri: str) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.annotations import Link  # ruff: ignore[import-outside-top-level]
    from pypdf.generic import (  # ruff: ignore[import-outside-top-level]
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        RectangleObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)
        })
    })
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_annotation(
        page_number=0,
        annotation=Link(rect=RectangleObject((0, 0, 50, 50)), url=uri),
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_normalize_pdf_bounds_expansion_and_restores_pypdf_limits() -> None:
    pytest.importorskip("pypdf")
    from pypdf import filters  # ruff: ignore[import-outside-top-level]

    original = filters.ZLIB_MAX_OUTPUT_LENGTH
    recovery_original = filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH
    pdf = _make_pdf(b"q\n" * 2_000, compressed=True)

    with pytest.raises(MonitorError, match="PDF"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf"),
            max_pdf_decompressed_bytes=128,
            max_pdf_extracted_chars=1_000,
        )

    assert original == filters.ZLIB_MAX_OUTPUT_LENGTH
    assert recovery_original == filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH


def test_normalize_valid_pdf_extracts_text() -> None:
    pytest.importorskip("pypdf")
    pdf = _make_pdf(b"BT /F1 12 Tf 72 200 Td (hello) Tj ET", compressed=True)

    normalized = normalize_document(
        Document(pdf, "https://example.com/file.pdf", "application/pdf"),
        max_pdf_decompressed_bytes=4_096,
        max_pdf_extracted_chars=1_000,
    )

    assert "hello" in normalized


@pytest.mark.parametrize(
    ("before_uri", "after_uri"),
    [
        ("https://example.com/v1", "https://example.com/v2"),
        ("https://example.com/v1?page=1", "https://example.com/v1?page=2"),
    ],
    ids=["path", "query"],
)
def test_normalize_pdf_detects_link_destination_changes(
    before_uri: str, after_uri: str
) -> None:
    content = b"BT /F1 12 Tf 72 200 Td (hello) Tj ET"
    before = normalize_document(
        Document(
            _make_pdf_with_link(content, uri=before_uri),
            "https://example.com/file.pdf",
            "application/pdf",
        ),
        max_pdf_decompressed_bytes=4_096,
        max_pdf_extracted_chars=1_000,
    )
    after = normalize_document(
        Document(
            _make_pdf_with_link(content, uri=after_uri),
            "https://example.com/file.pdf",
            "application/pdf",
        ),
        max_pdf_decompressed_bytes=4_096,
        max_pdf_extracted_chars=1_000,
    )

    assert before != after
    assert before_uri not in before
    assert after_uri not in after


@pytest.mark.parametrize(
    "uri",
    [
        "https://user:pass@example.com/item",
        "https://example.com/item?token=secret",
        "https://hooks.slack.com/services/T00000000/B00000000/" + "X" * 24,
    ],
    ids=["userinfo", "query-credential", "webhook"],
)
def test_normalize_pdf_rejects_credential_bearing_links(uri: str) -> None:
    pdf = _make_pdf_with_link(b"BT /F1 12 Tf (hello) Tj ET", uri=uri)

    with pytest.raises(MonitorError, match="credentials"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf")
        )


def test_normalize_pdf_bounds_font_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdf")
    pdf = _make_pdf(b"BT /F1 12 Tf (hello) Tj ET", compressed=False)
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_FONTS", 0)

    with pytest.raises(MonitorError, match="font resources"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf")
        )


def test_normalize_pdf_bounds_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdf")
    pdf = _make_pdf_with_link(
        b"BT /F1 12 Tf (hello) Tj ET", uri="https://example.com/item"
    )
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_ANNOTATIONS", 0)

    with pytest.raises(MonitorError, match="annotations"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf")
        )


def test_pypdf_recovery_input_limit_is_applied_and_restored() -> None:
    pytest.importorskip("pypdf")
    from pypdf import filters  # ruff: ignore[import-outside-top-level]

    original = filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH
    with monitor._pypdf_output_limits(2):  # pyright: ignore[reportPrivateUsage]
        assert filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH == 2
        with pytest.raises(MonitorError, match="recovery input"):
            filters.decompress(b"\x00" * 10)
    assert original == filters.ZLIB_MAX_RECOVERY_INPUT_LENGTH


def test_normalize_pdf_bounds_extracted_text() -> None:
    pytest.importorskip("pypdf")
    pdf = _make_pdf(b"BT /F1 12 Tf 72 200 Td (abcdefghij) Tj ET", compressed=True)

    with pytest.raises(MonitorError, match="extracted text"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf"),
            max_pdf_decompressed_bytes=4_096,
            max_pdf_extracted_chars=5,
        )


def test_normalize_pdf_bounds_page_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter  # ruff: ignore[import-outside-top-level]

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_PAGES", 1)

    with pytest.raises(MonitorError, match="page count"):
        normalize_document(
            Document(
                output.getvalue(), "https://example.com/file.pdf", "application/pdf"
            )
        )


def test_normalize_pdf_bounds_object_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter  # ruff: ignore[import-outside-top-level]

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_OBJECTS", 1)

    with pytest.raises(MonitorError, match="object traversal"):
        normalize_document(
            Document(
                output.getvalue(), "https://example.com/file.pdf", "application/pdf"
            )
        )


def test_run_local_file_writes_normalized_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    output = tmp_path / "snapshot.txt"
    source.write_text("<main> Alpha   Beta </main>")
    args = Namespace(
        url=None,
        input=source,
        source_url="http://93.184.216.34/",
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


def _run_local_with_output(source: Path, output: Path) -> dict[str, object]:
    return run(
        Namespace(
            url=None,
            input=source,
            source_url="http://93.184.216.34/",
            content_type="text/html",
            previous=None,
            output=output,
            timeout=30.0,
            max_bytes=1024,
            max_diff_lines=20,
        )
    )


def test_run_output_replaces_symlink_without_following_target(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    target = tmp_path / "target.txt"
    output = tmp_path / "snapshot.txt"
    source.write_text("<main>new</main>")
    target.write_text("keep")
    output.symlink_to(target)

    _run_local_with_output(source, output)

    assert target.read_text() == "keep"
    assert not output.is_symlink()
    assert output.read_text() == "new\n"


def test_run_output_replaces_fifo_without_blocking(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    output = tmp_path / "snapshot.fifo"
    source.write_text("<main>new</main>")
    os.mkfifo(output)

    _run_local_with_output(source, output)

    assert output.is_file()
    assert output.read_text() == "new\n"


def test_run_rejects_same_previous_and_output_path(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    snapshot = tmp_path / "snapshot.txt"
    source.write_text("<main>new</main>")
    snapshot.write_text("old\n")
    args = Namespace(
        url=None,
        input=source,
        source_url="http://93.184.216.34/",
        content_type="text/html",
        previous=snapshot,
        output=snapshot,
        timeout=30.0,
        max_bytes=1024,
        max_diff_lines=20,
    )

    with pytest.raises(MonitorError, match="different files"):
        run(args)


@pytest.mark.parametrize(
    "source_url",
    [
        "http://93.184.216.34/?token=secret",
        "http://93.184.216.34/#token=secret",
        "http://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
    ],
    ids=["query", "fragment", "webhook"],
)
def test_read_document_rejects_unsafe_source_url(
    tmp_path: Path, source_url: str
) -> None:
    source = tmp_path / "page.html"
    source.write_text("<p>hello</p>")

    with pytest.raises(MonitorError, match=r"credential|fragment"):
        monitor.read_document(
            source,
            source_url=source_url,
            content_type="text/html",
            max_bytes=1024,
        )


def test_run_applies_timeout_to_input_source_url_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "page.html"
    source.write_text("<p>hello</p>")
    deadlines: list[float | None] = []

    def resolve_addresses(
        _host: str, _port: int, deadline: float | None
    ) -> tuple[str, ...]:
        deadlines.append(deadline)
        message = "DNS resolution exceeded the fetch deadline"
        raise TimeoutError(message)

    monkeypatch.setattr(monitor, "_resolve_addresses", resolve_addresses)
    args = Namespace(
        url=None,
        input=source,
        source_url="https://example.com/",
        content_type="text/html",
        previous=None,
        output=None,
        timeout=0.1,
        max_bytes=1024,
        max_diff_lines=20,
    )

    with pytest.raises(TimeoutError, match="DNS resolution"):
        run(args)
    assert deadlines
    assert deadlines[0] is not None


def test_read_document_bounds_input_before_retaining_it(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_bytes(b"x" * 1025)

    with pytest.raises(MonitorError, match="max-bytes"):
        monitor.read_document(
            source,
            source_url="",
            content_type="text/plain",
            max_bytes=1024,
        )


def test_read_document_rejects_symlink_input(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text("hello")
    link = tmp_path / "link.html"
    link.symlink_to(source)

    with pytest.raises(MonitorError, match="regular file"):
        monitor.read_document(
            link,
            source_url="",
            content_type="text/plain",
            max_bytes=1024,
        )


@pytest.mark.parametrize("option", ["input", "previous"], ids=["input", "previous"])
def test_read_regular_file_rejects_fifo_without_blocking(
    tmp_path: Path, option: str
) -> None:
    fifo = tmp_path / f"{option}.fifo"
    os.mkfifo(fifo)

    with pytest.raises(MonitorError, match="regular file"):
        monitor._read_regular_file_limited(  # pyright: ignore[reportPrivateUsage]
            fifo, 1024, f"--{option}"
        )


def test_run_bounds_previous_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text("<p>hello</p>")
    previous = tmp_path / "previous.txt"
    snapshot_limit = monitor._DEFAULT_MAX_SNAPSHOT_BYTES  # pyright: ignore[reportPrivateUsage]
    previous.write_bytes(b"x" * (snapshot_limit + 1))

    args = Namespace(
        url=None,
        input=source,
        source_url="http://93.184.216.34/",
        content_type="text/html",
        previous=previous,
        output=None,
        timeout=30.0,
        max_bytes=monitor._DEFAULT_MAX_BYTES,  # pyright: ignore[reportPrivateUsage]
        max_diff_lines=20,
    )

    with pytest.raises(MonitorError, match="max-bytes"):
        run(args)
