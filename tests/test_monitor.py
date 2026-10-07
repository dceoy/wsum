"""Tests for the monitor module."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import builtins
import ipaddress
import os
import runpy
import ssl
import tomllib
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from time import sleep
from types import SimpleNamespace
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


def test_user_agent_matches_project_version() -> None:
    with Path("pyproject.toml").open("rb") as stream:
        project_version = tomllib.load(stream)["project"]["version"]

    assert monitor._USER_AGENT == f"wsum/{project_version}"


def test_normalize_html_removes_markup_and_scripts() -> None:
    document = Document(
        b"<html><body><h1>Hello</h1><script>ignore()</script>"
        b"<p>World</p></body></html>",
        "https://example.com/",
        "text/html",
    )

    assert normalize_document(document) == "Hello\nWorld\n"


@pytest.mark.parametrize(
    ("current", "previous", "status", "previous_sha256", "diff"),
    [
        ("alpha\n", None, "baseline", "", ""),
        (
            "alpha\n",
            "alpha\n",
            "unchanged",
            "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060",
            "",
        ),
        (
            "beta\n",
            "alpha\n",
            "changed",
            "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060",
            "--- previous\n+++ current\n@@ -1 +1 @@\n-alpha\n+beta",
        ),
    ],
    ids=["baseline", "unchanged", "changed"],
)
def test_compare_text_reports_baseline_unchanged_and_change(
    current: str,
    previous: str | None,
    status: str,
    previous_sha256: str,
    diff: str,
) -> None:
    result = compare_text(current, previous, max_diff_lines=20)

    assert result["status"] == status
    assert result["previous_sha256"] == previous_sha256
    assert result["diff"] == diff
    assert result["diff_truncated"] is False


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
    ids=["pdf", "html", "htm-alias", "rss", "atom", "xml", "plain-text"],
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


class _TextReader:
    @staticmethod
    def read(_size: int) -> str:
        return "not bytes"


class _OversizedReader:
    @staticmethod
    def read(_size: int) -> bytes:
        return b"012345"


@pytest.mark.parametrize(
    ("response", "limit", "message"),
    [
        (object(), 10, "not readable"),
        (_TextReader(), 10, "did not return bytes"),
        (_OversizedReader(), 3, "max-bytes"),
    ],
    ids=["missing-reader", "text-chunk", "oversized-chunk"],
)
def test_read_response_rejects_invalid_readers_and_chunks(
    response: object, limit: int, message: str
) -> None:
    with pytest.raises(MonitorError, match=message):
        monitor._read_response_limited(  # pyright: ignore[reportPrivateUsage]
            response, limit
        )


def test_parse_content_type_extracts_charset() -> None:
    assert monitor._parse_content_type(  # pyright: ignore[reportPrivateUsage]
        "text/html; charset=utf-8"
    ) == ("text/html", "utf-8")


def _resolver_failure() -> int:
    message = "resolver failed"
    raise ValueError(message)


@pytest.mark.parametrize(
    ("resolver", "timeout", "error", "message"),
    [
        (_resolver_failure, 1.0, ValueError, "resolver failed"),
        (lambda: 42, 0.0, TimeoutError, "deadline"),
    ],
    ids=["worker-error", "expired-deadline"],
)
def test_resolver_pool_propagates_errors_and_deadlines(
    resolver: Callable[[], int],
    timeout: float,
    error: type[Exception],
    message: str,
) -> None:
    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(error, match=message):
        pool.resolve(resolver, (), {}, timeout)


def test_resolver_pool_returns_result() -> None:
    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]

    assert pool.resolve(lambda: 42, (), {}, 1.0) == 42


@pytest.mark.parametrize(
    "value", ["x" * 65, "not-a-real-encoding"], ids=["too-long", "unknown"]
)
def test_encoding_name_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(MonitorError, match="unsupported"):
        monitor._encoding_name(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (Document(b"hello", "https://example.com/", "image/png"), "unsupported"),
        (
            Document(
                b"<html><body>hello</body></html>",
                "https://example.com/",
                "application/xml",
            ),
            "does not match",
        ),
    ],
    ids=["unsupported-content-type", "mismatched-document-type"],
)
def test_normalization_rejects_unsupported_and_mismatched_types(
    document: Document, message: str
) -> None:
    with pytest.raises(MonitorError, match=message):
        normalize_document(document)


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


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "192.0.0.8"], ids=["loopback", "reserved"]
)
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
        "http://93.184.216.34/?next=token%3Dsecret",
        "http://93.184.216.34/?%2574oken=secret",
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
        "decoded-query-text",
        "multiply-encoded-query-name",
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
            b'<?xml version="1.0" encoding="shift_jis"?><root>'
            + "こんにちは".encode("shift_jis")
            + b"</root>",
            "application/xml",
            None,
            "こんにちは\n",
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
        "xml-declaration",
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
    from pypdf import get_configuration  # ruff: ignore[import-outside-top-level]

    original = get_configuration().zlib_maximum_output_length
    recovery_original = get_configuration().zlib_maximum_recovery_input_length
    pdf = _make_pdf(b"q\n" * 2_000, compressed=True)

    with pytest.raises(MonitorError, match="PDF"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf"),
            max_pdf_decompressed_bytes=128,
            max_pdf_extracted_chars=1_000,
        )

    assert original == get_configuration().zlib_maximum_output_length
    assert recovery_original == get_configuration().zlib_maximum_recovery_input_length


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


def _pdf_with_multiple_pages() -> bytes:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter  # ruff: ignore[import-outside-top-level]

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _pdf_with_single_page() -> bytes:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter  # ruff: ignore[import-outside-top-level]

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("pdf_factory", "limit_name", "limit", "message"),
    [
        (
            lambda: _make_pdf(b"BT /F1 12 Tf (hello) Tj ET", compressed=False),
            "_DEFAULT_MAX_PDF_FONTS",
            0,
            "font resources",
        ),
        (
            lambda: _make_pdf_with_link(
                b"BT /F1 12 Tf (hello) Tj ET", uri="https://example.com/item"
            ),
            "_DEFAULT_MAX_PDF_ANNOTATIONS",
            0,
            "annotations",
        ),
        (_pdf_with_multiple_pages, "_DEFAULT_MAX_PDF_PAGES", 1, "page count"),
        (
            _pdf_with_single_page,
            "_DEFAULT_MAX_PDF_OBJECTS",
            1,
            "object traversal",
        ),
    ],
    ids=["font-resources", "annotations", "pages", "objects"],
)
def test_normalize_pdf_bounds_structure(
    monkeypatch: pytest.MonkeyPatch,
    pdf_factory: Callable[[], bytes],
    limit_name: str,
    limit: int,
    message: str,
) -> None:
    pdf = pdf_factory()
    monkeypatch.setattr(monitor, limit_name, limit)

    with pytest.raises(MonitorError, match=message):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf")
        )


def test_pypdf_recovery_input_limit_is_applied_and_restored() -> None:
    pytest.importorskip("pypdf")
    from pypdf import (  # ruff: ignore[import-outside-top-level]
        filters,
        get_configuration,
    )

    original = get_configuration()
    limits = monitor._pypdf_output_limits(2)  # pyright: ignore[reportPrivateUsage]
    with limits as set_limit:
        assert get_configuration().zlib_maximum_recovery_input_length == 2
        assert get_configuration().zlib_maximum_output_length == 2
        set_limit(1)
        assert get_configuration().zlib_maximum_recovery_input_length == 1
        assert get_configuration().zlib_maximum_output_length == 1
        with pytest.raises(MonitorError, match="recovery input"):
            filters.decompress(b"\x00" * 10)
    assert get_configuration() == original


def test_normalize_pdf_bounds_extracted_text() -> None:
    pytest.importorskip("pypdf")
    pdf = _make_pdf(b"BT /F1 12 Tf 72 200 Td (abcdefghij) Tj ET", compressed=True)

    with pytest.raises(MonitorError, match="extracted text"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf"),
            max_pdf_decompressed_bytes=4_096,
            max_pdf_extracted_chars=5,
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


@pytest.mark.parametrize("line_break", ["\n", "\r"], ids=["lf", "cr"])
def test_diff_complexity_short_circuits_before_unified_diff(
    monkeypatch: pytest.MonkeyPatch, line_break: str
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
    previous = line_break.join(f"old-{index}" for index in range(2_100))
    current = line_break.join(f"new-{index}" for index in range(2_100))

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
    ("content_type", "template", "before_href", "after_href"),
    [
        (
            "application/rss+xml",
            (
                '<rss><channel><item><description><![CDATA[<a href="{href}">'
                "Apply</a>]]></description></item></channel></rss>"
            ),
            "/v1",
            "/v2",
        ),
        (
            "application/rss+xml",
            (
                '<rss><channel><item><description><![CDATA[<a href="{href}">'
                "Apply</a>]]></description></item></channel></rss>"
            ),
            "https://example.com/v1",
            "https://example.com/v2",
        ),
        (
            "application/atom+xml",
            (
                '<feed><entry><id>entry-1</id><content type="xhtml">'
                '<div xmlns="http://www.w3.org/1999/xhtml"><a href="{href}">'
                "Apply</a></div></content></entry></feed>"
            ),
            "/v1",
            "/v2",
        ),
        (
            "application/atom+xml",
            (
                '<feed><entry><id>entry-1</id><content type="xhtml">'
                '<div xmlns="http://www.w3.org/1999/xhtml"><a href="{href}">'
                "Apply</a></div></content></entry></feed>"
            ),
            "https://example.com/v1",
            "https://example.com/v2",
        ),
    ],
    ids=["rss-relative", "rss-absolute", "atom-relative", "atom-absolute"],
)
def test_feed_embedded_destination_change_is_detected(
    content_type: str,
    template: str,
    before_href: str,
    after_href: str,
) -> None:
    def make_document(href: str) -> Document:
        return Document(
            template.format(href=href).encode(),
            "https://example.com/feed",
            content_type,
        )

    previous = normalize_document(make_document(before_href))
    current = normalize_document(make_document(after_href))

    assert previous != current
    assert before_href not in previous
    assert after_href not in current


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "application/rss+xml",
            (
                b"<rss><channel><item><description><![CDATA["
                b'<a href="https://example.com/?token=secret">Apply</a>'
                b"]]></description></item></channel></rss>"
            ),
        ),
        (
            "application/rss+xml",
            (
                b"<rss><channel><item><description>"
                b'&lt;a href="https://example.com/?token=secret"&gt;Apply&lt;/a&gt;'
                b"</description></item></channel></rss>"
            ),
        ),
        (
            "application/atom+xml",
            (
                b'<feed><entry><id>entry-1</id><content type="xhtml">'
                b'<div xmlns="http://www.w3.org/1999/xhtml">'
                b'<a href="https://example.com/?token=secret">Apply</a>'
                b"</div></content></entry></feed>"
            ),
        ),
        (
            "application/atom+xml",
            (
                b'<feed><entry><id>entry-1</id><content type="html">'
                b'&lt;a href="https://example.com/?token=secret"&gt;Apply&lt;/a&gt;'
                b"</content></entry></feed>"
            ),
        ),
    ],
    ids=["rss-cdata", "rss-escaped-html", "atom-xhtml", "atom-escaped-html"],
)
def test_feed_embedded_html_rejects_credential_bearing_destination(
    content_type: str, body: bytes
) -> None:
    document = Document(body, "https://example.com/feed", content_type)

    with pytest.raises(MonitorError, match="credentials"):
        normalize_document(document)


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
        (
            b'<base href="/v1/"><a href="download">Download</a>',
            b'<base href="/v2/"><a href="download">Download</a>',
        ),
    ],
    ids=["link", "form", "base-relative-link"],
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


_WINDOWS_1252_META = (
    b"<meta http-equiv='Content-Type' content='text/html; charset=windows-1252'>"
)


class _DeadlineProbeSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.timeouts: list[float | None] = []

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value
        self.timeouts.append(value)

    def recv(self, *_args: Any, **_kwargs: Any) -> bytes:  # ruff: ignore[any-type, no-self-use]
        return b"data"

    def recv_into(self, buffer: bytearray, *_args: Any, **_kwargs: Any) -> int:  # ruff: ignore[any-type, no-self-use]
        buffer[:4] = b"data"
        return 4


class _DeadlineProbe(monitor._DeadlineTrackingMixin, _DeadlineProbeSocket):
    pass


@pytest.mark.parametrize("method", ["recv", "recv_into"], ids=["recv", "recv-into"])
def test_deadline_socket_clamps_reads_and_rejects_expired_deadline(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    now = 10.0
    monkeypatch.setattr(monitor, "monotonic", lambda: now)
    socket = _DeadlineProbe()
    socket.settimeout(None)
    socket.set_deadline(14.0)
    if method == "recv":
        assert socket.recv(4) == b"data"
    else:
        assert socket.recv_into(bytearray(4)) == 4
    assert socket.timeouts[-1] == 4.0  # ruff: ignore[float-equality-comparison]

    socket.settimeout(2.0)
    socket.set_deadline(15.0)
    if method == "recv":
        socket.recv(4)
    else:
        socket.recv_into(bytearray(4))
    assert socket.timeouts[-1] == 2.0  # ruff: ignore[float-equality-comparison]

    socket.set_deadline(10.0)
    with pytest.raises(TimeoutError, match="fetch deadline"):  # ruff: ignore[pytest-raises-with-multiple-statements]
        if method == "recv":
            socket.recv(4)
        else:
            socket.recv_into(bytearray(4))


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("<p>A<br>B</p>", "A\nB"),
        ("<script><p>ignored</p></script><div>shown</div>", "shown"),
        ("<base href='/v1/'><base href='/v2/'><a href='x'>go</a>", "go"),
    ],
    ids=["line-break", "skip-nested-markup", "first-base-only"],
)
def test_html_extractor_handles_blocks_skips_and_base_urls(
    fragment: str, expected: str
) -> None:
    parser = monitor._TextExtractor("https://example.com/")
    parser.feed(fragment)
    parser.close()
    normalized = monitor._normalize_whitespace("".join(parser.parts))
    assert expected in normalized
    if "base" in fragment:
        digest = monitor.hashlib.sha256(b"https://example.com/v1/x").hexdigest()
        assert digest in normalized


def test_html_extractor_keeps_first_destinations_and_flags_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "_MAX_HTML_DESTINATIONS", 1)
    parser = monitor._TextExtractor("https://example.com/")
    parser.feed('<a href="/one">one</a><a href="/two">two</a><form action="/f"></form>')
    parser.close()
    first = monitor.hashlib.sha256(b"https://example.com/one").hexdigest()
    second = monitor.hashlib.sha256(b"https://example.com/two").hexdigest()
    assert list(parser.links) == [first]
    assert parser.links.omitted_hashes == set()
    assert parser.links.overflow_count == 2
    text = "".join(parser.parts)
    assert second not in text
    assert "[omitted-destinations:2:sha256:" in text


def test_html_extractor_overflow_metadata_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "_MAX_HTML_DESTINATIONS", 1)
    parser = monitor._TextExtractor("https://example.com/")
    parser.feed("".join(f'<a href="/{index}">x</a>' for index in range(2_000)))
    parser.close()
    assert parser.links.omitted_hashes == set()
    assert parser.links.overflow_count == 1_999
    assert len(parser.links) == 1


def test_html_extractor_rejects_credentials_after_destination_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "_MAX_HTML_DESTINATIONS", 1)
    parser = monitor._TextExtractor("https://example.com/")
    fragment = (
        '<a href="/one">one</a>'
        '<a href="https://user:pass@example.com/private">secret</a>'
    )
    with pytest.raises(MonitorError, match="credentials"):
        parser.feed(fragment)


def test_html_extractor_omitted_destination_change_alters_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "_MAX_HTML_DESTINATIONS", 1)

    def text_for(html: str) -> str:
        parser = monitor._TextExtractor("https://example.com/")
        parser.feed(html)
        parser.close()
        parser.close()
        return "".join(parser.parts)

    base = '<a href="/one">one</a><a href="/two">same</a>'
    assert text_for(base) != text_for(base.replace("/two", "/changed"))
    assert text_for(base) == text_for(base)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1",  # malformed authority
        "ftp://example.com/",  # unsupported scheme
        "http:///missing-host",  # absent host
        "http://user@example.com/",  # user information
        "http://example.com/#fragment",  # fragment
        "http://discord.com/api/webhooks/123/secret",  # webhook path
    ],
    ids=[
        "invalid-url",
        "scheme",
        "missing-host",
        "userinfo",
        "fragment",
        "webhook",
    ],
)
def test_public_url_rejects_invalid_components(url: str) -> None:
    with pytest.raises(MonitorError):
        monitor._resolve_public_url(url)  # pyright: ignore[reportPrivateUsage]


def test_public_url_rejects_control_characters_in_parsed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = SimpleNamespace(
        scheme="http",
        hostname="exa\rample.com",
        port=None,
        username=None,
        password=None,
        fragment="",
        query="",
    )
    monkeypatch.setattr(monitor, "urlsplit", lambda _url: parsed)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(MonitorError, match="host contains control characters"):
        monitor._resolve_public_url("http://example.com/")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "value",
    [
        # malformed nested authority
        "https://example.com/?next=https%3A%2F%2F%5B%3A%3A1",
        "https://example.com/?next=%252F%252Fuser%2540example.com",  # nested userinfo
        "https://example.com/?next=%252525252525token%253Dsecret",  # depth limit
    ],
    ids=["malformed-nested", "nested-userinfo", "nested-depth-limit"],
)
def test_nested_credentials_fail_closed(value: str) -> None:
    assert monitor.url_has_credentials(value)


@pytest.mark.parametrize(
    ("address", "expected"),
    [("192.0.0.9", True), ("192.0.0.10", True), ("10.0.0.1", False)],
    ids=["special-public-address-a", "special-public-address-b", "private-address"],
)
def test_ipv4_special_public_allowlist(address: str, expected: bool) -> None:
    assert monitor._is_public_unicast(ipaddress.ip_address(address)) is expected  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure",
    [None, "peer", "connect"],
    ids=["connected", "wrong-peer", "connect-error"],
)
def test_connect_pinned_socket_closes_or_returns_socket(  # ruff: ignore[complex-structure]
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    class FakeSocket:
        def __init__(self, _family: int, _socket_type: int) -> None:
            self.closed = False
            self.address: tuple[Any, ...] | None = None

        def set_deadline(self, _deadline: float) -> None:
            pass

        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, address: tuple[Any, ...]) -> None:
            self.address = address
            if failure == "connect":
                raise OSError("connection refused")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        def getpeername(self) -> tuple[str, int]:  # ruff: ignore[no-self-use]
            if failure == "peer":
                return "93.184.216.35", 80
            return "93.184.216.34", 80

        def close(self) -> None:
            self.closed = True

    instance: FakeSocket | None = None

    def create(family: int, socket_type: int) -> FakeSocket:
        nonlocal instance
        instance = FakeSocket(family, socket_type)
        return instance

    monkeypatch.setattr(monitor, "_DeadlineSocket", create)
    if failure is None:
        result = monitor._connect_pinned_socket(  # pyright: ignore[reportPrivateUsage]
            "93.184.216.34", 80, deadline=monitor.monotonic() + 2
        )
        assert result is instance
        assert instance is not None
        assert not instance.closed
        assert instance.address == ("93.184.216.34", 80)
    else:
        message = "connected peer" if failure == "peer" else "connection refused"
        with pytest.raises(
            MonitorError if failure == "peer" else OSError, match=message
        ):
            monitor._connect_pinned_socket(  # pyright: ignore[reportPrivateUsage]
                "93.184.216.34", 80, deadline=monitor.monotonic() + 2
            )
        assert instance is not None
        assert instance.closed


@pytest.mark.parametrize(
    ("family", "expected_address"),
    [
        ("93.184.216.34", ("93.184.216.34", 443)),
        ("2001:4860:4860::8888", ("2001:4860:4860::8888", 443, 0, 0)),
    ],
    ids=["ipv4-sockaddr", "ipv6-sockaddr"],
)
def test_connect_pinned_socket_formats_address_families(
    monkeypatch: pytest.MonkeyPatch, family: str, expected_address: tuple[Any, ...]
) -> None:
    captured: list[tuple[Any, ...]] = []

    class FakeSocket:
        def __init__(self, _family: int, _socket_type: int) -> None:
            pass

        def set_deadline(self, _deadline: float) -> None:
            pass

        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, address: tuple[Any, ...]) -> None:  # ruff: ignore[no-self-use]
            captured.append(address)

        def getpeername(self) -> tuple[str, int]:  # ruff: ignore[no-self-use]
            return family, 443

        def close(self) -> None:  # ruff: ignore[no-self-use]
            raise AssertionError("successful socket must remain open")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

    monkeypatch.setattr(monitor, "_DeadlineSocket", FakeSocket)
    monitor._connect_pinned_socket(family, 443, deadline=monitor.monotonic() + 2)  # pyright: ignore[reportPrivateUsage]
    assert captured == [expected_address]


@pytest.mark.parametrize(
    ("want", "readiness", "message"),
    [
        ("read", True, ""),
        ("write", True, ""),
        ("read", False, "deadline"),
        ("write", False, "deadline"),
    ],
    ids=["read-retry", "write-retry", "read-timeout", "write-timeout"],
)
def test_tls_handshake_retries_and_enforces_deadline(
    monkeypatch: pytest.MonkeyPatch, want: str, readiness: bool, message: str
) -> None:
    class FakeSocket:
        timeout: float | None = 3.0
        calls = 0
        blocking: bool | None = None

        def gettimeout(self) -> float | None:
            return self.timeout

        def settimeout(self, value: float | None) -> None:
            self.timeout = value

        def setblocking(self, value: bool) -> None:
            self.blocking = value

        def do_handshake(self) -> None:
            self.calls += 1
            if self.calls == 1:
                error = (
                    ssl.SSLWantReadError if want == "read" else ssl.SSLWantWriteError
                )
                raise error()

    fake = FakeSocket()

    def select(
        reads: list[object],
        writes: list[object],
        _exceptional: list[object],
        _timeout: float,
    ) -> tuple[list[object], list[object], list[object]]:
        return (
            reads if readiness and reads else [],
            writes if readiness and writes else [],
            [],
        )

    monkeypatch.setattr(monitor.select, "select", select)
    if readiness:
        monitor._do_handshake_with_deadline(fake, monitor.monotonic() + 2)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
        assert fake.calls == 2
    else:
        with pytest.raises(TimeoutError, match=message):
            monitor._do_handshake_with_deadline(fake, monitor.monotonic() + 2)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
        assert fake.calls == 1
    assert fake.blocking is False
    assert fake.timeout == 3.0  # ruff: ignore[float-equality-comparison]


def test_wrap_tls_rejects_unguarded_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class Guarded:
        pass

    class Context:
        minimum_version: object = None
        sslsocket_class: object = None

        def wrap_socket(self, *_args: Any, **_kwargs: Any) -> object:  # ruff: ignore[any-type, no-self-use]
            return object()

    monkeypatch.setattr(monitor, "_DeadlineSSLSocket", Guarded)
    monkeypatch.setattr(monitor.ssl, "create_default_context", Context)
    with pytest.raises(MonitorError, match="unguarded socket"):
        monitor._wrap_tls(object(), "example.com", monitor.monotonic() + 1)  # pyright: ignore[reportArgumentType, reportPrivateUsage]


@pytest.mark.parametrize(
    ("addresses", "error", "expected"),
    [
        (("one", "two"), "first-fails", "second-succeeds"),
        (("one",), "timeout", "timeout"),
        (("one",), "oserror", "all-fail"),
        (("one",), "monitor", "monitor-error"),
    ],
    ids=[
        "retry-address",
        "rethrow-timeout",
        "wrap-all-errors",
        "preserve-monitor-error",
    ],
)
def test_open_connection_handles_address_failures(
    monkeypatch: pytest.MonkeyPatch,
    addresses: tuple[str, ...],
    error: str,
    expected: str,
) -> None:
    class FakeSocket:
        closed = False

        def close(self) -> None:
            self.closed = True

    created: list[FakeSocket] = []
    calls: list[str] = []

    def connect(address: str, _port: int, *, deadline: float) -> FakeSocket:
        del deadline
        calls.append(address)
        if error != "first-fails" or address == "one":
            if error == "timeout":
                raise TimeoutError("late")  # ruff: ignore[raw-string-in-exception]
            if error == "monitor":
                raise MonitorError("invalid")  # ruff: ignore[raw-string-in-exception]
            raise OSError("down")  # ruff: ignore[raw-string-in-exception]
        sock = FakeSocket()
        created.append(sock)
        return sock

    class FakeConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
            self.sock: object | None = None

    monkeypatch.setattr(monitor, "_connect_pinned_socket", connect)
    monkeypatch.setattr(monitor.http.client, "HTTPConnection", FakeConnection)
    target = monitor._ResolvedTarget(  # pyright: ignore[reportPrivateUsage]
        "http://example.com", "http", "example.com", 80, addresses
    )
    if expected == "second-succeeds":
        result = monitor._open_connection(target, deadline=monitor.monotonic() + 2)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(result, FakeConnection)
        assert calls == ["one", "two"]
        assert len(created) == 1
    else:
        exception = TimeoutError if expected == "timeout" else MonitorError
        message = (
            "late"
            if expected == "timeout"
            else (
                "invalid" if expected == "monitor-error" else "every validated address"
            )
        )
        with pytest.raises(exception, match=message):
            monitor._open_connection(target, deadline=monitor.monotonic() + 2)  # pyright: ignore[reportPrivateUsage]
        assert created == []


def test_request_target_rejects_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor,
        "urlsplit",
        lambda _value: type("Parts", (), {"path": "/bad\r\npath", "query": ""})(),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    with pytest.raises(MonitorError, match="control characters"):
        monitor._request_target("http://example.com/")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (500, {}, "HTTP 500"),
        (200, {"Content-Encoding": "gzip"}, "compressed"),
        (200, {"Content-Length": "invalid"}, "Content-Length"),
        (200, {"Content-Length": "20"}, "max-bytes"),
    ],
    ids=["server-error", "compressed-response", "invalid-length", "oversized-length"],
)
def test_response_validation_rejects_unsafe_responses(
    status: int, headers: dict[str, str], message: str
) -> None:
    response = type(
        "Response",
        (),
        {
            "status": status,
            "getheader": lambda _self, name, default=None: headers.get(name, default),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        },
    )()
    with pytest.raises(MonitorError, match=message):
        monitor._validate_response(response, max_bytes=10)  # pyright: ignore[reportArgumentType, reportPrivateUsage]


def test_response_validation_exposes_http_status() -> None:
    response = type(
        "Response",
        (),
        {
            "status": 403,
            "getheader": lambda _self, _name, default=None: default,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        },
    )()
    with pytest.raises(monitor.HTTPStatusError, match="HTTP 403") as captured:
        monitor._validate_response(response, max_bytes=10)  # pyright: ignore[reportArgumentType, reportPrivateUsage]

    assert captured.value.status == 403


def test_read_response_supports_read_fallback_closed_response_and_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self) -> None:
            self.parts = [b"abc", b""]

        def read(self, _size: int) -> bytes:
            return self.parts.pop(0)

        def isclosed(self) -> bool:  # ruff: ignore[no-self-use]
            return False

    class Transport:
        timeouts: list[float] = []  # ruff: ignore[mutable-class-default]

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    transport = Transport()
    monkeypatch.setattr(monitor, "monotonic", lambda: 10.0)
    assert (
        monitor._read_response_limited(  # pyright: ignore[reportPrivateUsage]
            Reader(),
            10,
            deadline=12.0,
            sock=transport,  # pyright: ignore[reportArgumentType]
        )
        == b"abc"
    )
    assert transport.timeouts == [2.0, 2.0]

    class Closed:
        @staticmethod
        def read(_size: int) -> bytes:
            raise AssertionError("closed responses must not be read")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        @staticmethod
        def isclosed() -> bool:
            return True

    assert monitor._read_response_limited(Closed(), 10) == b""  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="positive"):
        monitor._read_response_limited(Reader(), 0)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("text/plain", b"<?xml version='1.0'?><html>ok</html>", "text/html"),
        ("text/plain", b"<rss><channel/></rss>", "application/rss+xml"),
        ("text/plain", b"\xff", "text/plain"),
    ],
    ids=["xml-html", "feed-sniff", "invalid-charset-fallback"],
)
def test_type_sniffing_and_plain_text_fallback(
    content_type: str, body: bytes, expected: str
) -> None:
    assert (
        monitor._normalization_content_type(  # pyright: ignore[reportPrivateUsage]
            Document(body, "https://example.com/", content_type)
        )
        == expected
    )


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (
            _WINDOWS_1252_META,
            "text/html",
            "windows-1252",
        ),
        (b"<p>plain</p>", "text/html", None),
        (b"<?xml version='1.0' encoding='utf-8'?><root/>", "application/xml", "utf-8"),
        (b"plain", "text/plain", None),
    ],
    ids=["html-http-equiv", "html-no-charset", "xml-declaration", "plain-text"],
)
def test_document_encoding_declarations(
    body: bytes, content_type: str, expected: str | None
) -> None:
    assert monitor._in_document_encoding(body, content_type) == expected  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("value", ["", "utf-7"], ids=["empty", "unsupported-codec"])
def test_encoding_name_rejects_unsupported_codec(value: str) -> None:
    with pytest.raises(MonitorError, match="unsupported"):
        monitor._encoding_name(value)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("x" * 4_097, "base URL"),
        ("https://example.com/", "size limit"),
    ],
    ids=["oversized-base-url", "oversized-text"],
)
def test_feed_parser_enforces_base_and_text_limits(base_url: str, message: str) -> None:
    if message == "base URL":
        with pytest.raises(MonitorError, match=message):
            monitor._FeedTextTarget(10, preserve_structure=False, base_url=base_url)  # pyright: ignore[reportPrivateUsage]
    else:
        target = monitor._FeedTextTarget(0, preserve_structure=False, base_url=base_url)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(MonitorError, match=message):
            target.data("x")


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (b"<!DOCTYPE rss><rss/>", "DOCTYPE"),
        (b"<rss><item>", "could not be parsed"),
    ],
    ids=["doctype", "malformed-xml"],
)
def test_feed_rejects_doctype_and_parse_failures(xml: bytes, message: str) -> None:
    with pytest.raises(MonitorError, match=message):
        monitor._normalize_feed(  # pyright: ignore[reportPrivateUsage]
            xml.decode(),
            max_chars=100,
            preserve_structure=True,
            base_url="https://example.com/",
        )


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "replace", "revision": "a" * 32, "target_id": "x", "version": 1},
        None,
    ],
    ids=["reader-error", "reader-returns-none"],
)
def test_pdf_preflight_handles_empty_and_unreadable_page_trees(record: object) -> None:
    if record is None:
        reader = type("Reader", (), {"trailer": {"/Root": {"/Pages": {"/Count": 0}}}})()
        assert monitor._preflight_pdf_pages(reader) == [1]  # pyright: ignore[reportPrivateUsage]
    else:
        pages = {"/Count": 1, "/Kids": [object()]}
        reader = type("Reader", (), {"trailer": {"/Root": {"/Pages": pages}}})()
        assert monitor._preflight_pdf_pages(reader) == [2]  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("contents", "resources", "expected_count"),
    [
        (None, None, 0),
        ([b"same", b"same"], {"/XObject": []}, 1),
    ],
    ids=["no-streams", "deduplicated-streams"],
)
def test_pdf_content_stream_collection(
    contents: object, resources: object, expected_count: int
) -> None:
    class Page(dict[str, object]):  # ruff: ignore[subclass-builtin]
        def get(self, key: str, default: object = None) -> object:
            return super().get(key, default)

    page = Page({"/Contents": contents, "/Resources": resources})
    result = monitor._page_content_streams(page, [0])  # pyright: ignore[reportPrivateUsage]
    assert len(result) == expected_count


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        ({"/Subtype": "/Text"}, ""),
        (
            {"/Subtype": "/Link", "/A": {"/S": "/GoTo", "/URI": "https://example.com"}},
            "",
        ),
        (
            {
                "/Subtype": "/Link",
                "/A": {"/S": "/URI", "/URI": "mailto:test@example.com"},
            },
            "",
        ),
        (
            {"/Subtype": "/Link", "/A": {"/S": "/URI", "/URI": "https://example.com"}},
            "digest",
        ),
    ],
    ids=["non-link", "non-uri-action", "non-http-uri", "http-uri"],
)
def test_pdf_annotation_link_filters_unsupported_annotations(
    annotation: dict[str, str], expected: str
) -> None:
    result = monitor._pdf_annotation_link(annotation, [0])  # pyright: ignore[reportPrivateUsage]
    if expected == "digest":
        assert len(result) == 64
    else:
        assert result == expected


@pytest.mark.parametrize(
    "uri",
    ["mailto:test@example.com", "https://[::1", "relative/path"],
    ids=["other-scheme", "malformed", "relative"],
)
def test_pdf_link_destinations_reject_invalid_or_ignore_other_schemes(uri: str) -> None:
    if uri.startswith("mailto:"):
        assert monitor._pdf_link_destination(uri) == ""  # ruff: ignore[compare-to-empty-string]  # pyright: ignore[reportPrivateUsage]
    else:
        with pytest.raises(MonitorError):
            monitor._pdf_link_destination(uri)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("annots", ["bad", ["a", "b"]], ids=["wrong-type", "too-many"])
def test_pdf_page_annotations_validate_structure_and_count(
    monkeypatch: pytest.MonkeyPatch, annots: object
) -> None:
    if annots == ["a", "b"]:
        monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_ANNOTATIONS", 1)
    page = {"/Annots": annots}
    with pytest.raises(MonitorError, match="annotations"):
        monitor._page_link_destinations(page, [0], [0])  # pyright: ignore[reportPrivateUsage]


def test_pdf_text_extraction_handles_visitor_and_returned_text_limits() -> None:
    class Page:
        def extract_text(self, *, visitor_text: Any) -> str:  # ruff: ignore[any-type, no-self-use]
            visitor_text("x" * 6)
            return ""

    with pytest.raises(MonitorError, match="extracted text"):
        monitor._extract_page_text(Page(), extracted=0, maximum=5)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("current", "previous", "max_lines", "max_bytes"),
    [
        ("b", "a", 1, 100),
        ("長", "短", 20, 3),
        ("a\n", "b\n", 20, 100),
    ],
    ids=["line-budget", "utf8-byte-budget", "complete-diff"],
)
def test_bounded_diff_handles_line_and_byte_limits(
    current: str, previous: str, max_lines: int, max_bytes: int
) -> None:
    diff, truncated = monitor._bounded_diff(  # pyright: ignore[reportPrivateUsage]
        current, previous, max_diff_lines=max_lines, max_diff_bytes=max_bytes
    )
    assert len(diff.encode()) <= max_bytes
    if max_lines == 1 or max_bytes == 3:
        assert truncated


def test_utf8_prefix_empty_and_complete_prefix() -> None:
    assert monitor._utf8_prefix("abc", 0) == ""  # ruff: ignore[compare-to-empty-string]  # pyright: ignore[reportPrivateUsage]
    assert monitor._utf8_prefix("abc", 3) == "abc"  # pyright: ignore[reportPrivateUsage]


def test_compare_text_rejects_non_positive_limits() -> None:
    with pytest.raises(MonitorError, match="positive"):
        monitor.compare_text("a", "b", max_diff_lines=0)


def test_snapshot_write_cleans_temporary_file_on_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "snapshot.txt"
    original_replace = Path.replace

    def fail_replace(path: Path, target: Path) -> Path:
        if path.name.startswith(".snapshot.txt."):
            raise OSError("cannot replace")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace"):
        monitor._write_snapshot_atomic(destination, b"body")  # pyright: ignore[reportPrivateUsage]
    assert list(tmp_path.glob(".snapshot.txt.*.tmp")) == []


def test_run_rejects_non_positive_limits_and_invalid_previous_utf8(
    tmp_path: Path,
) -> None:
    source = tmp_path / "page.txt"
    source.write_text("<p>hello</p>")
    args = monitor._parser().parse_args(["--input", str(source), "--timeout", "0"])
    with pytest.raises(MonitorError, match="positive"):
        monitor.run(args)

    previous = tmp_path / "previous.txt"
    previous.write_bytes(b"\xff")
    args = monitor._parser().parse_args([
        "--input",
        str(source),
        "--previous",
        str(previous),
    ])
    with pytest.raises(MonitorError, match="valid UTF-8"):
        monitor.run(args)


def test_resolver_pool_covers_started_capacity_and_wait_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]
    assert pool.resolve(lambda: 1, (), {}, 1.0) == 1
    assert pool.resolve(lambda: 2, (), {}, 1.0) == 2

    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(pool, "_ensure_started", lambda: None)
    monkeypatch.setattr(pool._capacity, "acquire", lambda **_kwargs: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(TimeoutError, match="DNS resolution"):
        pool.resolve(lambda: 1, (), {}, 1.0)

    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]

    class RacingLock:
        def __enter__(self) -> None:
            pool._workers.append(object())  # pyright: ignore[reportArgumentType]

        def __exit__(self, *_args: object) -> None:
            pass

    pool._start_lock = RacingLock()  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    pool._ensure_started()  # pyright: ignore[reportPrivateUsage]

    class FakeCapacity:
        def acquire(self, *, timeout: float) -> bool:  # ruff: ignore[no-self-use]
            return timeout > 0

        def release(self) -> None:
            pass

    class FakeQueue:
        def put(self, _job: object) -> None:
            pass

    class FakeEvent:
        def wait(self, _timeout: float) -> bool:  # ruff: ignore[no-self-use]
            return False

        def set(self) -> None:
            pass

    class FakeJob:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
            self.done = FakeEvent()
            self.error = None
            self.result = None

    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(pool, "_ensure_started", lambda: None)
    monkeypatch.setattr(pool, "_capacity", FakeCapacity())
    monkeypatch.setattr(pool, "_jobs", FakeQueue())
    monkeypatch.setattr(monitor, "_ResolverJob", FakeJob)
    monkeypatch.setattr(monitor, "monotonic", lambda: 1.0)
    with pytest.raises(TimeoutError, match="DNS resolution"):
        pool.resolve(lambda: 1, (), {}, 1.0)


def test_html_extractor_handles_nested_skip_and_block_boundaries() -> None:
    parser = monitor._TextExtractor("https://example.com/")
    parser.feed("<span>before</span><div>after</div><template><p>hidden</p></template>")
    parser.close()
    result = "".join(parser.parts)
    assert "before\nafter" in result
    assert "hidden" not in result


@pytest.mark.parametrize(
    ("value", "depth"),
    [
        ("https://example.com/", 6),
        ("https://[::1", 0),
        ("https://[abc]/", 0),
        ("%25252525252561", 0),
    ],
    ids=["depth-limit", "malformed-url", "invalid-host", "decode-limit"],
)
def test_nested_url_credential_validation_fails_closed(value: str, depth: int) -> None:
    assert monitor._nested_url_has_credentials(  # pyright: ignore[reportPrivateUsage]
        value, depth=depth
    )


def test_nested_query_depth_and_decode_limits() -> None:
    assert monitor._query_has_credentials(  # pyright: ignore[reportPrivateUsage]
        "safe=1", depth=6
    )
    assert monitor._query_has_credentials(  # pyright: ignore[reportPrivateUsage]
        "next=%25252525252561", depth=0
    )


def test_credential_scanners_fail_closed_after_decode_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_query_check = monitor._query_has_credentials
    monkeypatch.setattr(
        monitor,
        "_query_has_credentials",
        lambda *_args, **_kwargs: False,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(monitor, "unquote", lambda value: value + "x")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert monitor._nested_url_has_credentials("plain", depth=0)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(monitor, "_query_has_credentials", original_query_check)
    monkeypatch.setattr(monitor, "parse_qsl", lambda *_args, **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert monitor._query_has_credentials("safe=1", depth=0)  # pyright: ignore[reportPrivateUsage]


def test_nested_url_rejects_invalid_host_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parsed:
        @property
        def hostname(self) -> str:
            raise ValueError("invalid host")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

    monkeypatch.setattr(monitor, "urlsplit", lambda _value: Parsed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert monitor._nested_url_has_credentials("value", depth=0)  # pyright: ignore[reportPrivateUsage]


def test_public_url_rejects_host_controls_and_invalid_dns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parsed:
        scheme = "http"
        hostname = "bad\nhost"
        port = 80
        username = None
        password = None
        fragment = ""
        query = ""
        path = "/"

    monkeypatch.setattr(monitor, "urlsplit", lambda _value: Parsed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(MonitorError, match="control characters"):
        monitor._resolve_public_url("http://anything/")  # pyright: ignore[reportPrivateUsage]

    monkeypatch.undo()
    monkeypatch.setattr(
        monitor.socket,
        "getaddrinfo",
        lambda *_args: [(0, 0, 0, "", ("not-an-ip", 80))],  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    with pytest.raises(MonitorError, match="invalid address"):
        monitor._resolve_addresses("example.com", 80, None)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.undo()
    assert monitor._resolve_public_url("http://93.184.216.34/").port == 80  # pyright: ignore[reportPrivateUsage]


def test_regular_file_reader_rejects_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(MonitorError, match="positive"):
        monitor._read_regular_file_limited(  # pyright: ignore[reportPrivateUsage]
            tmp_path / "missing", 0, "--input"
        )


def test_content_type_parser_wraps_header_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidMessage:
        def __setitem__(self, _name: str, _value: str) -> None:
            raise ValueError("bad header")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

    monkeypatch.setattr(monitor, "Message", InvalidMessage)
    with pytest.raises(MonitorError, match="content type is invalid"):
        monitor._parse_content_type("bad")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("prefix", "charset"),
    [(b"\xef\xbb\xbf", None), (b"", "utf-8")],
    ids=["utf8-bom", "declared-utf8"],
)
def test_sniff_content_type_tolerates_multibyte_cut_at_prefix_bound(
    prefix: bytes, charset: str | None
) -> None:
    body = prefix + b"<!DOCTYPE html><html><body><p>" + "あ".encode() * 5_000
    with pytest.raises(UnicodeDecodeError):
        body[:8_192].decode("utf-8")  # prefix ends mid-character
    assert monitor._sniff_content_type(body, charset=charset) == "text/html"  # pyright: ignore[reportPrivateUsage]


def test_sniff_content_type_returns_plain_text_for_invalid_declared_encoding() -> None:
    assert monitor._sniff_content_type(b"\xff", charset="ascii") == "text/plain"  # pyright: ignore[reportPrivateUsage]


def test_connect_pinned_socket_rejects_non_ip_address() -> None:
    with pytest.raises(MonitorError, match="not an IP literal"):
        monitor._connect_pinned_socket(  # pyright: ignore[reportPrivateUsage]
            "example.com", 80, deadline=monitor.monotonic() + 1
        )


def test_wrap_tls_sets_deadline_and_runs_guarded_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Guarded:
        deadline: float | None = None

        def set_deadline(self, deadline: float) -> None:
            self.deadline = deadline

    class Context:
        minimum_version: object = None
        sslsocket_class: object = None

        def __init__(self) -> None:
            self.wrapped = Guarded()

        def wrap_socket(self, *_args: Any, **_kwargs: Any) -> Guarded:  # ruff: ignore[any-type]
            return self.wrapped

    context = Context()
    deadline = monitor.monotonic() + 1
    monkeypatch.setattr(monitor, "_DeadlineSSLSocket", Guarded)
    monkeypatch.setattr(monitor.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(
        monitor,
        "_do_handshake_with_deadline",
        lambda _sock, _deadline: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    result = monitor._wrap_tls(object(), "example.com", deadline)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result is context.wrapped
    assert result.deadline == deadline  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


@pytest.mark.parametrize("failure", ["tls", "http"], ids=["tls-error", "http-error"])
def test_open_connection_closes_socket_after_post_connect_errors(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class FakeSocket:
        closed = False

        def close(self) -> None:
            self.closed = True

    raw = FakeSocket()
    monkeypatch.setattr(
        monitor,
        "_connect_pinned_socket",
        lambda *_args, **_kwargs: raw,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    if failure == "tls":
        monkeypatch.setattr(
            monitor,
            "_wrap_tls",
            lambda *_args: (_ for _ in ()).throw(MonitorError("TLS rejected")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        )
        target = monitor._ResolvedTarget(  # pyright: ignore[reportPrivateUsage]
            "https://example.com", "https", "example.com", 443, ("93.184.216.34",)
        )
        expected_message = "TLS rejected"
    else:

        class BrokenConnection:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type]
                raise OSError("HTTP setup failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        monkeypatch.setattr(monitor.http.client, "HTTPConnection", BrokenConnection)
        target = monitor._ResolvedTarget(  # pyright: ignore[reportPrivateUsage]
            "http://example.com", "http", "example.com", 80, ("93.184.216.34",)
        )
        expected_message = "every validated address"
    with pytest.raises(MonitorError, match=expected_message):
        monitor._open_connection(target, deadline=monitor.monotonic() + 1)  # pyright: ignore[reportPrivateUsage]
    assert raw.closed


def test_open_response_closes_connection_when_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        sock = None
        closed = False

        def request(self, *_args: Any, **_kwargs: Any) -> None:  # ruff: ignore[any-type, no-self-use]
            raise OSError("request failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        monitor,
        "_open_connection",
        lambda *_args, **_kwargs: connection,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    target = monitor._ResolvedTarget(
        "http://example.com", "http", "example.com", 80, ()
    )
    with (
        pytest.raises(OSError, match="request failed"),
        monitor._open_response(target, deadline=monitor.monotonic() + 1),
    ):  # pyright: ignore[reportPrivateUsage]
        pass
    assert connection.closed


@pytest.mark.parametrize("response", [301, 302], ids=["too-many", "no-location"])
def test_redirect_validation_rejects_missing_location_or_excess(
    monkeypatch: pytest.MonkeyPatch, response: int
) -> None:
    class Redirect:
        status = response

        @staticmethod
        def getheader(_name: str) -> None:
            return None

    target = monitor._ResolvedTarget(
        "http://example.com", "http", "example.com", 80, ()
    )
    if response == 301:
        monkeypatch.setattr(monitor, "_DEFAULT_MAX_REDIRECTS", 1)
        redirect_count = 1
        message = "maximum redirects"
    else:
        redirect_count = 0
        message = "Location"
    with pytest.raises(MonitorError, match=message):
        monitor._redirect_target(  # pyright: ignore[reportPrivateUsage]
            Redirect(),  # pyright: ignore[reportArgumentType]
            target,
            redirect_count,
            monitor.monotonic() + 1,
        )


def test_fetch_document_validates_limits_wraps_io_errors_and_stops_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(MonitorError, match="positive"):
        monitor.fetch_document("http://93.184.216.34/", timeout=0, max_bytes=10)

    target = monitor._ResolvedTarget(
        "http://example.com", "http", "example.com", 80, ()
    )
    monkeypatch.setattr(
        monitor,
        "_resolve_public_url",
        lambda *_args, **_kwargs: target,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        monitor,
        "_fetch_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("network down")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    with pytest.raises(MonitorError, match="fetch failed: OSError"):
        monitor.fetch_document("http://example.com/", timeout=1, max_bytes=10)

    monkeypatch.setattr(monitor, "_DEFAULT_MAX_REDIRECTS", 0)
    monkeypatch.setattr(monitor, "_fetch_once", lambda *_args, **_kwargs: target)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(MonitorError, match="maximum redirects"):
        monitor.fetch_document("http://example.com/", timeout=1, max_bytes=10)


def test_feed_target_covers_nested_entries_identities_and_empty_links() -> None:
    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        10_000, preserve_structure=True, base_url="https://example.com/feed"
    )
    target.start("item", {})
    target.start("entry", {})
    target.start("id", {})
    target.data(" ")
    target.end("id")
    target.start("link", {"rel": "self", "href": "/self"})
    target.end("link")
    target.start("link", {})
    target.end("link")
    target.end("entry")
    target.end("item")
    target.end("stray")
    assert "item" in target.close()
    assert monitor._FeedTextTarget._identity_value("link", {"rel": "self"}) is None  # pyright: ignore[reportPrivateUsage]
    assert monitor._FeedTextTarget._identity_value("title", {}) is None  # pyright: ignore[reportPrivateUsage]

    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        10_000, preserve_structure=True, base_url="https://example.com/feed"
    )
    target.start("entry", {})
    target.start("id", {})
    target.data("entry-id")
    target.end("id")
    target.start("id", {})
    target.data("entry-id-2")
    target.end("id")
    target.end("entry")

    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        10_000, preserve_structure=True, base_url="https://example.com/feed"
    )
    target.start("entry", {})
    target.start("link", {})
    target.data("https://example.com/text-link")
    target.end("link")
    target.end("entry")

    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        10_000, preserve_structure=True, base_url="https://example.com/feed"
    )
    target.start("entry", {})
    target.start("guid", {})
    target.data("urn:uuid:1234")
    target.end("guid")
    target.end("entry")


@pytest.mark.parametrize(
    "failure", ["base-length", "base-stack"], ids=["base-length", "base-stack"]
)
def test_feed_target_enforces_base_url_stack_limits(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        100_000, preserve_structure=False, base_url="https://example.com/"
    )
    if failure == "base-length":
        monkeypatch.setattr(monitor, "_DEFAULT_MAX_XML_BASE_URL_CHARS", 30)
        target._base_url = "https://e/a/"
        attrs = {"xml:base": "12345678901234567890"}
        message = "base URL"
    else:
        monkeypatch.setattr(monitor, "_DEFAULT_MAX_XML_BASE_STACK_BYTES", 1)
        attrs = {}
        message = "base stack"
    with pytest.raises(MonitorError, match=message):
        target.start("root", attrs)


def test_feed_target_ignores_empty_xml_base() -> None:
    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        100_000, preserve_structure=False, base_url="https://example.com/"
    )
    target.start("root", {"xml:base": ""})
    assert target._base_stack == ["https://example.com/"]


def test_feed_identity_tokens_and_doctype_validation() -> None:
    assert (
        monitor._feed_identity_token(  # pyright: ignore[reportPrivateUsage]
            "id", "plain-id", "https://example.com/"
        )
        == "plain-id"
    )
    with pytest.raises(MonitorError, match="identity is invalid"):
        monitor._feed_identity_token(  # pyright: ignore[reportPrivateUsage]
            "id", "http://[::1", "https://example.com/"
        )
    with pytest.raises(MonitorError, match="DOCTYPE"):
        monitor._FeedTextTarget.doctype("rss", "", "")  # pyright: ignore[reportPrivateUsage]


def test_feed_parser_rejects_non_string_parser_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parser:
        def feed(self, _value: str) -> None:
            pass

        def close(self) -> object:  # ruff: ignore[no-self-use]
            return object()

    monkeypatch.setattr(monitor.ET, "XMLParser", lambda **_kwargs: Parser())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(MonitorError, match="invalid result"):
        monitor._normalize_feed(  # pyright: ignore[reportPrivateUsage]
            "<root/>",
            max_chars=100,
            preserve_structure=False,
            base_url="https://example.com/",
        )


def test_pdf_missing_dependency_errors_are_contextual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def fail_pypdf(name: str, *args: Any, **kwargs: Any) -> Any:  # ruff: ignore[any-type]
        if name == "pypdf":
            raise ImportError("not installed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_pypdf)
    with (
        pytest.raises(MonitorError, match="requires pypdf"),
        monitor._pypdf_output_limits(10),
    ):  # pyright: ignore[reportPrivateUsage]
        pass
    with pytest.raises(MonitorError, match="requires pypdf"):
        monitor._normalize_pdf_content(  # pyright: ignore[reportPrivateUsage]
            b"%PDF-1.7", max_decompressed_bytes=10, max_extracted_chars=10
        )


def test_pdf_value_and_page_tree_helpers_cover_tuple_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert monitor._pdf_values((1, 2)) == [1, 2]  # pyright: ignore[reportPrivateUsage]
    assert monitor._pdf_values(None) == []  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_PAGES", 1)
    pages = {"/Count": 0, "/Kids": [{}, {}]}  # pyright: ignore[reportUnknownVariableType]
    reader = type("Reader", (), {"trailer": {"/Root": {"/Pages": pages}}})()
    with pytest.raises(MonitorError, match="page count"):
        monitor._preflight_pdf_pages(reader)  # pyright: ignore[reportPrivateUsage]

    unreadable_page = object()
    reader = type(
        "Reader",
        (),
        {"trailer": {"/Root": {"/Pages": {"/Count": 0, "/Kids": [unreadable_page]}}}},
    )()
    assert monitor._preflight_pdf_pages(reader) == [2]  # pyright: ignore[reportPrivateUsage]


def test_pdf_form_xobject_traversal_covers_mapping_and_duplicate_forms() -> None:
    nested = {"/Subtype": "/Form", "/Resources": {"/XObject": {}}}  # pyright: ignore[reportUnknownVariableType]
    resources = {  # pyright: ignore[reportUnknownVariableType]
        "/XObject": {
            "/first": nested,
            "/same": nested,
            "/plain": object(),
        }
    }
    collected: list[Any] = []
    monitor._append_form_xobjects(  # pyright: ignore[reportPrivateUsage]
        resources, collected, set(), [0]
    )
    assert collected == [nested]
    monitor._append_form_xobjects(None, collected, set(), [0])  # pyright: ignore[reportPrivateUsage]
    monitor._append_form_xobjects({"/XObject": []}, collected, set(), [0])  # pyright: ignore[reportPrivateUsage]


def test_pdf_font_and_content_resource_shortcuts() -> None:
    assert (
        monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
            {},
            used=3,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
            font_count=[0],
            seen_cmaps=set(),
        )
        == 3
    )
    assert (
        monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
            {"/Resources": {"/Font": []}},
            used=3,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
            font_count=[0],
            seen_cmaps=set(),
        )
        == 3
    )
    assert (
        monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
            {"/Resources": {"/Font": {"unreadable": object()}}},
            used=3,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
            font_count=[0],
            seen_cmaps=set(),
        )
        == 3
    )
    assert (
        monitor._bound_page_content(  # pyright: ignore[reportPrivateUsage]
            {"/Contents": object()},
            used=0,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
        )
        == 0
    )


def test_pdf_font_maps_are_deduplicated_and_bounded() -> None:
    class CMap:
        def get_data(self) -> bytes:  # ruff: ignore[no-self-use]
            return b"map"

    cmap = CMap()
    page = {
        "/Resources": {
            "/Font": {"one": {"/ToUnicode": cmap}, "two": {"/ToUnicode": cmap}}
        }
    }
    limits: list[int] = []
    used = monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
        page,
        used=0,
        maximum=10,
        set_limit=limits.append,
        object_count=[0],
        font_count=[0],
        seen_cmaps=set(),
    )
    assert used == 3
    assert limits == [10]
    assert (
        monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
            {"/Resources": {"/Font": {"font": {"/ToUnicode": object()}}}},
            used=0,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
            font_count=[0],
            seen_cmaps=set(),
        )
        == 0
    )

    class InvalidCMap:
        def get_data(self) -> object:  # ruff: ignore[no-self-use]
            return "not bytes"

    with pytest.raises(MonitorError, match="font mappings"):
        monitor._bound_page_fonts(  # pyright: ignore[reportPrivateUsage]
            {"/Resources": {"/Font": {"font": {"/ToUnicode": InvalidCMap()}}}},
            used=0,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
            font_count=[0],
            seen_cmaps=set(),
        )


def test_pdf_content_streams_handle_cumulative_budget_and_invalid_data() -> None:
    class Stream:
        def __init__(self, result: object) -> None:
            self.result = result

        def get_data(self) -> object:
            return self.result

    limits: list[int] = []
    page = {"/Contents": [Stream(b"1"), Stream(b"")], "/Resources": None}
    assert (
        monitor._bound_page_content(  # pyright: ignore[reportPrivateUsage]
            page, used=3, maximum=4, set_limit=limits.append, object_count=[0]
        )
        == 4
    )
    assert limits == [1, 1]
    with pytest.raises(MonitorError, match="decompressed streams"):
        monitor._bound_page_content(  # pyright: ignore[reportPrivateUsage]
            {"/Contents": Stream("bad")},
            used=0,
            maximum=10,
            set_limit=lambda _value: None,
            object_count=[0],
        )


def test_pdf_annotation_filtering_handles_empty_and_non_uri_values() -> None:
    assert monitor._pdf_link_destination("mailto:a@example.com") == ""  # ruff: ignore[compare-to-empty-string]  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="invalid"):
        monitor._pdf_link_destination("https://[::1")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="relative link"):
        monitor._pdf_link_destination("relative")  # pyright: ignore[reportPrivateUsage]
    assert monitor._pdf_annotation_link(object(), [0]) == ""  # ruff: ignore[compare-to-empty-string]  # pyright: ignore[reportPrivateUsage]
    assert monitor._pdf_annotation_link({"/Subtype": "/Link"}, [0]) == ""  # ruff: ignore[compare-to-empty-string]  # pyright: ignore[reportPrivateUsage]
    assert (
        monitor._pdf_annotation_link(  # pyright: ignore[reportPrivateUsage]
            {"/Subtype": "/Link", "/A": {"/S": "/URI", "/URI": None}}, [0]
        )
        == ""  # ruff: ignore[compare-to-empty-string]
    )
    assert monitor._page_link_destinations({}, [0], [0]) == []  # pyright: ignore[reportPrivateUsage]
    assert (
        monitor._page_link_destinations(  # pyright: ignore[reportPrivateUsage]
            {"/Annots": [object()]}, [0], [0]
        )
        == []
    )


def test_pdf_page_text_checks_final_extracted_string_size() -> None:
    class NoVisitor:
        @staticmethod
        def extract_text(*, visitor_text: Any) -> str:  # ruff: ignore[any-type]
            del visitor_text
            return "too long"

    with pytest.raises(MonitorError, match="extracted text"):
        monitor._extract_page_text(NoVisitor(), extracted=0, maximum=2)  # pyright: ignore[reportPrivateUsage]


def test_pdf_encryption_and_materialized_page_count_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(MonitorError, match="encrypted"):
        monitor._normalize_pdf_content(  # pyright: ignore[reportPrivateUsage]
            output.getvalue(), max_decompressed_bytes=1_000, max_extracted_chars=1_000
        )

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr(monitor, "_DEFAULT_MAX_PDF_PAGES", 1)
    monkeypatch.setattr(monitor, "_preflight_pdf_pages", lambda _reader: [0])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(MonitorError, match="page count"):
        monitor._normalize_pdf_content(  # pyright: ignore[reportPrivateUsage]
            output.getvalue(), max_decompressed_bytes=1_000, max_extracted_chars=1_000
        )


def test_pdf_page_preflight_deduplicates_shared_nodes() -> None:
    page = {"/Kids": None}
    pages = {"/Count": 1, "/Kids": [page, page]}
    reader = type("Reader", (), {"trailer": {"/Root": {"/Pages": pages}}})()
    assert monitor._preflight_pdf_pages(reader) == [2]  # pyright: ignore[reportPrivateUsage]


def test_normalize_document_rejects_nonpositive_pdf_limits() -> None:
    with pytest.raises(MonitorError, match="positive"):
        monitor.normalize_document(
            Document(b"x", "https://example.com/", "text/plain"),
            max_pdf_decompressed_bytes=0,
        )


def test_utf8_prefix_returns_empty_when_first_character_exceeds_budget() -> None:
    assert not monitor._utf8_prefix("日本", 1)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("failure", ["empty", "oversized"], ids=["empty", "oversized"])
def test_run_rejects_empty_and_oversized_normalized_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    text = "" if failure == "empty" else "x" * (monitor._DEFAULT_MAX_SNAPSHOT_BYTES + 1)
    monkeypatch.setattr(monitor, "normalize_document", lambda *_args, **_kwargs: text)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    args = monitor._parser().parse_args(["--input", str(source)])
    message = "empty content" if failure == "empty" else "size limit"
    with pytest.raises(MonitorError, match=message):
        monitor.run(args)


def test_snapshot_write_cleans_temporary_file_without_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(monitor.os, "O_DIRECTORY", raising=False)
    destination = tmp_path / "snapshot.txt"
    original_replace = Path.replace

    def fail_replace(path: Path, target: Path) -> Path:
        if path.name.startswith(".snapshot.txt."):
            raise OSError("replace failed")  # ruff: ignore[raw-string-in-exception, raise-vanilla-args]
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        monitor._write_snapshot_atomic(destination, b"body")  # pyright: ignore[reportPrivateUsage]
    assert list(tmp_path.glob(".snapshot.txt.*.tmp")) == []
    monkeypatch.undo()
    monkeypatch.delattr(monitor.os, "O_DIRECTORY", raising=False)
    monitor._write_snapshot_atomic(destination, b"complete")  # pyright: ignore[reportPrivateUsage]
    assert destination.read_bytes() == b"complete"


def test_monitor_cli_success_failure_and_module_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    assert monitor.main(["--input", str(source)]) == 0
    assert '"status": "baseline"' in capsys.readouterr().out

    monkeypatch.setattr(
        monitor,
        "run",
        lambda _args: (_ for _ in ()).throw(MonitorError("failed")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    assert monitor.main(["--input", str(source)]) == 1
    assert '"error": "failed"' in capsys.readouterr().err

    monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "--input", str(source)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(monitor.__file__)), run_name="__main__")
    assert exit_info.value.code == 0


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (
            (
                '<base href="https://other.example/news/"><a href="item#part">item</a>'
                '<area href="/pdf"><form action="/submit">'
                '<button formaction="/do">go</button></form>'
                '<a href="mailto:a@example.com">mail</a>'
                '<a href="javascript:void(0)">js</a>'
                '<a href="https://example.com:bad/x">bad</a><a href="http://example.com:0/">zero</a>'
            ),
            "text/html",
            {"https://other.example/news/item", "https://other.example/pdf"},
        ),
        (
            (
                "<rss><channel><item><link>https://example.com/article#part</link>"
                '<description><![CDATA[<a href="/detail">detail</a>]]></description>'
                '<enclosure url="https://example.com/media"/></item></channel></rss>'
            ),
            "application/rss+xml",
            {"https://example.com/article", "https://example.com/detail"},
        ),
        (
            (
                '<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://example.org/">'
                '<link rel="self" href="feed"/><entry><id>x</id><link href="article"/>'
                '<link rel="enclosure" href="media"/><a href="detail"/></entry></feed>'
            ),
            "application/atom+xml",
            {"https://example.org/article", "https://example.org/detail"},
        ),
        (
            (
                '<rss xml:base="https://example.com/news/"><channel><item>'
                '<description>See &lt;a href="release"&gt;release'
                "&lt;/a&gt;</description>"
                "</item></channel></rss>"
            ),
            "application/rss+xml",
            {"https://example.com/news/release"},
        ),
        (
            (
                '<feed xmlns="http://www.w3.org/2005/Atom" '
                'xml:base="https://example.org/news/"><entry><id>x</id>'
                '<content type="html">See &lt;a href="release"&gt;release&lt;/a&gt;'
                "</content></entry></feed>"
            ),
            "application/atom+xml",
            {"https://example.org/news/release"},
        ),
        ("plain text", "text/plain", set[str]()),
    ],
    ids=[
        "html-navigation-only",
        "rss-embedded-html",
        "atom-base-and-rel",
        "rss-escaped-html",
        "atom-escaped-html",
        "text",
    ],
)
def test_normalization_collects_navigation_links_without_changing_text(
    body: str, content_type: str, expected: set[str]
) -> None:
    document = monitor.Document(body.encode(), "https://example.com/", content_type)
    links: dict[str, str] = {}
    text = monitor.normalize_document(document, links=links)
    assert text == monitor.normalize_document(document)
    assert set(links.values()) == expected
    assert all(f"sha256:{digest}" in text for digest in links)


def test_embedded_html_normalization_without_link_collection() -> None:
    assert "item" in monitor._normalize_html_fragment(
        '<a href="/item">item</a>', "https://example.com/"
    )


@pytest.mark.parametrize(
    "relation",
    [None, "nofollow", "noopener noreferrer"],
    ids=["no-rel", "nofollow", "noopener-noreferrer"],
)
def test_feed_xhtml_anchors_collect_navigation_for_all_relations(
    relation: str | None,
) -> None:
    relation_attr = "" if relation is None else f' rel="{relation}"'
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xml:base="https://example.org/news/"><entry><id>x</id>'
        '<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'
        f'<a href="release"{relation_attr}>Details</a>'
        "</div></content></entry></feed>"
    )
    destination = "https://example.org/news/release"
    digest = monitor.hashlib.sha256(destination.encode()).hexdigest()
    links = monitor.LinkCollection()

    normalized = monitor.normalize_document(
        Document(body.encode(), "https://example.org/feed", "application/atom+xml"),
        links=links,
    )

    assert set(links.values()) == {destination}
    assert f"sha256:{digest}" in normalized
    assert destination not in normalized


def test_feed_embedded_html_buffer_flushes_before_real_xml_children() -> None:
    document = Document(
        (
            b'<rss><channel><item><description xml:base="https://example.com/news/">'
            b'before &lt;a href="first"&gt;one&lt;/a&gt;<b>middle</b>'
            b'after &lt;a href="last"&gt;two&lt;/a&gt;'
            b"</description></item></channel></rss>"
        ),
        "https://example.com/feed",
        "application/rss+xml",
    )
    links = monitor.LinkCollection()

    normalized = monitor.normalize_document(document, links=links)

    assert set(links.values()) == {
        "https://example.com/news/first",
        "https://example.com/news/last",
    }
    tokens = ["before", "one", "[b:start]", "middle", "[b:end]", "after", "two"]
    positions = [normalized.index(token) for token in tokens]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "url",
    ["http:///", "https://[bad/", "http://example.com:0/", "mailto:a@example.com"],
)
def test_link_collection_ignores_invalid_or_non_http_destinations(url: str) -> None:
    links: dict[str, str] = {}
    monitor._collect_link(links, url)
    assert not links


@pytest.mark.parametrize(
    "track_omitted", [False, True], ids=["plain-dict", "link-collection"]
)
def test_link_collection_bounds_url_size(track_omitted: bool) -> None:
    destination = "https://example.com/" + "x" * 4096
    links: dict[str, str] = monitor.LinkCollection() if track_omitted else {}
    monitor._collect_link(links, destination)

    assert not links
    if isinstance(links, monitor.LinkCollection):
        assert links.omitted_hashes == {
            monitor.hashlib.sha256(destination.encode()).hexdigest()
        }


def test_feed_target_bounds_buffered_embedded_text() -> None:
    target = monitor._FeedTextTarget(  # pyright: ignore[reportPrivateUsage]
        100, preserve_structure=True, base_url="https://example.com/"
    )
    target.start("description", {})

    with pytest.raises(MonitorError, match="size limit"):
        target.data("x" * 100)


@pytest.mark.parametrize(
    ("content_type", "template"),
    [
        ("text/html", '<p>Readable</p><a href="{destination}">long</a>'),
        (
            "application/rss+xml",
            '<rss><channel><link href="{destination}"/></channel></rss>',
        ),
    ],
    ids=["html", "feed"],
)
def test_normalization_keeps_oversized_navigation_links_as_omitted_evidence(
    content_type: str, template: str
) -> None:
    destination = "https://example.com/" + "x" * 4096
    digest = monitor.hashlib.sha256(destination.encode()).hexdigest()
    links = monitor.LinkCollection()

    normalized = monitor.normalize_document(
        Document(
            template.format(destination=destination).encode(),
            "https://example.com/feed",
            content_type,
        ),
        links=links,
    )

    assert not links
    assert links.omitted_hashes == {digest}
    assert f"sha256:{digest}" in normalized
    assert destination not in normalized


def test_pdf_parser_failure_is_reported_as_monitor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdf  # ruff: ignore[import-outside-top-level]

    def fail_reader(*_args: object, **_kwargs: object) -> None:
        message = "malformed PDF"
        raise ValueError(message)

    monkeypatch.setattr(pypdf, "PdfReader", fail_reader)
    with pytest.raises(MonitorError, match="PDF could not be normalized safely"):
        normalize_document(
            Document(b"%PDF-1.7", "https://example.com/file.pdf", "application/pdf")
        )
