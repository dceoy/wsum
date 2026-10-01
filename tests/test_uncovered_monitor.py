"""Focused boundary tests for monitor branches not reached by the main workflows."""

from __future__ import annotations

import builtins
import ipaddress
import runpy
import ssl
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import monitor
import pytest
from monitor import Document, MonitorError

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

    def recv(self, *_args: Any, **_kwargs: Any) -> bytes:
        return b"data"

    def recv_into(self, buffer: bytearray, *_args: Any, **_kwargs: Any) -> int:
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
    assert socket.timeouts[-1] == 4.0

    socket.settimeout(2.0)
    socket.set_deadline(15.0)
    if method == "recv":
        socket.recv(4)
    else:
        socket.recv_into(bytearray(4))
    assert socket.timeouts[-1] == 2.0

    socket.set_deadline(10.0)
    with pytest.raises(TimeoutError, match="fetch deadline"):
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


def test_html_extractor_caps_destination_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor, "_MAX_HTML_DESTINATIONS", 0)
    parser = monitor._TextExtractor("https://example.com/")
    with pytest.raises(MonitorError, match="too many monitored destinations"):
        parser.feed('<a href="/one">one</a>')


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
    monkeypatch.setattr(monitor, "urlsplit", lambda _url: parsed)
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
def test_connect_pinned_socket_closes_or_returns_socket(
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
                raise OSError("connection refused")

        def getpeername(self) -> tuple[str, int]:
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

        def connect(self, address: tuple[Any, ...]) -> None:
            captured.append(address)

        def getpeername(self) -> tuple[str, int]:
            return family, 443

        def close(self) -> None:
            raise AssertionError("successful socket must remain open")

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
        monitor._do_handshake_with_deadline(fake, monitor.monotonic() + 2)  # pyright: ignore[reportPrivateUsage]
        assert fake.calls == 2
    else:
        with pytest.raises(TimeoutError, match=message):
            monitor._do_handshake_with_deadline(fake, monitor.monotonic() + 2)  # pyright: ignore[reportPrivateUsage]
        assert fake.calls == 1
    assert fake.blocking is False
    assert fake.timeout == 3.0


def test_wrap_tls_rejects_unguarded_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class Guarded:
        pass

    class Context:
        minimum_version: object = None
        sslsocket_class: object = None

        def wrap_socket(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

    monkeypatch.setattr(monitor, "_DeadlineSSLSocket", Guarded)
    monkeypatch.setattr(monitor.ssl, "create_default_context", Context)
    with pytest.raises(MonitorError, match="unguarded socket"):
        monitor._wrap_tls(object(), "example.com", monitor.monotonic() + 1)  # pyright: ignore[reportPrivateUsage]


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
                raise TimeoutError("late")
            if error == "monitor":
                raise MonitorError("invalid")
            raise OSError("down")
        sock = FakeSocket()
        created.append(sock)
        return sock

    class FakeConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
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
        lambda _value: type("Parts", (), {"path": "/bad\r\npath", "query": ""})(),
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
            "getheader": lambda _self, name, default=None: headers.get(name, default),
        },
    )()
    with pytest.raises(MonitorError, match=message):
        monitor._validate_response(response, max_bytes=10)  # pyright: ignore[reportPrivateUsage]


def test_read_response_supports_read_fallback_closed_response_and_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self) -> None:
            self.parts = [b"abc", b""]

        def read(self, _size: int) -> bytes:
            return self.parts.pop(0)

        def isclosed(self) -> bool:
            return False

    class Transport:
        timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    transport = Transport()
    monkeypatch.setattr(monitor, "monotonic", lambda: 10.0)
    assert (
        monitor._read_response_limited(  # pyright: ignore[reportPrivateUsage]
            Reader(), 10, deadline=12.0, sock=transport
        )
        == b"abc"
    )
    assert transport.timeouts == [2.0, 2.0]

    class Closed:
        @staticmethod
        def read(_size: int) -> bytes:
            raise AssertionError("closed responses must not be read")

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
    class Page(dict[str, object]):
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
        assert monitor._pdf_link_destination(uri) == ""  # pyright: ignore[reportPrivateUsage]
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
        def extract_text(self, *, visitor_text: Any) -> str:
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
    assert monitor._utf8_prefix("abc", 0) == ""  # pyright: ignore[reportPrivateUsage]
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
            raise OSError("cannot replace")
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
    monkeypatch.setattr(pool._capacity, "acquire", lambda **_kwargs: False)
    with pytest.raises(TimeoutError, match="DNS resolution"):
        pool.resolve(lambda: 1, (), {}, 1.0)

    pool = monitor._ResolverPool(1)  # pyright: ignore[reportPrivateUsage]

    class RacingLock:
        def __enter__(self) -> None:
            pool._workers.append(object())

        def __exit__(self, *_args: object) -> None:
            pass

    pool._start_lock = RacingLock()  # pyright: ignore[reportPrivateUsage]
    pool._ensure_started()  # pyright: ignore[reportPrivateUsage]

    class FakeCapacity:
        def acquire(self, *, timeout: float) -> bool:
            return timeout > 0

        def release(self) -> None:
            pass

    class FakeQueue:
        def put(self, _job: object) -> None:
            pass

    class FakeEvent:
        def wait(self, _timeout: float) -> bool:
            return False

        def set(self) -> None:
            pass

    class FakeJob:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
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
        monitor, "_query_has_credentials", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(monitor, "unquote", lambda value: value + "x")
    assert monitor._nested_url_has_credentials("plain", depth=0)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(monitor, "_query_has_credentials", original_query_check)
    monkeypatch.setattr(monitor, "parse_qsl", lambda *_args, **_kwargs: [])
    assert monitor._query_has_credentials("safe=1", depth=0)  # pyright: ignore[reportPrivateUsage]


def test_nested_url_rejects_invalid_host_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parsed:
        @property
        def hostname(self) -> str:
            raise ValueError("invalid host")

    monkeypatch.setattr(monitor, "urlsplit", lambda _value: Parsed())
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

    monkeypatch.setattr(monitor, "urlsplit", lambda _value: Parsed())
    with pytest.raises(MonitorError, match="control characters"):
        monitor._resolve_public_url("http://anything/")  # pyright: ignore[reportPrivateUsage]

    monkeypatch.undo()
    monkeypatch.setattr(
        monitor.socket,
        "getaddrinfo",
        lambda *_args: [(0, 0, 0, "", ("not-an-ip", 80))],
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
            raise ValueError("bad header")

    monkeypatch.setattr(monitor, "Message", InvalidMessage)
    with pytest.raises(MonitorError, match="content type is invalid"):
        monitor._parse_content_type("bad")  # pyright: ignore[reportPrivateUsage]


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

        def wrap_socket(self, *_args: Any, **_kwargs: Any) -> Guarded:
            return self.wrapped

    context = Context()
    deadline = monitor.monotonic() + 1
    monkeypatch.setattr(monitor, "_DeadlineSSLSocket", Guarded)
    monkeypatch.setattr(monitor.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(
        monitor, "_do_handshake_with_deadline", lambda _sock, _deadline: None
    )
    result = monitor._wrap_tls(object(), "example.com", deadline)  # pyright: ignore[reportPrivateUsage]
    assert result is context.wrapped
    assert result.deadline == deadline


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
        monitor, "_connect_pinned_socket", lambda *_args, **_kwargs: raw
    )
    if failure == "tls":
        monkeypatch.setattr(
            monitor,
            "_wrap_tls",
            lambda *_args: (_ for _ in ()).throw(MonitorError("TLS rejected")),
        )
        target = monitor._ResolvedTarget(  # pyright: ignore[reportPrivateUsage]
            "https://example.com", "https", "example.com", 443, ("93.184.216.34",)
        )
        expected_message = "TLS rejected"
    else:

        class BrokenConnection:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise OSError("HTTP setup failed")

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

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("request failed")

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        monitor, "_open_connection", lambda *_args, **_kwargs: connection
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
            Redirect(), target, redirect_count, monitor.monotonic() + 1
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
        monitor, "_resolve_public_url", lambda *_args, **_kwargs: target
    )
    monkeypatch.setattr(
        monitor,
        "_fetch_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("network down")),
    )
    with pytest.raises(MonitorError, match="fetch failed: OSError"):
        monitor.fetch_document("http://example.com/", timeout=1, max_bytes=10)

    monkeypatch.setattr(monitor, "_DEFAULT_MAX_REDIRECTS", 0)
    monkeypatch.setattr(monitor, "_fetch_once", lambda *_args, **_kwargs: target)
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

        def close(self) -> object:
            return object()

    monkeypatch.setattr(monitor.ET, "XMLParser", lambda **_kwargs: Parser())
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

    def fail_pypdf(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pypdf":
            raise ImportError("not installed")
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
    pages = {"/Count": 0, "/Kids": [{}, {}]}
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
    nested = {"/Subtype": "/Form", "/Resources": {"/XObject": {}}}
    resources = {
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
        def get_data(self) -> bytes:
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
        def get_data(self) -> object:
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
    assert monitor._pdf_link_destination("mailto:a@example.com") == ""  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="invalid"):
        monitor._pdf_link_destination("https://[::1")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(MonitorError, match="relative link"):
        monitor._pdf_link_destination("relative")  # pyright: ignore[reportPrivateUsage]
    assert monitor._pdf_annotation_link(object(), [0]) == ""  # pyright: ignore[reportPrivateUsage]
    assert monitor._pdf_annotation_link({"/Subtype": "/Link"}, [0]) == ""  # pyright: ignore[reportPrivateUsage]
    assert (
        monitor._pdf_annotation_link(  # pyright: ignore[reportPrivateUsage]
            {"/Subtype": "/Link", "/A": {"/S": "/URI", "/URI": None}}, [0]
        )
        == ""
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
        def extract_text(*, visitor_text: Any) -> str:
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
    monkeypatch.setattr(monitor, "_preflight_pdf_pages", lambda _reader: [0])
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
    monkeypatch.setattr(monitor, "normalize_document", lambda *_args, **_kwargs: text)
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
            raise OSError("replace failed")
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
        monitor, "run", lambda _args: (_ for _ in ()).throw(MonitorError("failed"))
    )
    assert monitor.main(["--input", str(source)]) == 1
    assert '"error": "failed"' in capsys.readouterr().err

    monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "--input", str(source)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(monitor.__file__)), run_name="__main__")
    assert exit_info.value.code == 0
