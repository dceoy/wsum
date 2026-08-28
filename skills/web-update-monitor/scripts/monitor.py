"""Fetch or read a document, normalize it, and compare it with a snapshot."""

# MonitorError and TimeoutError messages are intentionally contextual at their
# call sites so the CLI can report the failed operation precisely.
# ruff: file-ignore[raise-vanilla-args]

from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import io
import ipaddress
import json
import os
import queue
import re
import select
import socket
import ssl
import stat
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET  # ruff: ignore[suspicious-xml-etree-import]
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from difflib import unified_diff
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_DIFF_LINES = 200
_DEFAULT_MAX_DIFF_BYTES = 65_536
_DEFAULT_MAX_DIFF_COMPLEXITY = 4_000_000
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_REDIRECTS = 10
_DEFAULT_MAX_PDF_DECOMPRESSED_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_PDF_EXTRACTED_CHARS = 10 * 1024 * 1024
_DEFAULT_MAX_PDF_PAGES = 1_000
_DEFAULT_MAX_PDF_OBJECTS = 10_000
_DEFAULT_MAX_PDF_ANNOTATIONS = 500
_DEFAULT_MAX_PDF_FONTS = 500
_DEFAULT_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024
_DEFAULT_MAX_XML_CHARS = 10 * 1024 * 1024
_DEFAULT_MAX_XML_DEPTH = 200
_DEFAULT_MAX_XML_BASE_URL_CHARS = 4_096
_DEFAULT_MAX_XML_BASE_STACK_BYTES = 1_000_000
_RESOLVER_WORKERS = 4
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_HTML_DESTINATION_ATTRS = {
    "a": ("href",),
    "area": ("href",),
    "button": ("formaction",),
    "form": ("action",),
    "input": ("formaction",),
}
_HTML_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_HTML_LINE_BREAK_TAGS = {"br"}
_MAX_HTML_DESTINATIONS = 500
_SENSITIVE_QUERY_NAMES = frozenset({
    "access-token",
    "api-key",
    "api-token",
    "apikey",
    "auth",
    "auth-token",
    "authorization",
    "awsaccesskeyid",
    "credential",
    "id-token",
    "key",
    "oauth-token",
    "password",
    "refresh-token",
    "secret",
    "sig",
    "signature",
    "subscription-key",
    "token",
    "x-api-key",
})
_SENSITIVE_QUERY_SUFFIXES = (
    "credential",
    "secret",
    "signature",
    "security-token",
    "session-token",
)
_QUERY_SEPARATOR_RE = re.compile(r"[\s._-]+")
_MAX_NESTED_URL_DEPTH = 5
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_FEED_CONTENT_TYPES = {"application/atom+xml", "application/rss+xml"}
_XML_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_SUPPORTED_TEXT_ENCODINGS = {
    "ascii",
    "big5",
    "cp932",
    "cp1252",
    "euc_jp",
    "euc_kr",
    "gb18030",
    "gbk",
    "iso2022_jp",
    "iso8859-1",
    "shift_jis",
    "utf-16",
    "utf-16-be",
    "utf-16-le",
    "utf-32",
    "utf-32-be",
    "utf-32-le",
    "utf-8",
    "utf-8-sig",
}
_BOM_ENCODINGS = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
_HTML_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})",
    re.IGNORECASE,
)
_HTML_CONTENT_TYPE_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*\bcontent\s*=\s*[\"'][^\"']*?\bcharset\s*=\s*"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})",
    re.IGNORECASE,
)
_XML_ENCODING_RE = re.compile(
    rb"<\?xml\b[^>]*\bencoding\s*=\s*[\"']"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})[\"']",
    re.IGNORECASE,
)
_PYPDF_LIMIT_NAMES = (
    "MAX_DECLARED_STREAM_LENGTH",
    "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    "JBIG2_MAX_OUTPUT_LENGTH",
    "LZW_MAX_OUTPUT_LENGTH",
    "RUN_LENGTH_MAX_OUTPUT_LENGTH",
    "ZLIB_MAX_OUTPUT_LENGTH",
    "FLATE_MAX_BUFFER_SIZE",
    "ZLIB_MAX_RECOVERY_INPUT_LENGTH",
)
_NON_PUBLIC_IPV6_NETWORKS = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("2001:10::/28"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fec0::/10"),
    ipaddress.ip_network("ff00::/8"),
)
_NON_PUBLIC_IPV4_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)
_PUBLIC_SPECIAL_IPV4_ADDRESSES = frozenset({
    ipaddress.ip_address("192.0.0.9"),
    ipaddress.ip_address("192.0.0.10"),
})
_PDF_LIMIT_LOCK = threading.Lock()


class MonitorError(RuntimeError):
    """Expected input, network, or normalization failure."""


@dataclass(frozen=True, slots=True)
class Document:
    """Fetched or local document bytes plus source metadata."""

    body: bytes
    source_url: str
    content_type: str
    charset: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """One URL resolved to the public addresses used for its request."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


class _ResolverJob:
    """A bounded asynchronous getaddrinfo call."""

    def __init__(
        self,
        resolver: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self.resolver = resolver
        self.args = args
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class _ResolverPool:
    """Run at most four DNS calls without creating unbounded queued work."""

    def __init__(self, worker_count: int) -> None:
        self._capacity = threading.BoundedSemaphore(worker_count)
        self._jobs: queue.SimpleQueue[_ResolverJob] = queue.SimpleQueue()
        self._workers: list[threading.Thread] = []
        self._worker_count = worker_count
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._workers:
            return
        with self._start_lock:
            if self._workers:
                return
            for index in range(self._worker_count):
                worker = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"wsum-resolver-{index}",
                )
                worker.start()
                self._workers.append(worker)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job.result = job.resolver(*job.args, **job.kwargs)
            except BaseException as exc:
                job.error = exc
            finally:
                job.done.set()
                self._capacity.release()

    def resolve(
        self,
        resolver: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float,
    ) -> Any:
        """Resolve one hostname while preserving the caller's deadline."""
        if timeout <= 0:
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        self._ensure_started()
        deadline = monotonic() + timeout
        if not self._capacity.acquire(timeout=timeout):
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        job = _ResolverJob(resolver, args, kwargs)
        self._jobs.put(job)
        remaining = deadline - monotonic()
        if remaining <= 0 or not job.done.wait(remaining):
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        if job.error is not None:
            raise job.error
        return job.result


_RESOLVER_POOL = _ResolverPool(_RESOLVER_WORKERS)


class _DeadlineTrackingMixin:
    """Clamp every socket receive to the remaining total fetch deadline."""

    _deadline = float("inf")

    def set_deadline(self, deadline: float) -> None:
        """Set the absolute deadline used by the receive guards."""
        self._deadline = deadline

    def _clamp_to_deadline(self) -> None:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("fetch deadline exceeded")
        current = self.gettimeout()  # type: ignore[attr-defined]
        if current is None or current > remaining:
            self.settimeout(remaining)  # type: ignore[attr-defined]

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        """Receive bytes only while time remains in the fetch budget."""
        self._clamp_to_deadline()
        return super().recv_into(*args, **kwargs)  # type: ignore[misc]

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        """Receive bytes only while time remains in the fetch budget."""
        self._clamp_to_deadline()
        return super().recv(*args, **kwargs)  # type: ignore[misc]


class _DeadlineSocket(_DeadlineTrackingMixin, socket.socket):
    pass


class _DeadlineSSLSocket(_DeadlineTrackingMixin, ssl.SSLSocket):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._document_url = source_url
        self._base_url = source_url
        self._base_href_seen = False
        self._skip_depth = 0
        self._destination_count = 0
        self.parts: list[str] = []

    def handle_starttag(  # ruff: ignore[complex-structure]
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HTML_LINE_BREAK_TAGS:
            self.parts.append("\n")
        if tag in _HTML_BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        values = {name.lower(): value for name, value in attrs if name and value}
        if tag == "base":
            base_href = values.get("href")
            if base_href and not self._base_href_seen:
                self._base_url = urljoin(self._document_url, base_href.strip())
                self._base_href_seen = True
            return
        destinations = _HTML_DESTINATION_ATTRS.get(tag, ())
        if not destinations:
            return
        for name in destinations:
            value = values.get(name)
            if not value:
                continue
            self._destination_count += 1
            if self._destination_count > _MAX_HTML_DESTINATIONS:
                raise MonitorError("HTML has too many monitored destinations")
            destination = urljoin(self._base_url, value.strip())
            if _destination_has_credentials(destination):
                raise MonitorError("HTML destination contains credentials")
            digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
            self.parts.append(f"\n[{tag}:{name}:sha256:{digest}]\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


_EMBEDDED_HTML_RE = re.compile(r"</?[A-Za-z][^>]*>")


def _normalize_html_fragment(value: str, base_url: str) -> str:
    """Normalize HTML embedded in an XML text field."""
    if not _EMBEDDED_HTML_RE.search(value):
        return value
    parser = _TextExtractor(base_url)
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("fetch deadline exceeded")
    return remaining


def _is_sensitive_query_name(name: str) -> bool:
    normalized = _QUERY_SEPARATOR_RE.sub("-", name.strip().lower())
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
        _SENSITIVE_QUERY_SUFFIXES
    )


def _is_webhook_credential_path(host: str, path: str) -> bool:
    normalized_host = host.rstrip(".").lower()
    return (normalized_host == "hooks.slack.com" and path.startswith("/services/")) or (
        normalized_host in {"discord.com", "discordapp.com"}
        and "/api/webhooks/" in path
    )


def _nested_url_has_credentials(value: str, *, depth: int) -> bool:
    if depth > _MAX_NESTED_URL_DEPTH:
        return True
    candidate = value
    for _ in range(_MAX_NESTED_URL_DEPTH + 1):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return True
        try:
            host = parsed.hostname or ""
        except ValueError:
            return True
        has_credentials = (
            parsed.username is not None
            or parsed.password is not None
            or _is_webhook_credential_path(host, unquote(parsed.path))
            or _query_has_credentials(parsed.query, depth=depth + 1)
            or _query_has_credentials(parsed.fragment, depth=depth + 1)
        )
        if has_credentials:
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def _query_has_credentials(query: str, *, depth: int) -> bool:
    # Treat the legacy semicolon separator as equivalent to '&'.
    for name, value in parse_qsl(query.replace(";", "&"), keep_blank_values=True):
        if _is_sensitive_query_name(name):
            return True
        if value and _nested_url_has_credentials(value, depth=depth):
            return True
    return False


def _resolve_public_url(url: str, *, deadline: float | None = None) -> _ResolvedTarget:
    """Validate a URL and return the exact public addresses for one request."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise MonitorError("source must be a valid HTTP(S) URL") from exc
    if scheme not in {"http", "https"} or not host:
        raise MonitorError("source must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MonitorError("embedded URL credentials are not allowed")
    if parsed.fragment:
        raise MonitorError("URL fragments are not allowed")
    if _query_has_credentials(parsed.query, depth=0):
        raise MonitorError("credential-bearing URL query is not allowed")
    if any(character in host for character in "\r\n"):
        raise MonitorError("source host contains control characters")
    if _is_webhook_credential_path(host, unquote(parsed.path)):
        raise MonitorError("webhook credential URLs are not allowed")
    if port == 0:
        raise MonitorError("source port must not be zero")
    port = port if port is not None else (443 if scheme == "https" else 80)
    addresses = _resolve_addresses(host, port, deadline)
    if deadline is not None:
        _remaining(deadline)
    return _ResolvedTarget(url, scheme, host, port, addresses)


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Apply a stable public-unicast policy across supported Python versions."""
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 4:
        if any(address in network for network in _NON_PUBLIC_IPV4_NETWORKS):
            return address in _PUBLIC_SPECIAL_IPV4_ADDRESSES
        return True
    return not any(address in network for network in _NON_PUBLIC_IPV6_NETWORKS)


def _resolve_addresses(host: str, port: int, deadline: float | None) -> tuple[str, ...]:
    """Resolve one host and reject every non-public result."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            if deadline is None:
                infos = socket.getaddrinfo(host, port)
            else:
                infos = _RESOLVER_POOL.resolve(
                    socket.getaddrinfo,
                    (host, port),
                    {},
                    _remaining(deadline),
                )
        except socket.gaierror as exc:
            raise MonitorError("hostname resolution failed") from exc
        addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))

    if not addresses:
        raise MonitorError("hostname resolved to no addresses")
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as exc:
        raise MonitorError("hostname resolved to an invalid address") from exc
    if any(not _is_public_unicast(address) for address in parsed_addresses):
        raise MonitorError("source must resolve only to public IP addresses")
    return addresses


def _validate_public_url(url: str) -> None:  # pyright: ignore[reportUnusedFunction]
    """Validate a public URL without exposing its resolution details."""
    _resolve_public_url(url)


def validate_public_url(url: str) -> None:
    """Validate a public URL without exposing its resolution details."""
    _validate_public_url(url)


def _connect_pinned_socket(
    address: str, port: int, *, deadline: float
) -> socket.socket:
    """Connect to a previously validated numeric address without DNS."""
    remaining = _remaining(deadline)
    try:
        literal = ipaddress.ip_address(address)
    except ValueError as exc:
        raise MonitorError("validated address is not an IP literal") from exc
    family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
    sockaddr: tuple[Any, ...]
    if family == socket.AF_INET6:
        sockaddr = (str(literal), port, 0, 0)
    else:
        sockaddr = (str(literal), port)
    sock = _DeadlineSocket(family, socket.SOCK_STREAM)
    sock.set_deadline(deadline)
    sock.settimeout(remaining)
    try:
        sock.connect(sockaddr)
        peer = ipaddress.ip_address(sock.getpeername()[0])
    except BaseException:
        sock.close()
        raise
    if peer != literal:
        sock.close()
        raise MonitorError("connected peer is not the validated address")
    return sock


def _do_handshake_with_deadline(sock: ssl.SSLSocket, deadline: float) -> None:
    """Complete a non-blocking TLS handshake within the fetch deadline."""
    original_timeout = sock.gettimeout()
    sock.setblocking(False)  # ruff: ignore[boolean-positional-value-in-call]
    try:
        while True:
            remaining = _remaining(deadline)
            try:
                sock.do_handshake()
            except ssl.SSLWantReadError:
                readable, _, _ = select.select([sock], [], [], remaining)
                if not readable:
                    raise TimeoutError(
                        "TLS handshake exceeded the fetch deadline"
                    ) from None
            except ssl.SSLWantWriteError:
                _, writable, _ = select.select([], [sock], [], remaining)
                if not writable:
                    raise TimeoutError(
                        "TLS handshake exceeded the fetch deadline"
                    ) from None
            else:
                return
    finally:
        sock.settimeout(original_timeout)


def _wrap_tls(sock: socket.socket, host: str, deadline: float) -> _DeadlineSSLSocket:
    """Wrap a pinned socket while preserving hostname verification and SNI."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.sslsocket_class = _DeadlineSSLSocket
    wrapped = context.wrap_socket(
        sock,
        server_hostname=host,
        do_handshake_on_connect=False,
    )
    if not isinstance(wrapped, _DeadlineSSLSocket):
        raise MonitorError("TLS context returned an unguarded socket")
    wrapped.set_deadline(deadline)
    _do_handshake_with_deadline(wrapped, deadline)
    return wrapped


def _open_connection(
    target: _ResolvedTarget, *, deadline: float
) -> http.client.HTTPConnection:
    """Open an HTTP connection whose socket is pinned to a validated address."""
    last_error: BaseException | None = None
    for address in target.addresses:
        raw_socket: socket.socket | None = None
        try:
            raw_socket = _connect_pinned_socket(address, target.port, deadline=deadline)
            active_socket = (
                _wrap_tls(raw_socket, target.host, deadline)
                if target.scheme == "https"
                else raw_socket
            )
            connection = http.client.HTTPConnection(
                target.host,
                port=target.port,
                timeout=_remaining(deadline),
            )
            connection.sock = active_socket
        except MonitorError:
            if raw_socket is not None:
                raw_socket.close()
            raise
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            last_error = exc
            if raw_socket is not None:
                raw_socket.close()
        else:
            return connection
    if isinstance(last_error, TimeoutError):
        raise last_error
    raise MonitorError("connection failed for every validated address") from last_error


def _request_target(url: str) -> str:
    """Build a safe origin-form HTTP request target from a validated URL."""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if any(character in path for character in "\r\n"):
        raise MonitorError("request target contains control characters")
    return path


def _host_header(target: _ResolvedTarget) -> str:
    display_host = f"[{target.host}]" if ":" in target.host else target.host
    default_port = 443 if target.scheme == "https" else 80
    return (
        display_host if target.port == default_port else f"{display_host}:{target.port}"
    )


def _read_response_limited(
    response: Any,
    max_bytes: int,
    *,
    deadline: float | None = None,
    sock: socket.socket | None = None,
) -> bytes:
    """Read a response in small chunks under byte and total-time limits."""
    reader = getattr(response, "read1", None) or getattr(response, "read", None)
    if reader is None:
        raise MonitorError("HTTP response is not readable")
    if max_bytes <= 0:
        raise MonitorError("--max-bytes must be positive")
    active_deadline = deadline if deadline is not None else float("inf")
    chunks: list[bytes] = []
    size = 0
    while True:
        is_closed = getattr(response, "isclosed", None)
        if callable(is_closed) and is_closed():
            break
        remaining = (
            _remaining(active_deadline) if deadline is not None else float("inf")
        )
        if sock is not None and deadline is not None:
            sock.settimeout(remaining)
        chunk = reader(min(65_536, max_bytes - size + 1))
        if not isinstance(chunk, bytes):
            raise MonitorError("HTTP response did not return bytes")
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:
            raise MonitorError("document exceeds --max-bytes")
    if deadline is not None:
        _remaining(deadline)
    return b"".join(chunks)


def _response_content_type(
    response: http.client.HTTPResponse,
) -> tuple[str, str | None]:
    raw_value = response.getheader("Content-Type", "") or ""
    message = Message()
    message["Content-Type"] = raw_value
    content_type = message.get_content_type().lower()
    raw_charset = message.get_param("charset")
    charset = raw_charset if isinstance(raw_charset, str) else None
    return content_type, charset


@contextmanager
def _open_response(
    target: _ResolvedTarget, *, deadline: float
) -> Generator[tuple[http.client.HTTPResponse, socket.socket | None], None, None]:
    """Open one response while enforcing the shared deadline and cleanup.

    Yields:
        The HTTP response and the pinned transport socket used to read it.
    """
    connection = _open_connection(target, deadline=deadline)
    response: http.client.HTTPResponse | None = None
    try:
        transport_socket = connection.sock
        if transport_socket is not None:
            transport_socket.settimeout(_remaining(deadline))
        connection.request(
            "GET",
            _request_target(target.url),
            headers={
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Host": _host_header(target),
                "User-Agent": "wsum/0.1 (+https://github.com/dceoy/wsum)",
            },
        )
        _remaining(deadline)
        response = connection.getresponse()
        yield response, transport_socket
    finally:
        if response is not None:
            response.close()
        connection.close()


def _redirect_target(
    response: http.client.HTTPResponse,
    target: _ResolvedTarget,
    redirect_count: int,
    deadline: float,
) -> _ResolvedTarget | None:
    if response.status not in {301, 302, 303, 307, 308}:
        return None
    if redirect_count >= _DEFAULT_MAX_REDIRECTS:
        raise MonitorError("maximum redirects exceeded")
    location = response.getheader("Location")
    if not location:
        raise MonitorError("redirect response has no Location header")
    return _resolve_public_url(urljoin(target.url, location), deadline=deadline)


def _validate_response(
    response: http.client.HTTPResponse, max_bytes: int
) -> int | None:
    if not 200 <= response.status < 300:
        message = f"fetch failed: HTTP {response.status}"
        raise MonitorError(message)
    content_encoding = (response.getheader("Content-Encoding", "") or "").strip()
    if content_encoding.lower() not in {"", "identity"}:
        raise MonitorError("compressed HTTP content encoding is not supported")
    declared_length = response.getheader("Content-Length")
    if declared_length is None:
        return None
    try:
        length = int(declared_length)
    except ValueError as exc:
        raise MonitorError("HTTP Content-Length is invalid") from exc
    if length < 0:
        raise MonitorError("HTTP Content-Length is invalid")
    if length > max_bytes:
        raise MonitorError("document exceeds --max-bytes")
    return length


def _fetch_once(
    target: _ResolvedTarget,
    *,
    deadline: float,
    max_bytes: int,
    redirect_count: int,
) -> Document | _ResolvedTarget:
    """Fetch one response or return its already-validated redirect target."""
    with _open_response(target, deadline=deadline) as (response, transport_socket):
        redirected = _redirect_target(response, target, redirect_count, deadline)
        if redirected is not None:
            return redirected
        declared_length = _validate_response(response, max_bytes)
        body = _read_response_limited(
            response,
            max_bytes,
            deadline=deadline,
            sock=transport_socket,
        )
        if declared_length is not None and len(body) != declared_length:
            raise MonitorError("HTTP response body does not match Content-Length")
        content_type, charset = _response_content_type(response)
        return Document(body, target.url, content_type, charset)


def fetch_document(url: str, *, timeout: float, max_bytes: int) -> Document:
    """Fetch one public HTTP(S) document with pinned, bounded connections."""
    if timeout <= 0 or max_bytes <= 0:
        raise MonitorError("numeric limits must be positive")
    deadline = monotonic() + timeout
    target = _resolve_public_url(url, deadline=deadline)
    for redirect_count in range(_DEFAULT_MAX_REDIRECTS + 1):
        try:
            result = _fetch_once(
                target,
                deadline=deadline,
                max_bytes=max_bytes,
                redirect_count=redirect_count,
            )
        except MonitorError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            message = f"fetch failed: {type(exc).__name__}"
            raise MonitorError(message) from exc
        if isinstance(result, _ResolvedTarget):
            target = result
            continue
        return result
    raise MonitorError("maximum redirects exceeded")


def read_document(
    path: Path,
    *,
    source_url: str,
    content_type: str,
    max_bytes: int,
    deadline: float | None = None,
) -> Document:
    """Read a local document supplied by the caller."""
    body = _read_regular_file_limited(path, max_bytes, "--input")
    if source_url:
        _resolve_public_url(source_url, deadline=deadline)
    inferred, charset = _parse_content_type(content_type or _guess_content_type(path))
    return Document(body, source_url or path.resolve().as_uri(), inferred, charset)


def _read_regular_file_limited(path: Path, max_bytes: int, label: str) -> bytes:
    """Read a non-symlink regular file without exceeding a byte budget."""
    if max_bytes <= 0:
        raise MonitorError("numeric limits must be positive")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        message = f"{label} must be a regular file"
        raise MonitorError(message) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            message = f"{label} must be a regular file"
            raise MonitorError(message)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) > max_bytes:
        raise MonitorError("document exceeds --max-bytes")
    return body


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".html", ".htm"}:
        return "text/html"
    if suffix == ".rss":
        return "application/rss+xml"
    if suffix == ".atom":
        return "application/atom+xml"
    if suffix == ".xml":
        return "application/xml"
    return "text/plain"


def _normalize_whitespace(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def _parse_content_type(value: str) -> tuple[str, str | None]:
    message = Message()
    try:
        message["Content-Type"] = value
    except ValueError as exc:
        raise MonitorError("content type is invalid") from exc
    raw_charset = message.get_param("charset")
    charset = raw_charset if isinstance(raw_charset, str) else None
    return message.get_content_type().lower(), charset


def _sniff_text_content_type(sample: str) -> str:
    """Detect structured content from a decoded bounded prefix."""
    if re.search(r"<(?:\w+:)?(?:rss|feed|rdf)\b", sample):
        return "application/rss+xml"
    xml_declaration = re.match(r"<\?xml\b[^>]*\?>", sample)
    if xml_declaration is not None:
        after_declaration = sample[xml_declaration.end() :].lstrip()
        if re.match(r"<(?:!doctype\s+html|(?:\w+:)?html)\b", after_declaration):
            return "text/html"
        return "application/xml"
    if re.search(
        r"<(?:!doctype\s+html|html|head|body|main|article|section|div|"
        r"h[1-6]|p|table|script|a|area|form|button|input)\b",
        sample,
    ):
        return "text/html"
    return "text/plain"


def _sniff_content_type(body: bytes, *, charset: str | None = None) -> str:
    """Detect the structural document type from a bounded byte prefix."""
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    encoding = _bom_encoding(body)
    if encoding is None and charset:
        try:
            encoding = _encoding_name(charset)
        except MonitorError:
            encoding = None
    sample_bytes = body[:8_192]
    if encoding in {"utf-16-le", "utf-16-be"}:
        sample_bytes = sample_bytes[: len(sample_bytes) - len(sample_bytes) % 2]
    elif encoding in {"utf-32-le", "utf-32-be"}:
        sample_bytes = sample_bytes[: len(sample_bytes) - len(sample_bytes) % 4]
    try:
        sample = (
            sample_bytes.decode(encoding, errors="strict")
            if encoding is not None
            else sample_bytes.decode("latin-1", errors="strict")
        )
    except UnicodeDecodeError:
        return "text/plain"
    return _sniff_text_content_type(sample.lstrip().lower())


def _normalization_content_type(document: Document) -> str:
    """Cross-check declared MIME type with bytes and select the parser type."""
    declared = document.content_type
    sniffed = _sniff_content_type(document.body, charset=document.charset)
    supported = {
        "application/pdf",
        "text/plain",
        *_XML_CONTENT_TYPES,
        *_HTML_CONTENT_TYPES,
    }
    if declared not in supported:
        raise MonitorError("declared content type is unsupported")
    if declared == "text/plain":
        return sniffed
    if declared in _XML_CONTENT_TYPES and sniffed in _XML_CONTENT_TYPES:
        return sniffed
    if declared in _HTML_CONTENT_TYPES and sniffed in _HTML_CONTENT_TYPES:
        return sniffed
    if declared == sniffed:
        return sniffed
    raise MonitorError("declared content type does not match detected document type")


def _encoding_name(value: str) -> str:
    candidate = value.strip().strip("\"'")
    if not candidate or len(candidate) > 64:
        raise MonitorError("document encoding is unsupported")
    try:
        name = codecs.lookup(candidate).name
    except LookupError as exc:
        message = f"document encoding is unsupported: {candidate}"
        raise MonitorError(message) from exc
    if name not in _SUPPORTED_TEXT_ENCODINGS:
        message = f"document encoding is unsupported: {candidate}"
        raise MonitorError(message)
    return name


def _bom_encoding(body: bytes) -> str | None:
    for marker, encoding in _BOM_ENCODINGS:
        if body.startswith(marker):
            return encoding
    return None


def _in_document_encoding(body: bytes, content_type: str) -> str | None:
    prefix = body[:4_096]
    match: re.Match[bytes] | None
    if content_type in _XML_CONTENT_TYPES:
        match = _XML_ENCODING_RE.search(prefix)
    elif content_type in _HTML_CONTENT_TYPES:
        match = _HTML_CHARSET_RE.search(prefix)
        if match is None:
            match = _HTML_CONTENT_TYPE_CHARSET_RE.search(prefix)
    else:
        match = None
    return match.group(1).decode("ascii") if match else None


def _decode_document(document: Document) -> str:
    encoding = _bom_encoding(document.body)
    if encoding is None and document.charset:
        encoding = _encoding_name(document.charset)
    if encoding is None:
        declared = _in_document_encoding(document.body, document.content_type)
        encoding = _encoding_name(declared) if declared else "utf-8"
    try:
        return document.body.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        message = f"document could not be decoded as {encoding}"
        raise MonitorError(message) from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class _FeedTextTarget:
    """Collect bounded feed text, identities, and link destinations."""

    _ENTRY_TAGS = frozenset({"entry", "item"})
    _IDENTITY_TAGS = frozenset({"guid", "id", "link"})
    _EMBEDDED_TEXT_TAGS = frozenset({"content", "description", "encoded", "summary"})
    _ENTRY_MARKER = "\x00wsum-entry-placeholder\x00"

    def __init__(
        self, max_chars: int, *, preserve_structure: bool, base_url: str
    ) -> None:
        if len(base_url.encode("utf-8")) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
            raise MonitorError("XML base URL exceeds the size limit")
        self._max_chars = max_chars
        self._preserve_structure = preserve_structure
        self._base_url = base_url
        self._base_stack: list[str] = []
        self._base_stack_bytes = 0
        self._tag_stack: list[str] = []
        self._text_destination_stack: list[list[str] | None] = []
        self._identity_frames: list[tuple[str, list[str], str | None]] = []
        self._entry_parts: list[str] | None = None
        self._entry_level = 0
        self._entry_identity: dict[str, str] = {}
        self._entries: list[tuple[str, str]] = []
        self._entry_marker_added = False
        self._size = 0
        self._parts: list[str] = []

    def _append(self, value: str) -> None:
        self._size += len(value)
        if self._size > self._max_chars:
            raise MonitorError("XML extracted text exceeds the size limit")
        if self._entry_parts is not None:
            self._entry_parts.append(value)
        else:
            self._parts.append(value)

    def _start_entry(self) -> None:
        if self._entry_parts is not None:
            return
        self._entry_parts = []
        self._entry_level = 0
        self._entry_identity = {}
        if not self._entry_marker_added:
            self._parts.append(self._ENTRY_MARKER)
            self._entry_marker_added = True

    @staticmethod
    def _identity_value(local_tag: str, attrs: dict[str, str]) -> str | None:
        if local_tag == "link":
            relation = next(
                (
                    value.strip().lower()
                    for name, value in attrs.items()
                    if _local_name(name) == "rel" and value
                ),
                "alternate",
            )
            if relation != "alternate":
                return None
            return next(
                (
                    value.strip()
                    for name, value in attrs.items()
                    if _local_name(name) == "href" and value
                ),
                None,
            )
        return None

    def _push_base(self, local_tag: str, attrs: dict[str, str]) -> None:
        if len(self._base_stack) >= _DEFAULT_MAX_XML_DEPTH:
            raise MonitorError("XML nesting exceeds the size limit")
        base_url = self._base_stack[-1] if self._base_stack else self._base_url
        for name, value in attrs.items():
            if (
                name == "{http://www.w3.org/XML/1998/namespace}base"
                or name.lower() == "xml:base"
            ):
                if value:
                    base_value = value.strip()
                    if len(base_value) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
                        raise MonitorError("XML base URL exceeds the size limit")
                    base_url = urljoin(base_url, base_value)
                break
        base_size = len(base_url.encode("utf-8"))
        if base_size > _DEFAULT_MAX_XML_BASE_URL_CHARS:
            raise MonitorError("XML base URL exceeds the size limit")
        if self._base_stack_bytes + base_size > _DEFAULT_MAX_XML_BASE_STACK_BYTES:
            raise MonitorError("XML base stack exceeds the size limit")
        self._base_stack.append(base_url)
        self._base_stack_bytes += base_size
        self._tag_stack.append(local_tag)

    def _append_start(self, local_tag: str, attrs: dict[str, str]) -> None:
        destinations = sorted(
            (
                (_local_name(name), value)
                for name, value in attrs.items()
                if _local_name(name) in {"href", "url"} and value
            )
        )
        is_text_destination = (
            self._preserve_structure and local_tag == "link" and not destinations
        )
        self._text_destination_stack.append([] if is_text_destination else None)
        if not self._preserve_structure:
            return
        self._append(f"\n[{local_tag}:start]\n")
        if local_tag not in {"a", "enclosure", "link"}:
            return
        for name, value in destinations:
            self._append_destination(local_tag, name, value)

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        local_tag = _local_name(tag)
        if self._preserve_structure and local_tag in self._ENTRY_TAGS:
            self._start_entry()
        if self._entry_parts is not None:
            self._entry_level += 1
        self._push_base(local_tag, attrs)
        identity_value = self._identity_value(local_tag, attrs)
        if self._entry_parts is not None and local_tag in self._IDENTITY_TAGS:
            self._identity_frames.append((local_tag, [], identity_value))
        self._append_start(local_tag, attrs)

    def _finish_identity(self, field: str, value: str) -> None:
        if not value:
            return
        if field not in self._entry_identity:
            self._entry_identity[field] = value
        if field in {"guid", "id"}:
            self._append(_feed_identity_token(field, value, self._base_url))

    def end(self, tag: str) -> None:
        local_tag = _local_name(tag)
        text_destination = (
            self._text_destination_stack.pop() if self._text_destination_stack else None
        )
        if text_destination is not None:
            value = "".join(text_destination).strip()
            if value:
                self._append_destination("link", "href", value)

        if self._identity_frames and self._identity_frames[-1][0] == local_tag:
            field, values, attribute_value = self._identity_frames.pop()
            value = attribute_value or "".join(values).strip()
            self._finish_identity(field, value)
        if self._preserve_structure:
            self._append(f"\n[{local_tag}:end]\n")

        if self._entry_parts is not None:
            self._entry_level -= 1
            if self._entry_level == 0:
                entry_text = "".join(self._entry_parts)
                identity = next(
                    (
                        self._entry_identity[field]
                        for field in ("guid", "id", "link")
                        if self._entry_identity.get(field)
                    ),
                    entry_text,
                )
                key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                self._entries.append((key, entry_text))
                self._entry_parts = None
                self._entry_identity = {}
                self._identity_frames.clear()

        if self._tag_stack:
            self._tag_stack.pop()
        if self._base_stack:
            self._base_stack_bytes -= len(self._base_stack.pop().encode("utf-8"))

    def data(self, data: str) -> None:
        if self._identity_frames:
            for _, values, _ in self._identity_frames:
                values.append(data)
            if self._identity_frames[-1][0] in {"guid", "id"}:
                return
        if (
            self._text_destination_stack
            and self._text_destination_stack[-1] is not None
        ):
            self._text_destination_stack[-1].append(data)  # type: ignore[union-attr]
            return
        if (
            self._preserve_structure
            and self._tag_stack
            and self._tag_stack[-1] in self._EMBEDDED_TEXT_TAGS
        ):
            base_url = self._base_stack[-1] if self._base_stack else self._base_url
            data = _normalize_html_fragment(data, base_url)
        self._append(data)

    def _append_destination(self, tag: str, name: str, value: str) -> None:
        base_url = self._base_stack[-1] if self._base_stack else self._base_url
        destination = urljoin(base_url, value.strip())
        if _destination_has_credentials(destination):
            raise MonitorError("feed destination contains credentials")
        digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
        self._append(f"[{tag}:{name}:sha256:{digest}]\n")

    def close(self) -> str:
        entries = "\n".join(entry_text for _, entry_text in sorted(self._entries))
        return "".join(self._parts).replace(self._ENTRY_MARKER, entries)

    @staticmethod
    def doctype(name: str, public_id: str, system_id: str) -> None:
        del name, public_id, system_id
        raise MonitorError("DOCTYPE declarations are not supported")


def _destination_has_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        return True
    return (
        parsed.username is not None
        or parsed.password is not None
        or _query_has_credentials(parsed.fragment, depth=0)
        or _is_webhook_credential_path(host, unquote(parsed.path))
        or _query_has_credentials(parsed.query, depth=0)
    )


def _feed_identity_token(field: str, value: str, base_url: str) -> str:
    """Return bounded feed identity text without exposing URI-shaped values."""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise MonitorError("feed identity is invalid") from exc
    if not parsed.scheme and not parsed.netloc:
        return value
    identity = value
    if parsed.scheme.lower() in {"http", "https"} or parsed.netloc:
        identity = urljoin(base_url, value)
        if _destination_has_credentials(identity):
            raise MonitorError("feed identity contains credentials")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"[{field}:sha256:{digest}]"


def _normalize_feed(
    text: str, *, max_chars: int, preserve_structure: bool, base_url: str
) -> str:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise MonitorError("DOCTYPE and entity declarations are not supported")
    target = _FeedTextTarget(
        max_chars, preserve_structure=preserve_structure, base_url=base_url
    )
    parser = ET.XMLParser(target=target)  # ruff: ignore[suspicious-xml-element-tree-usage]
    try:
        parser.feed(text)
        parsed = parser.close()
    except MonitorError:
        raise
    except ET.ParseError as exc:
        raise MonitorError("XML document could not be parsed") from exc
    if not isinstance(parsed, str):
        raise MonitorError("XML parser returned an invalid result")
    return _normalize_whitespace(parsed)


@contextmanager
def _pypdf_output_limits(limit: int) -> Generator[Callable[[int], None], None, None]:
    """Temporarily cap pypdf stream expansion while holding a process lock.

    Yields:
        A setter for reducing the remaining output budget during extraction.
    """
    try:
        from pypdf import filters  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        raise MonitorError("PDF normalization requires pypdf") from exc
    with _PDF_LIMIT_LOCK:
        previous = {
            name: getattr(filters, name)
            for name in _PYPDF_LIMIT_NAMES
            if hasattr(filters, name)
        }

        def set_limit(value: int) -> None:
            for name in previous:
                setattr(filters, name, value)

        original_decompress = filters.decompress

        def bounded_decompress(data: bytes) -> bytes:
            if len(data) > limit:
                raise MonitorError("PDF Flate recovery input exceeds the size limit")
            return original_decompress(data)

        filters.decompress = bounded_decompress
        set_limit(limit)
        try:
            yield set_limit
        finally:
            filters.decompress = original_decompress
            for name, value in previous.items():
                setattr(filters, name, value)


def _resolve_pdf_object(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _pdf_values(value: Any) -> list[Any]:
    resolved = _resolve_pdf_object(value)
    if resolved is None:
        return []
    if isinstance(resolved, list):
        return cast("list[Any]", resolved).copy()
    if isinstance(resolved, tuple):
        return list(cast("tuple[Any, ...]", resolved))
    return [resolved]


def _consume_pdf_object(object_count: list[int]) -> None:
    object_count[0] += 1
    if object_count[0] > _DEFAULT_MAX_PDF_OBJECTS:
        raise MonitorError("PDF object traversal exceeds the size limit")


def _preflight_pdf_pages(reader: Any) -> list[int]:
    """Bound page-tree traversal before pypdf materializes the page list."""
    object_count = [0]
    trailer = getattr(reader, "trailer", {})
    root = _resolve_pdf_object(trailer.get("/Root"))
    pages_root = _resolve_pdf_object(root.get("/Pages"))
    declared_count = pages_root.get("/Count")
    if isinstance(declared_count, int) and declared_count > _DEFAULT_MAX_PDF_PAGES:
        raise MonitorError("PDF page count exceeds the size limit")
    seen: set[int] = set()
    page_count = 0

    def visit(node: Any) -> None:
        nonlocal page_count
        resolved = _resolve_pdf_object(node)
        identity = id(resolved)
        if identity in seen:
            return
        seen.add(identity)
        _consume_pdf_object(object_count)
        get_value = getattr(resolved, "get", None)
        if not callable(get_value):
            return
        kids = _resolve_pdf_object(get_value("/Kids"))
        if isinstance(kids, (list, tuple)):
            typed_kids = cast("list[Any] | tuple[Any, ...]", kids)
            for child in typed_kids:
                visit(child)
        else:
            page_count += 1
            if page_count > _DEFAULT_MAX_PDF_PAGES:
                raise MonitorError("PDF page count exceeds the size limit")

    visit(pages_root)
    return object_count


def _append_form_xobjects(
    resources: Any,
    streams: list[Any],
    seen: set[int],
    object_count: list[int],
) -> None:
    resources = _resolve_pdf_object(resources)
    get_value = getattr(resources, "get", None)
    if not callable(get_value):
        return
    xobjects = _resolve_pdf_object(get_value("/XObject"))
    if not isinstance(xobjects, Mapping):
        return
    typed_xobjects = cast("Mapping[object, Any]", xobjects)
    for candidate in typed_xobjects.values():
        _consume_pdf_object(object_count)
        form = _resolve_pdf_object(candidate)
        form_get = getattr(form, "get", None)
        if not callable(form_get) or form_get("/Subtype") != "/Form":
            continue
        identity = id(form)
        if identity in seen:
            continue
        seen.add(identity)
        streams.append(form)
        _append_form_xobjects(form_get("/Resources"), streams, seen, object_count)


def _page_content_streams(page: Any, object_count: list[int]) -> list[Any]:
    streams: list[Any] = []
    seen: set[int] = set()
    for value in _pdf_values(page.get("/Contents")):
        _consume_pdf_object(object_count)
        stream = _resolve_pdf_object(value)
        if id(stream) not in seen:
            seen.add(id(stream))
            streams.append(stream)
    _append_form_xobjects(page.get("/Resources"), streams, seen, object_count)
    return streams


def _bound_page_fonts(
    page: Any,
    *,
    used: int,
    maximum: int,
    set_limit: Callable[[int], None],
    object_count: list[int],
    font_count: list[int],
    seen_cmaps: set[int],
) -> int:
    """Charge page fonts and their ToUnicode streams to the PDF budget."""
    resources = _resolve_pdf_object(page.get("/Resources"))
    get_value = getattr(resources, "get", None)
    if not callable(get_value):
        return used
    fonts = _resolve_pdf_object(get_value("/Font"))
    if not isinstance(fonts, Mapping):
        return used
    typed_fonts = cast("Mapping[object, Any]", fonts)
    for candidate in typed_fonts.values():
        font_count[0] += 1
        if font_count[0] > _DEFAULT_MAX_PDF_FONTS:
            raise MonitorError("PDF font resources exceed the size limit")
        _consume_pdf_object(object_count)
        font = _resolve_pdf_object(candidate)
        font_get = getattr(font, "get", None)
        if not callable(font_get):
            continue
        cmap = _resolve_pdf_object(font_get("/ToUnicode"))
        if cmap is None or id(cmap) in seen_cmaps:
            continue
        seen_cmaps.add(id(cmap))
        _consume_pdf_object(object_count)
        get_data = getattr(cmap, "get_data", None)
        if not callable(get_data):
            continue
        remaining = maximum - used
        set_limit(max(1, remaining))
        data = get_data()
        if not isinstance(data, bytes) or len(data) > remaining:
            raise MonitorError("PDF font mappings exceed the size limit")
        used += len(data)
    return used


def _bound_page_content(
    page: Any,
    *,
    used: int,
    maximum: int,
    set_limit: Callable[[int], None],
    object_count: list[int],
) -> int:
    """Expand each page content stream under a cumulative decompression budget."""
    for stream in _page_content_streams(page, object_count):
        get_data = getattr(stream, "get_data", None)
        if not callable(get_data):
            continue
        remaining = maximum - used
        if remaining <= 0:
            set_limit(1)
        else:
            set_limit(remaining)
        data = get_data()
        if not isinstance(data, bytes) or len(data) > remaining:
            raise MonitorError("PDF decompressed streams exceed the size limit")
        used += len(data)
    return used


def _pdf_link_destination(uri: str) -> str:
    """Return an opaque identity for one absolute HTTP(S) PDF link."""
    try:
        parsed = urlsplit(uri.strip())
    except ValueError as exc:
        raise MonitorError("PDF link destination is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        if parsed.scheme:
            return ""
        raise MonitorError("PDF relative link destinations are unsupported")
    if _destination_has_credentials(uri):
        raise MonitorError("PDF link destination contains credentials")
    return hashlib.sha256(uri.strip().encode("utf-8")).hexdigest()


def _pdf_annotation_link(annotation: Any, object_count: list[int]) -> str:
    _consume_pdf_object(object_count)
    obj = _resolve_pdf_object(annotation)
    if not isinstance(obj, Mapping):
        return ""
    typed_obj = cast("Mapping[object, Any]", obj)
    if typed_obj.get("/Subtype") != "/Link":
        return ""
    action = _resolve_pdf_object(typed_obj.get("/A"))
    if not isinstance(action, Mapping):
        return ""
    typed_action = cast("Mapping[object, Any]", action)
    if typed_action.get("/S") != "/URI":
        return ""
    uri = _resolve_pdf_object(typed_action.get("/URI"))
    return _pdf_link_destination(uri) if isinstance(uri, str) else ""


def _page_link_destinations(
    page: Any, object_count: list[int], annotation_count: list[int]
) -> list[str]:
    """Extract bounded opaque identities from page URI annotations."""
    annotations = _resolve_pdf_object(page.get("/Annots"))
    if annotations is None:
        return []
    if not isinstance(annotations, (list, tuple)):
        raise MonitorError("PDF annotations have an invalid structure")
    typed_annotations = cast("list[Any] | tuple[Any, ...]", annotations)
    if len(typed_annotations) > _DEFAULT_MAX_PDF_ANNOTATIONS - annotation_count[0]:
        raise MonitorError("PDF annotations exceed the size limit")
    lines: list[str] = []
    for annotation in typed_annotations:
        annotation_count[0] += 1
        digest = _pdf_annotation_link(annotation, object_count)
        if digest:
            lines.append(f"[link:sha256:{digest}]")
    return lines


def _extract_page_text(page: Any, *, extracted: int, maximum: int) -> tuple[str, int]:
    page_chars = 0

    def visitor_text(text: str, *args: Any) -> None:
        del args
        nonlocal page_chars
        page_chars += len(text)
        if extracted + page_chars > maximum:
            raise MonitorError("PDF extracted text exceeds the size limit")

    page_text = page.extract_text(visitor_text=visitor_text) or ""
    page_size = max(page_chars, len(page_text))
    if extracted + page_size > maximum:
        raise MonitorError("PDF extracted text exceeds the size limit")
    return page_text, page_size


def _normalize_pdf_content(
    body: bytes, *, max_decompressed_bytes: int, max_extracted_chars: int
) -> str:
    try:
        from pypdf import PdfReader  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        raise MonitorError("PDF normalization requires pypdf") from exc

    with _pypdf_output_limits(max_decompressed_bytes) as set_limit:
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise MonitorError("encrypted PDFs are not supported")
        object_count = _preflight_pdf_pages(reader)
        fragments: list[str] = []
        decompressed = 0
        extracted = 0
        font_count = [0]
        seen_cmaps: set[int] = set()
        annotation_count = [0]
        for page_number, page in enumerate(reader.pages, start=1):
            if page_number > _DEFAULT_MAX_PDF_PAGES:
                raise MonitorError("PDF page count exceeds the size limit")
            decompressed = _bound_page_content(
                page,
                used=decompressed,
                maximum=max_decompressed_bytes,
                set_limit=set_limit,
                object_count=object_count,
            )
            decompressed = _bound_page_fonts(
                page,
                used=decompressed,
                maximum=max_decompressed_bytes,
                set_limit=set_limit,
                object_count=object_count,
                font_count=font_count,
                seen_cmaps=seen_cmaps,
            )
            link_destinations = _page_link_destinations(
                page, object_count, annotation_count
            )
            page_text, page_size = _extract_page_text(
                page, extracted=extracted, maximum=max_extracted_chars
            )
            extracted += page_size
            fragments.append(page_text)
            fragments.extend(link_destinations)
        return _normalize_whitespace("\n".join(fragments))


def _normalize_pdf(
    body: bytes, *, max_decompressed_bytes: int, max_extracted_chars: int
) -> str:
    try:
        return _normalize_pdf_content(
            body,
            max_decompressed_bytes=max_decompressed_bytes,
            max_extracted_chars=max_extracted_chars,
        )
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError("PDF could not be normalized safely") from exc


def normalize_document(
    document: Document,
    *,
    max_pdf_decompressed_bytes: int = _DEFAULT_MAX_PDF_DECOMPRESSED_BYTES,
    max_pdf_extracted_chars: int = _DEFAULT_MAX_PDF_EXTRACTED_CHARS,
) -> str:
    """Convert HTML/XML, text, or PDF bytes into stable plain text."""
    if max_pdf_decompressed_bytes <= 0 or max_pdf_extracted_chars <= 0:
        raise MonitorError("numeric limits must be positive")
    content_type = _normalization_content_type(document)
    normalized_document = Document(
        document.body,
        document.source_url,
        content_type,
        document.charset,
    )
    if content_type == "application/pdf":
        return _normalize_pdf(
            document.body,
            max_decompressed_bytes=max_pdf_decompressed_bytes,
            max_extracted_chars=max_pdf_extracted_chars,
        )

    text = _decode_document(normalized_document)
    if content_type in _XML_CONTENT_TYPES:
        return _normalize_feed(
            text,
            max_chars=_DEFAULT_MAX_XML_CHARS,
            preserve_structure=content_type in _FEED_CONTENT_TYPES,
            base_url=document.source_url,
        )
    if content_type in _HTML_CONTENT_TYPES:
        parser = _TextExtractor(document.source_url)
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    return _normalize_whitespace(text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return the longest prefix of ``value`` that fits a UTF-8 byte limit."""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    prefix = encoded[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return ""


def _bounded_diff(
    current: str,
    previous: str,
    *,
    max_diff_lines: int,
    max_diff_bytes: int,
) -> tuple[str, bool]:
    """Render a unified diff under output and pre-computation complexity bounds."""
    previous_line_count = _line_count(previous)
    current_line_count = _line_count(current)
    if previous_line_count * current_line_count > _DEFAULT_MAX_DIFF_COMPLEXITY:
        marker = "\n".join(
            [
                "--- previous",
                "+++ current",
                "@@ diff omitted: complexity limit exceeded @@",
            ][:max_diff_lines]
        )
        return _utf8_prefix(marker, max_diff_bytes), True
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()

    parts: list[str] = []
    size = 0
    for emitted_lines, line in enumerate(
        unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    ):
        if emitted_lines >= max_diff_lines:
            return "".join(parts), True
        separator = "" if not parts else "\n"
        candidate = f"{separator}{line}"
        candidate_size = len(candidate.encode("utf-8"))
        remaining = max_diff_bytes - size
        if candidate_size > remaining:
            return "".join(parts) + _utf8_prefix(candidate, remaining), True
        parts.append(candidate)
        size += candidate_size
    return "".join(parts), False


def _line_count(value: str) -> int:
    """Count lines using the same boundary characters as ``str.splitlines``."""
    count = 0
    last_end = 0
    for match in _LINE_BREAK_RE.finditer(value):
        count += 1
        last_end = match.end()
    if value and last_end != len(value):
        count += 1
    return count


def compare_text(
    current: str,
    previous: str | None,
    *,
    max_diff_lines: int,
    max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
) -> dict[str, object]:
    """Return baseline/unchanged/changed metadata and a bounded unified diff."""
    if max_diff_lines <= 0 or max_diff_bytes <= 0:
        raise MonitorError("numeric limits must be positive")
    current_hash = _sha256(current)
    if previous is None:
        return {
            "status": "baseline",
            "sha256": current_hash,
            "previous_sha256": "",
            "diff": "",
            "diff_truncated": False,
        }

    previous_hash = _sha256(previous)
    if previous_hash == current_hash:
        return {
            "status": "unchanged",
            "sha256": current_hash,
            "previous_sha256": previous_hash,
            "diff": "",
            "diff_truncated": False,
        }

    bounded, truncated = _bounded_diff(
        current,
        previous,
        max_diff_lines=max_diff_lines,
        max_diff_bytes=max_diff_bytes,
    )
    return {
        "status": "changed",
        "sha256": current_hash,
        "previous_sha256": previous_hash,
        "diff": bounded,
        "diff_truncated": truncated,
    }


def _write_snapshot_atomic(path: Path, body: bytes) -> None:
    """Replace a snapshot without following an existing unsafe destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        temporary_fd, temporary_path = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(temporary_fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary_path).replace(path)
        temporary_path = None
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                Path(temporary_path).unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch/read, normalize, and diff one monitored document."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="public HTTP(S) URL to fetch")
    source.add_argument("--input", type=Path, help="local/rendered document")
    parser.add_argument(
        "--source-url", default="", help="canonical URL when using --input"
    )
    parser.add_argument("--content-type", default="", help="override input MIME type")
    parser.add_argument("--previous", type=Path, help="previous normalized snapshot")
    parser.add_argument("--output", type=Path, help="write current normalized snapshot")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)
    parser.add_argument("--max-diff-lines", type=int, default=_DEFAULT_MAX_DIFF_LINES)
    parser.add_argument("--max-diff-bytes", type=int, default=_DEFAULT_MAX_DIFF_BYTES)
    parser.add_argument(
        "--max-pdf-decompressed-bytes",
        type=int,
        default=_DEFAULT_MAX_PDF_DECOMPRESSED_BYTES,
    )
    parser.add_argument(
        "--max-pdf-extracted-chars",
        type=int,
        default=_DEFAULT_MAX_PDF_EXTRACTED_CHARS,
    )
    return parser


def _limits_are_positive(*values: float) -> bool:
    return all(value > 0 for value in values)


def run(args: argparse.Namespace) -> dict[str, object]:
    """Execute one comparison and return its JSON-ready result."""
    max_diff_bytes = getattr(args, "max_diff_bytes", _DEFAULT_MAX_DIFF_BYTES)
    max_pdf_decompressed_bytes = getattr(
        args, "max_pdf_decompressed_bytes", _DEFAULT_MAX_PDF_DECOMPRESSED_BYTES
    )
    max_pdf_extracted_chars = getattr(
        args, "max_pdf_extracted_chars", _DEFAULT_MAX_PDF_EXTRACTED_CHARS
    )
    if not _limits_are_positive(
        args.timeout,
        args.max_bytes,
        args.max_diff_lines,
        max_diff_bytes,
        max_pdf_decompressed_bytes,
        max_pdf_extracted_chars,
    ):
        raise MonitorError("numeric limits must be positive")
    if (
        args.previous is not None
        and args.output is not None
        and args.previous.resolve() == args.output.resolve()
    ):
        raise MonitorError("--previous and --output must be different files")

    document = (
        fetch_document(args.url, timeout=args.timeout, max_bytes=args.max_bytes)
        if args.url
        else read_document(
            args.input,
            source_url=args.source_url,
            content_type=args.content_type,
            max_bytes=args.max_bytes,
            deadline=monotonic() + args.timeout,
        )
    )
    current = normalize_document(
        document,
        max_pdf_decompressed_bytes=max_pdf_decompressed_bytes,
        max_pdf_extracted_chars=max_pdf_extracted_chars,
    )
    if not current:
        raise MonitorError("normalization produced empty content")
    current_bytes = current.encode("utf-8")
    if len(current_bytes) > _DEFAULT_MAX_SNAPSHOT_BYTES:
        raise MonitorError("normalized snapshot exceeds the size limit")

    previous = None
    if args.previous:
        previous_body = _read_regular_file_limited(
            args.previous, _DEFAULT_MAX_SNAPSHOT_BYTES, "--previous"
        )
        try:
            previous = previous_body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MonitorError("--previous is not valid UTF-8") from exc
    result = compare_text(
        current,
        previous,
        max_diff_lines=args.max_diff_lines,
        max_diff_bytes=max_diff_bytes,
    )
    if args.output:
        _write_snapshot_atomic(args.output, current_bytes)
    return {
        "source_url": document.source_url,
        "content_type": document.content_type,
        **result,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    try:
        result = run(_parser().parse_args(argv))
    except (MonitorError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
