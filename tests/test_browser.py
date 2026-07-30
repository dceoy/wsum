from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import support
from errors import MonitorError
from fetch_browser import BrowserFetchConfig, fetch_rendered


class FakePlaywrightError(Exception):
    pass


class FakePlaywrightTimeout(FakePlaywrightError):
    pass


class FakeRequest:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.aborted = False
        self.continued = False

    def abort(self, _: str) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class FakeResponse:
    def __init__(
        self,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        peer: str = "93.184.216.34",
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.peer = peer

    def server_addr(self) -> dict[str, str]:
        return {"ipAddress": self.peer, "port": "443"}


class FakePage:
    def __init__(
        self,
        *,
        html: str,
        requests: list[FakeRequest],
        timeout: bool = False,
    ) -> None:
        self.html = html
        self.requests = requests
        self.timeout = timeout
        self.handlers: dict[str, object] = {}
        self.route_handler = None
        self.url = requests[0].url

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def route(self, _: str, handler) -> None:
        self.route_handler = handler

    def goto(self, url: str, **_: object) -> FakeResponse:
        self.url = url
        if self.timeout:
            raise FakePlaywrightTimeout
        assert self.route_handler is not None
        for request in self.requests:
            route = FakeRoute(request)
            self.route_handler(route)
            if not route.aborted and "response" in self.handlers:
                self.handlers["response"](
                    FakeResponse(request.url, headers={"content-length": "100"})
                )
        return FakeResponse(url, headers={"etag": "fixture"})

    def content(self) -> str:
        return self.html


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    def new_context(self, **_: object) -> FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class FakePlaywrightManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **_: browser)
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_: object) -> None:
        return None


def playwright_modules(browser: FakeBrowser) -> dict[str, ModuleType]:
    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.Error = FakePlaywrightError
    sync_api.TimeoutError = FakePlaywrightTimeout
    sync_api.sync_playwright = lambda: FakePlaywrightManager(browser)
    package.sync_api = sync_api
    return {"playwright": package, "playwright.sync_api": sync_api}


class BrowserFetcherTests(unittest.TestCase):
    def run_fake(
        self,
        page: FakePage,
        *,
        config: BrowserFetchConfig | None = None,
    ):
        context = FakeContext(page)
        browser = FakeBrowser(context)
        with patch.dict(sys.modules, playwright_modules(browser)):
            result = fetch_rendered(
                "https://example.com",
                config=config,
                resolver=support.public_resolver,
            )
        return result, context, browser

    def test_client_rendered_content_and_ephemeral_cleanup(self) -> None:
        page = FakePage(
            html="<html><body><main>Rendered</main></body></html>",
            requests=[FakeRequest("https://example.com")],
        )
        result, context, browser = self.run_fake(page)
        self.assertIn(b"Rendered", result.body)
        self.assertEqual("text/html", result.content_type)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_private_subresource_and_redirect_are_denied(self) -> None:
        for private_url in (
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
        ):
            page = FakePage(
                html="<html></html>",
                requests=[
                    FakeRequest("https://example.com"),
                    FakeRequest(private_url, "script"),
                ],
            )
            with (
                self.subTest(private_url=private_url),
                self.assertRaisesRegex(MonitorError, "non-public"),
            ):
                self.run_fake(page)

    def test_timeout_request_limit_and_rendered_size_fail(self) -> None:
        timeout_page = FakePage(
            html="<html></html>",
            requests=[FakeRequest("https://example.com")],
            timeout=True,
        )
        with self.assertRaisesRegex(MonitorError, "timeout"):
            self.run_fake(timeout_page)
        request_page = FakePage(
            html="<html></html>",
            requests=[
                FakeRequest("https://example.com"),
                FakeRequest("https://example.com/two"),
            ],
        )
        with self.assertRaisesRegex(MonitorError, "request limit"):
            self.run_fake(request_page, config=BrowserFetchConfig(max_requests=1))
        large_page = FakePage(
            html="<html>" + ("x" * 2_000) + "</html>",
            requests=[FakeRequest("https://example.com")],
        )
        with self.assertRaisesRegex(MonitorError, "rendered DOM"):
            self.run_fake(
                large_page,
                config=BrowserFetchConfig(max_rendered_bytes=1_024),
            )


if __name__ == "__main__":
    unittest.main()
