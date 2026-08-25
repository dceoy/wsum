from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
import support
from errors import MonitorError
from fetch_browser import BrowserFetchConfig, fetch_rendered
from models import State


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


class FakePopup:
    def __init__(self, requests: list[FakeRequest], context: "FakeContext") -> None:
        self.requests = requests
        self.context = context
        self.closed = False

    def close(self) -> None:
        self.closed = True
        # A real popup's initial request is issued by Chromium as soon as the
        # popup target is created, independent of when Playwright's "popup"
        # event reaches Python. Model that by routing it through the
        # context-level handlers before close() is observed.
        for request in self.requests:
            route = FakeRoute(request)
            self.context.route_handler(route)
            if not route.aborted and "response" in self.context.handlers:
                self.context.handlers["response"](
                    FakeResponse(request.url, headers={"content-length": "100"})
                )


class FakePage:
    def __init__(
        self,
        *,
        html: str,
        requests: list[FakeRequest],
        timeout: bool = False,
        popup_requests: list[FakeRequest] | None = None,
    ) -> None:
        self.html = html
        self.requests = requests
        self.timeout = timeout
        self.popup_requests = popup_requests or []
        self.handlers: dict[str, object] = {}
        self.context: "FakeContext | None" = None
        self.url = requests[0].url

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def goto(self, url: str, **_: object) -> FakeResponse:
        self.url = url
        if self.timeout:
            raise FakePlaywrightTimeout
        assert self.context is not None
        assert self.context.route_handler is not None
        for request in self.requests:
            route = FakeRoute(request)
            self.context.route_handler(route)
            if not route.aborted and "response" in self.context.handlers:
                self.context.handlers["response"](
                    FakeResponse(request.url, headers={"content-length": "100"})
                )
        if self.popup_requests:
            popup = FakePopup(self.popup_requests, self.context)
            self.handlers["popup"](popup)
        return FakeResponse(url, headers={"etag": "fixture"})

    def content(self) -> str:
        return self.html

    def evaluate(self, _script: str) -> int:
        return len(self.html.encode("utf-8"))


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.handlers: dict[str, object] = {}
        self.route_handler = None

    def route(self, _: str, handler) -> None:
        self.route_handler = handler

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def new_page(self) -> FakePage:
        self.page.context = self
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
        active_config = config or BrowserFetchConfig(
            verified_egress_pinning=True,
            verified_memory_bound=True,
            verified_execution_bound=True,
        )
        with patch.dict(sys.modules, playwright_modules(browser)):
            result = fetch_rendered(
                "https://example.com",
                config=active_config,
                resolver=support.public_resolver,
            )
        return result, context, browser

    def test_client_rendered_content_and_ephemeral_cleanup(self) -> None:
        page = FakePage(
            html="<html><body><main>Rendered</main></body></html>",
            requests=[FakeRequest("https://example.com")],
        )
        result, context, browser = self.run_fake(page)
        assert b"Rendered" in result.body
        assert result.content_type == "text/html"
        assert context.closed
        assert browser.closed

    def test_final_url_fragment_set_by_page_js_is_canonicalized(self) -> None:
        # Page JavaScript can rewrite `location.hash` after load (e.g. a
        # client-side router setting `#result`). `page.url` then carries
        # that fragment, but `State.from_mapping()` rejects any URL with a
        # fragment, so persisting the raw `page.url` as `validated_url`
        # would make State round-trip fail on the very next run. The
        # canonical URL returned by the SSRF guard strips the fragment.
        class FragmentPage(FakePage):
            def goto(self, url: str, **kwargs: object) -> FakeResponse:
                response = super().goto(url, **kwargs)
                self.url = f"{url}#result"
                return response

        page = FragmentPage(
            html="<html><body><main>Rendered</main></body></html>",
            requests=[FakeRequest("https://example.com")],
        )
        result, _, _ = self.run_fake(page)
        assert result.final_url == "https://example.com/"
        state = State.from_mapping(
            {"target_id": "t1", "validated_url": result.final_url}
        )
        assert state.validated_url == "https://example.com/"

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
                pytest.raises(MonitorError, match="non-public"),
            ):
                self.run_fake(page)

    def test_popup_with_private_initial_url_is_denied(self) -> None:
        page = FakePage(
            html="<html></html>",
            requests=[FakeRequest("https://example.com")],
            popup_requests=[FakeRequest("http://169.254.169.254/latest/meta-data")],
        )
        with pytest.raises(MonitorError, match="non-public"):
            self.run_fake(page)

    def test_timeout_request_limit_and_rendered_size_fail(self) -> None:
        timeout_page = FakePage(
            html="<html></html>",
            requests=[FakeRequest("https://example.com")],
            timeout=True,
        )
        with pytest.raises(MonitorError, match="timeout"):
            self.run_fake(timeout_page)
        request_page = FakePage(
            html="<html></html>",
            requests=[
                FakeRequest("https://example.com"),
                FakeRequest("https://example.com/two"),
            ],
        )
        with pytest.raises(MonitorError, match="request limit"):
            self.run_fake(
                request_page,
                config=BrowserFetchConfig(
                    max_requests=1,
                    verified_egress_pinning=True,
                    verified_memory_bound=True,
                    verified_execution_bound=True,
                ),
            )
        large_page = FakePage(
            html="<html>" + ("x" * 2_000) + "</html>",
            requests=[FakeRequest("https://example.com")],
        )
        with pytest.raises(MonitorError, match="rendered DOM"):
            self.run_fake(
                large_page,
                config=BrowserFetchConfig(
                    max_rendered_bytes=1_024,
                    verified_egress_pinning=True,
                    verified_memory_bound=True,
                    verified_execution_bound=True,
                ),
            )

    def test_browser_mode_fails_closed_without_verified_egress_pinning(self) -> None:
        with pytest.raises(MonitorError, match="browser_egress_not_verified"):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(),
                resolver=support.public_resolver,
            )

    def test_browser_mode_reports_missing_optional_runtime(self) -> None:
        with (
            patch.dict(
                sys.modules,
                {"playwright": None, "playwright.sync_api": None},
            ),
            pytest.raises(MonitorError, match="browser mode requires the optional Playwright runtime"),
        ):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(
                    verified_egress_pinning=True,
                    verified_memory_bound=True,
                    verified_execution_bound=True,
                ),
                resolver=support.public_resolver,
            )

    def test_browser_mode_fails_closed_without_verified_memory_bound(self) -> None:
        with pytest.raises(MonitorError, match="browser_memory_bound_not_verified"):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(verified_egress_pinning=True),
                resolver=support.public_resolver,
            )

    def test_browser_mode_fails_closed_without_verified_execution_bound(self) -> None:
        with pytest.raises(MonitorError, match="browser_execution_bound_not_verified"):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(
                    verified_egress_pinning=True, verified_memory_bound=True
                ),
                resolver=support.public_resolver,
            )


if __name__ == "__main__":
    unittest.main()
