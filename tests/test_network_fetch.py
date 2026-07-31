from __future__ import annotations

import ssl
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


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.closed = False
        self.request_headers: dict[str, str] = {}

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


if __name__ == "__main__":
    unittest.main()
