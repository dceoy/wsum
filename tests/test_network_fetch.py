from __future__ import annotations

import ssl
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


if __name__ == "__main__":
    unittest.main()
