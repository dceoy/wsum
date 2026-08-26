"""Tests for the compact monitor helper."""

from __future__ import annotations

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


def test_private_literal_ip_is_rejected() -> None:
    with pytest.raises(MonitorError, match="public IP"):
        validate_public_url("http://127.0.0.1/")


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
    ],
    ids=["utf8-bom", "utf16-bom", "html-meta", "http-charset"],
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
            ("https://example.com/new", "Update"),
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

    assert all(value in normalized for value in expected)


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


def test_normalize_pdf_bounds_expansion_and_restores_pypdf_limits() -> None:
    pytest.importorskip("pypdf")
    from pypdf import filters  # ruff: ignore[import-outside-top-level]

    original = filters.ZLIB_MAX_OUTPUT_LENGTH
    pdf = _make_pdf(b"q\n" * 2_000, compressed=True)

    with pytest.raises(MonitorError, match="PDF"):
        normalize_document(
            Document(pdf, "https://example.com/file.pdf", "application/pdf"),
            max_pdf_decompressed_bytes=128,
            max_pdf_extracted_chars=1_000,
        )

    assert original == filters.ZLIB_MAX_OUTPUT_LENGTH


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
