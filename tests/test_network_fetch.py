from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

import fetch
import support
from errors import MonitorError
from fetch import FetchConfig, fetch_url
from network_policy import BrowserNetworkGuard, resolve_public_url


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"<html><body>ok</body></html>",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._position = 0
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}

    def getheader(self, name: str, default: str | None = None) -> str:
        return self._headers.get(name.lower(), default or "")

    def read(self, amount: int) -> bytes:
        chunk = self._body[self._position : self._position + amount]
        self._position += len(chunk)
        return chunk

    def read1(self, amount: int) -> bytes:
        return self.read(amount)

    def isclosed(self) -> bool:
        return self._position >= len(self._body)


class FakeSocket:
    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TrickleResponse(FakeResponse):
    """Sends one byte per read, just under the per-op timeout each time."""

    def __init__(
        self, clock: FakeClock, seconds_per_byte: float, total_bytes: int
    ) -> None:
        super().__init__(200, b"x" * total_bytes, {"Content-Type": "text/html"})
        self._clock = clock
        self._seconds_per_byte = seconds_per_byte
        self.read_count = 0

    def read(self, amount: int) -> bytes:
        self._clock.advance(self._seconds_per_byte)
        self.read_count += 1
        return super().read(min(amount, 1))


class _RealSlowTrickleServer:
    """A real local TCP server that trickles an HTTP response body one byte
    at a time.

    A hand-rolled fake ``read()`` cannot reproduce ``http.client``'s own
    buffered-socket read loop, which can perform many real recvs inside a
    single ``HTTPResponse.read()`` call. Driving an actual socket proves the
    fix against that real buffering instead of a fake that already returns
    after one byte per call.
    """

    def __init__(self, body: bytes, seconds_per_byte: float) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._body = body
        self._seconds_per_byte = seconds_per_byte
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65_536)
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    f"Content-Length: {len(self._body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                conn.sendall(header)
                for byte in self._body:
                    conn.sendall(bytes((byte,)))
                    time.sleep(self._seconds_per_byte)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()


class _RealTruncatedServer:
    """A real local TCP server that declares a ``Content-Length`` but closes
    the connection after sending only part of the body.

    ``HTTPResponse.read1()`` returns an empty byte string on premature EOF
    instead of raising ``IncompleteRead``, so only a real socket proves a
    connection that closes early is rejected rather than normalized as a
    successful, silently truncated fetch.
    """

    def __init__(self, declared_length: int, sent_body: bytes) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._declared_length = declared_length
        self._sent_body = sent_body
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65_536)
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    f"Content-Length: {self._declared_length}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                conn.sendall(header)
                conn.sendall(self._sent_body)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()


class _RealSlowTrickleChunkedServer:
    """A real local TCP server that sends valid headers immediately, then
    trickles a padded chunk-size line (chunk-extension bytes before the
    terminating CRLF) one byte at a time, without ever sending chunk data.

    ``http.client``'s chunked decoder parses the chunk-size line via the
    buffered file's own ``readline()``, independently of ``read1()``'s
    single-recv guarantee used for chunk data. A hand-rolled fake response
    cannot reproduce that internal buffered-read loop, so this drives a
    real socket to prove the fix against real chunked framing.
    """

    def __init__(self, chunk_size_line: bytes, seconds_per_byte: float) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._chunk_size_line = chunk_size_line
        self._seconds_per_byte = seconds_per_byte
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65_536)
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                conn.sendall(header)
                for byte in self._chunk_size_line:
                    conn.sendall(bytes((byte,)))
                    time.sleep(self._seconds_per_byte)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()


class _RealStalledChunkedServer:
    """A real local TCP server that sends valid chunked headers immediately,
    then goes completely silent (holds the connection open with no further
    bytes) for the given duration.

    Unlike a trickle, a single recv here can block for the peer's entire
    silence. That only stays bounded by the fetch's total deadline if the
    per-recv timeout itself is clamped to whatever remains of the deadline;
    a per-op timeout set once before the read (and left at its original,
    larger value for every recv inside it) would let this one recv block
    for far longer than the deadline permits.
    """

    def __init__(self, hold_seconds: float) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._hold_seconds = hold_seconds
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65_536)
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                conn.sendall(header)
                time.sleep(self._hold_seconds)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()


class _RealStalledTLSHandshakeServer:
    """A real local TCP server that accepts a connection, sends the start of
    a TLS handshake record, then goes completely silent for the given
    duration.

    ``ssl.SSLSocket.do_handshake()`` reads the raw fd directly through
    OpenSSL, independent of the ``recv``/``recv_into`` overrides that clamp
    every other read on this module's sockets to the remaining fetch
    deadline. Only a real socket proves whether the handshake itself is
    bounded by that deadline rather than by the connection's original
    per-op timeout.
    """

    def __init__(self, hold_seconds: float) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._hold_seconds = hold_seconds
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65_536)  # ClientHello
                # A TLS handshake record header (content type 0x16,
                # version 3.3) declaring more body than is ever delivered,
                # leaving the client waiting mid-record.
                conn.sendall(bytes([0x16, 0x03, 0x03, 0x00, 0x40]))
                time.sleep(self._hold_seconds)
            except OSError:
                return

    def close(self) -> None:
        self._listener.close()


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.closed = False
        self.request_headers: dict[str, str] = {}
        self.sock = FakeSocket()

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.method = method
        self.path = path
        self.request_headers = headers

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class NetworkPolicyTests(unittest.TestCase):
    def test_rejects_private_loopback_and_non_http(self) -> None:
        for url in (
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(MonitorError):
                resolve_public_url(url)

    def test_detects_dns_rebinding(self) -> None:
        answers = iter(
            [
                [(2, 1, 6, "", ("93.184.216.34", 443))],
                [(2, 1, 6, "", ("127.0.0.1", 443))],
            ]
        )

        def resolver(*_: object, **__: object) -> list[tuple]:
            return next(answers)

        with self.assertRaisesRegex(MonitorError, "non-public|changed"):
            resolve_public_url("https://example.com", resolver=resolver)

    def test_browser_guard_requires_explicit_hosts(self) -> None:
        guard = BrowserNetworkGuard(
            "https://example.com", resolver=support.public_resolver
        )
        guard.validate_request("https://example.com/script.js")
        with self.assertRaisesRegex(MonitorError, "explicitly allowed"):
            guard.validate_request("https://cdn.example.org/script.js")


class FetchTests(unittest.TestCase):
    def test_304_is_explicit_unchanged_and_sends_validators(self) -> None:
        response = FakeResponse(304, b"", {"ETag": "new"})
        connection = FakeConnection(response)
        with patch.object(fetch, "_open_connection", return_value=connection):
            result = fetch_url(
                "https://example.com/page",
                etag="old",
                last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
                validated_url="https://example.com/page",
                resolver=support.public_resolver,
            )
        self.assertEqual("unchanged", result.result)
        self.assertEqual("old", connection.request_headers["If-None-Match"])
        self.assertEqual("new", result.etag)

    def test_unexpected_304_and_unsafe_validators_fail(self) -> None:
        with patch.object(
            fetch,
            "_open_connection",
            return_value=FakeConnection(FakeResponse(304, b"")),
        ):
            with self.assertRaisesRegex(MonitorError, "without a conditional"):
                fetch_url("https://example.com", resolver=support.public_resolver)
        with self.assertRaisesRegex(MonitorError, "control characters"):
            fetch_url(
                "https://example.com",
                etag="bad\r\nheader",
                resolver=support.public_resolver,
            )

    def test_304_is_rejected_when_validators_could_not_be_bound(self) -> None:
        with patch.object(
            fetch,
            "_open_connection",
            return_value=FakeConnection(FakeResponse(304, b"")),
        ):
            with self.assertRaisesRegex(MonitorError, "without a conditional"):
                fetch_url(
                    "https://example.com",
                    etag="old",
                    last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
                    validated_url="https://example.com/final",
                    resolver=support.public_resolver,
                )

    def test_redirect_target_is_revalidated(self) -> None:
        response = FakeResponse(302, b"", {"Location": "http://127.0.0.1/admin"})
        with patch.object(
            fetch, "_open_connection", return_value=FakeConnection(response)
        ):
            with self.assertRaisesRegex(MonitorError, "non-public"):
                fetch_url(
                    "https://example.com",
                    resolver=support.public_resolver,
                )

    def test_validators_bind_to_the_final_url_not_the_origin(self) -> None:
        redirect = FakeConnection(
            FakeResponse(302, b"", {"Location": "https://example.com/final"})
        )
        revalidated = FakeConnection(FakeResponse(304, b"", {"ETag": "new"}))
        with patch.object(
            fetch, "_open_connection", side_effect=[redirect, revalidated]
        ):
            result = fetch_url(
                "https://example.com",
                etag="old",
                last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
                validated_url="https://example.com/final",
                resolver=support.public_resolver,
            )
        self.assertEqual("unchanged", result.result)
        self.assertNotIn("If-None-Match", redirect.request_headers)
        self.assertNotIn("If-Modified-Since", redirect.request_headers)
        self.assertEqual("old", revalidated.request_headers["If-None-Match"])

    def test_response_size_content_type_and_malformed_length_errors(self) -> None:
        cases = (
            (
                FakeResponse(
                    200,
                    b"x",
                    {"Content-Type": "text/html", "Content-Length": "999999"},
                ),
                "response_too_large",
            ),
            (
                FakeResponse(200, b"x", {"Content-Type": "image/png"}),
                "unsupported_content_type",
            ),
            (
                FakeResponse(
                    200,
                    b"x",
                    {
                        "Content-Type": "text/html",
                        "Content-Encoding": "gzip",
                    },
                ),
                "unsupported_content_encoding",
            ),
            (
                FakeResponse(
                    200,
                    b"x",
                    {"Content-Type": "text/html", "Content-Length": "abc"},
                ),
                "malformed_response",
            ),
            (
                # Declares more bytes than the body actually carries: the
                # response closes right after the short body, so the read
                # loop stops without ever seeing the declared length.
                FakeResponse(
                    200,
                    b"x",
                    {"Content-Type": "text/html", "Content-Length": "50"},
                ),
                "malformed_response",
            ),
        )
        for response, code in cases:
            with (
                self.subTest(code=code),
                patch.object(
                    fetch, "_open_connection", return_value=FakeConnection(response)
                ),
            ):
                with self.assertRaises(MonitorError) as raised:
                    fetch_url(
                        "https://example.com",
                        resolver=support.public_resolver,
                        config=FetchConfig(max_response_bytes=1_024),
                    )
                self.assertEqual(code, raised.exception.code)

    def test_unicode_paths_are_percent_encoded_and_tls_cannot_be_disabled(self) -> None:
        connection = FakeConnection(
            FakeResponse(200, b"<p>ok</p>", {"Content-Type": "text/html"})
        )
        with patch.object(fetch, "_open_connection", return_value=connection):
            fetch_url(
                "https://example.com/製品?q=価格",
                resolver=support.public_resolver,
            )
        self.assertIn("%E8%A3%BD%E5%93%81", connection.path)
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        setattr(insecure, "check_hostname", False)  # noqa: B010
        insecure.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(MonitorError, "must require"):
            fetch_url(
                "https://example.com",
                resolver=support.public_resolver,
                ssl_context=insecure,
            )

    def test_streamed_oversize_and_timeout_have_stable_codes(self) -> None:
        response = FakeResponse(
            200,
            b"x" * 2_000,
            {"Content-Type": "text/html"},
        )
        with patch.object(
            fetch, "_open_connection", return_value=FakeConnection(response)
        ):
            with self.assertRaises(MonitorError) as raised:
                fetch_url(
                    "https://example.com",
                    resolver=support.public_resolver,
                    config=FetchConfig(max_response_bytes=1_024),
                )
            self.assertEqual("response_too_large", raised.exception.code)
        with patch.object(fetch, "_open_connection", side_effect=TimeoutError()):
            with self.assertRaises(MonitorError) as raised:
                fetch_url("https://example.com", resolver=support.public_resolver)
            self.assertEqual("fetch_timeout", raised.exception.code)
            self.assertTrue(raised.exception.retryable)

    def test_slow_trickle_is_stopped_by_the_total_deadline(self) -> None:
        # Each read individually completes just before the per-op timeout, so
        # a per-read socket timeout alone never fires; only a total wall-clock
        # deadline across all reads can stop this from running indefinitely.
        # This exercises only the loop's own bookkeeping with a fake clock;
        # it does not model http.client's real buffered socket reads, which
        # is covered separately below with a real socket.
        clock = FakeClock()
        response = TrickleResponse(clock, seconds_per_byte=10.0, total_bytes=10_000_000)
        connection = FakeConnection(response)
        with (
            patch.object(fetch, "monotonic", clock.monotonic),
            patch.object(fetch, "_open_connection", return_value=connection),
        ):
            with self.assertRaises(MonitorError) as raised:
                fetch_url(
                    "https://example.com",
                    resolver=support.public_resolver,
                    config=FetchConfig(
                        timeout_seconds=15.0,
                        max_total_seconds=60.0,
                        max_response_bytes=10_000_000,
                    ),
                )
        self.assertEqual("fetch_timeout", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        # It must have bailed out via the deadline, not by reading everything:
        # 60 seconds of budget at 10 seconds per byte allows at most 6 reads.
        self.assertLessEqual(response.read_count, 6)

    def test_real_socket_fetch_succeeds_when_server_closes_the_connection(
        self,
    ) -> None:
        # fetch_url always sends ``Connection: close``, and a compliant
        # server echoes that in its response. http.client's getresponse()
        # then nulls out connection.sock as soon as it sees the response
        # will close the connection, even though the underlying socket (and
        # the response body) is still readable. A FakeConnection/FakeSocket
        # never models that, so only a real socket proves a normal fetch
        # survives it.
        body = b"<html><body>hello real socket</body></html>"
        server = _RealSlowTrickleServer(body=body, seconds_per_byte=0.0)
        self.addCleanup(server.close)
        real_connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=1.0
        )
        self.addCleanup(real_connection.close)
        with patch.object(fetch, "_open_connection", return_value=real_connection):
            result = fetch_url(
                "http://example.com/",
                resolver=support.public_resolver,
                config=FetchConfig(timeout_seconds=1.0, max_total_seconds=2.0),
            )
        self.assertEqual("fetched", result.result)
        self.assertEqual(body, result.body)

    def test_real_socket_truncated_body_is_rejected_as_malformed(self) -> None:
        server = _RealTruncatedServer(declared_length=100, sent_body=b"short body")
        self.addCleanup(server.close)
        real_connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=1.0
        )
        self.addCleanup(real_connection.close)
        with patch.object(fetch, "_open_connection", return_value=real_connection):
            with self.assertRaises(MonitorError) as raised:
                fetch_url(
                    "http://example.com/",
                    resolver=support.public_resolver,
                    config=FetchConfig(timeout_seconds=1.0, max_total_seconds=2.0),
                )
        self.assertEqual("malformed_response", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    def test_real_socket_slow_trickle_is_stopped_by_the_total_deadline(self) -> None:
        # A real trickling server can complete ``HTTPResponse.read()``'s
        # internal multi-recv loop well past the total deadline, since each
        # individual recv comfortably beats the per-op timeout. Only reading
        # via an API bounded to a single underlying recv (read1) lets the
        # deadline be rechecked often enough to bail out promptly.
        server = _RealSlowTrickleServer(body=b"x" * 30, seconds_per_byte=0.3)
        self.addCleanup(server.close)
        real_connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=1.0
        )
        self.addCleanup(real_connection.close)
        started = time.perf_counter()
        with patch.object(fetch, "_open_connection", return_value=real_connection):
            with self.assertRaises(MonitorError) as raised:
                fetch_url(
                    "http://example.com/",
                    resolver=support.public_resolver,
                    config=FetchConfig(timeout_seconds=1.0, max_total_seconds=1.5),
                )
        elapsed = time.perf_counter() - started
        self.assertEqual("fetch_timeout", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        # Fully trickling the 30-byte body would take 30 * 0.3s = 9s; bailing
        # out near the 1.5s deadline (not after ~9s) proves the read is
        # bounded per-recv rather than per-full-buffer-fill.
        self.assertLess(elapsed, 4.0)

    def test_pinned_connection_delivers_a_normal_response_intact(self) -> None:
        # Every other test that exercises ``_PinnedHTTPConnection``'s
        # deadline-checked socket asserts a timeout. This is the golden
        # path: a well-behaved, fast server must still read back exactly
        # as before, proving the per-recv deadline clamp does not itself
        # truncate or corrupt an ordinary successful read.
        body = b"<html><body>hello pinned socket</body></html>"
        server = _RealSlowTrickleServer(body=body, seconds_per_byte=0.0)
        self.addCleanup(server.close)
        connection = fetch._PinnedHTTPConnection(
            "example.com",
            "127.0.0.1",
            server.port,
            timeout=5.0,
            allowed_addresses=("127.0.0.1",),
            deadline=time.monotonic() + 5.0,
        )
        self.addCleanup(connection.close)
        with patch.object(fetch, "validate_peer_address"):
            connection.request(
                "GET", "/", headers={"Host": "example.com", "Connection": "close"}
            )
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        chunks = []
        while True:
            chunk = response.read1(65_536)
            if not chunk:
                break
            chunks.append(chunk)
        self.assertEqual(body, b"".join(chunks))

    def test_pinned_connection_deadline_stops_a_trickled_chunk_size_line(
        self,
    ) -> None:
        # A padded chunk-extension on the chunk-size line lets a peer
        # trickle many bytes before the terminating CRLF, entirely inside
        # ``_read1_chunked``'s internal ``readline()`` -- independent of the
        # single-recv-per-call ``read1()`` fix used for chunk data. Only a
        # deadline check on every real recv (not just between ``read1()``
        # calls) can bound this. This drives ``_PinnedHTTPConnection``
        # directly since it owns the deadline-checked socket.
        chunk_size_line = b"a;" + b"x" * 40 + b"\r\n"
        server = _RealSlowTrickleChunkedServer(
            chunk_size_line=chunk_size_line, seconds_per_byte=0.05
        )
        self.addCleanup(server.close)
        connection = fetch._PinnedHTTPConnection(
            "example.com",
            "127.0.0.1",
            server.port,
            timeout=5.0,
            allowed_addresses=("127.0.0.1",),
            deadline=time.monotonic() + 0.5,
        )
        self.addCleanup(connection.close)
        # Address pinning always rejects loopback peers (by design, for
        # SSRF safety) and is exercised separately by NetworkPolicyTests;
        # bypass only that check here to reach the real local test server.
        with patch.object(fetch, "validate_peer_address"):
            connection.request(
                "GET", "/", headers={"Host": "example.com", "Connection": "close"}
            )
        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            response = connection.getresponse()
            while True:
                chunk = response.read1(65_536)
                if not chunk:
                    break
        elapsed = time.perf_counter() - started
        # Fully trickling the ~44-byte chunk-size line would take over 2s;
        # bailing out near the 0.5s deadline proves every recv is checked,
        # not just the boundary between read1() calls.
        self.assertLess(elapsed, 2.0)

    def test_pinned_connection_deadline_bounds_a_complete_stall(self) -> None:
        # A peer that sends nothing at all after the headers (rather than
        # trickling bytes) lets a single recv block for the peer's entire
        # silence. A per-op timeout set once before the read (and left
        # unchanged for every recv inside it) would let this one recv run
        # for far longer than what remains of the fetch's total deadline;
        # only clamping each recv's own timeout to the remaining budget
        # bounds it.
        server = _RealStalledChunkedServer(hold_seconds=5.0)
        self.addCleanup(server.close)
        connection = fetch._PinnedHTTPConnection(
            "example.com",
            "127.0.0.1",
            server.port,
            timeout=5.0,
            allowed_addresses=("127.0.0.1",),
            deadline=time.monotonic() + 0.5,
        )
        self.addCleanup(connection.close)
        with patch.object(fetch, "validate_peer_address"):
            connection.request(
                "GET", "/", headers={"Host": "example.com", "Connection": "close"}
            )
        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            response = connection.getresponse()
            response.read1(65_536)
        elapsed = time.perf_counter() - started
        # The server holds the connection open for 5s; bailing out near
        # the 0.5s deadline (not after ~5s) proves the recv itself is
        # bounded by the remaining budget, not the original per-op timeout.
        self.assertLess(elapsed, 2.0)

    def test_pinned_https_connection_deadline_bounds_a_stalled_handshake(
        self,
    ) -> None:
        # The server sends the start of a handshake record then falls
        # silent for far longer than the fetch's total deadline. Old code
        # ran the handshake synchronously inside ``wrap_socket()`` using
        # only the connection's original per-op timeout, so this recv
        # would block for the server's whole 5s hold. Bailing out near the
        # 0.5s deadline instead proves the handshake itself is bounded by
        # the remaining budget, not the original per-op timeout.
        server = _RealStalledTLSHandshakeServer(hold_seconds=5.0)
        self.addCleanup(server.close)
        context = ssl.create_default_context()
        connection = fetch._PinnedHTTPSConnection(
            "example.com",
            "127.0.0.1",
            server.port,
            timeout=5.0,
            allowed_addresses=("127.0.0.1",),
            context=context,
            deadline=time.monotonic() + 0.5,
        )
        self.addCleanup(connection.close)
        started = time.perf_counter()
        with (
            self.assertRaises(TimeoutError),
            patch.object(fetch, "validate_peer_address"),
        ):
            connection.connect()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)

    def test_server_and_rate_limit_are_retryable(self) -> None:
        for status, code in ((429, "http_rate_limited"), (503, "http_server_error")):
            with (
                self.subTest(status=status),
                patch.object(
                    fetch,
                    "_open_connection",
                    return_value=FakeConnection(FakeResponse(status)),
                ),
            ):
                with self.assertRaises(MonitorError) as raised:
                    fetch_url("https://example.com", resolver=support.public_resolver)
                self.assertEqual(code, raised.exception.code)
                self.assertTrue(raised.exception.retryable)

    def test_charset_is_extracted_from_content_type_for_normalization(self) -> None:
        connection = FakeConnection(
            FakeResponse(
                200,
                "価格改定".encode("shift_jis"),
                {"Content-Type": "text/plain; charset=Shift_JIS"},
            )
        )
        with patch.object(fetch, "_open_connection", return_value=connection):
            result = fetch_url("https://example.com", resolver=support.public_resolver)
        self.assertEqual("text/plain", result.content_type)
        self.assertEqual("Shift_JIS", result.charset)

    def test_slow_initial_dns_resolution_is_bounded_by_the_total_deadline(self) -> None:
        def slow_resolver(*_: object, **__: object) -> list[tuple]:
            time.sleep(30)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        started = time.perf_counter()
        with self.assertRaises(MonitorError) as raised:
            fetch_url(
                "https://example.com",
                resolver=slow_resolver,
                config=FetchConfig(timeout_seconds=0.5, max_total_seconds=1.0),
            )
        elapsed = time.perf_counter() - started
        self.assertEqual("dns_resolution_failed", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        self.assertLess(elapsed, 10.0)

    def test_slow_redirect_dns_resolution_is_bounded_by_the_total_deadline(
        self,
    ) -> None:
        response = FakeResponse(
            302, b"", {"Location": "https://redirect-target.example/"}
        )
        connection = FakeConnection(response)
        call_count = {"n": 0}

        def resolver(*_: object, **__: object) -> list[tuple]:
            call_count["n"] += 1
            # The first two calls are the initial resolution (with its DNS
            # rebinding stability check); only the redirect's resolution
            # hangs, matching the reported gap.
            if call_count["n"] > 2:
                time.sleep(30)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        started = time.perf_counter()
        with patch.object(fetch, "_open_connection", return_value=connection):
            with self.assertRaises(MonitorError) as raised:
                fetch_url(
                    "https://example.com",
                    resolver=resolver,
                    config=FetchConfig(timeout_seconds=0.5, max_total_seconds=1.0),
                )
        elapsed = time.perf_counter() - started
        self.assertEqual("dns_resolution_failed", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        self.assertLess(elapsed, 10.0)

    def test_repeated_dns_timeouts_keep_resolver_workers_bounded(self) -> None:
        release = threading.Event()
        pool = fetch._ResolverPool(worker_count=2)

        def stuck_resolver(*_: object, **__: object) -> list[tuple]:
            release.wait()
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        bounded = fetch._bounded_resolver(
            stuck_resolver,
            lambda: 0.01,
            pool=pool,
        )
        try:
            for _ in range(10):
                with self.assertRaises(TimeoutError):
                    bounded("example.com", 443)
            self.assertEqual(2, len(pool._workers))
            self.assertTrue(all(worker.is_alive() for worker in pool._workers))
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
