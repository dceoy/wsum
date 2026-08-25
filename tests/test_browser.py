"""Tests for the browser module."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from errors import MonitorError
from fetch_browser import BrowserFetchConfig, FetchResult, fetch_rendered
from models import State

from tests import support

if TYPE_CHECKING:
    from collections.abc import Callable


class FakePlaywrightError(Exception):
    """A fake `playwright.sync_api.Error` base exception."""


class FakePlaywrightTimeoutError(FakePlaywrightError):
    """A fake `playwright.sync_api.TimeoutError`."""


@dataclass
class FakeRequest:
    """A minimal fake Playwright request."""

    url: str
    resource_type: str = "document"


class FakeRoute:
    """A fake Playwright route that records abort/continue calls."""

    def __init__(self, request: FakeRequest) -> None:
        """Track a single incoming request."""
        self.request = request
        self.aborted = False
        self.continued = False

    def abort(self, _: str) -> None:
        """Record that this route was aborted."""
        self.aborted = True

    def continue_(self) -> None:
        """Record that this route was continued."""
        self.continued = True


class FakeResponse:
    """A fake Playwright response with a fixed peer address."""

    def __init__(
        self,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        peer: str = "93.184.216.34",
    ) -> None:
        """Build a fake response with the given status, headers, and peer."""
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.peer = peer

    def server_addr(self) -> dict[str, str]:
        """Return the fake TCP peer address for this response."""
        return {"ipAddress": self.peer, "port": "443"}


class FakePopup:
    """A fake Playwright popup page that replays its own requests on close."""

    def __init__(self, requests: list[FakeRequest], context: "FakeContext") -> None:
        """Track the requests this popup will replay on close."""
        self.requests = requests
        self.context = context
        self.closed = False

    def close(self) -> None:
        """Close the popup, replaying its requests through the parent context."""
        self.closed = True
        # A real popup's initial request is issued by Chromium as soon as the
        # popup target is created, independent of when Playwright's "popup"
        # event reaches Python. Model that by routing it through the
        # context-level handlers before close() is observed.
        for request in self.requests:
            route = FakeRoute(request)
            assert self.context.route_handler is not None
            self.context.route_handler(route)
            if not route.aborted and "response" in self.context.handlers:
                self.context.handlers["response"](
                    FakeResponse(request.url, headers={"content-length": "100"})
                )


class FakePage:
    """A fake Playwright page that replays canned requests and responses."""

    def __init__(
        self,
        *,
        html: str,
        requests: list[FakeRequest],
        timeout: bool = False,
        popup_requests: list[FakeRequest] | None = None,
    ) -> None:
        """Build a fake page that replays ``requests`` on navigation."""
        self.html = html
        self.requests = requests
        self.timeout = timeout
        self.popup_requests = popup_requests or []
        self.handlers: dict[str, Callable[..., object]] = {}
        self.context: "FakeContext | None" = None
        self.url = requests[0].url

    def on(self, event: str, handler: Callable[..., object]) -> None:
        """Register an event handler."""
        self.handlers[event] = handler

    def goto(self, url: str, **_: object) -> FakeResponse:
        """Navigate to ``url``, replaying canned requests, responses, and popups.

        Returns:
            The final fake response for the navigation.

        Raises:
            FakePlaywrightTimeoutError: If this page was built with
                ``timeout=True``.
        """
        self.url = url
        if self.timeout:
            raise FakePlaywrightTimeoutError
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
        """Return the fake page's rendered HTML."""
        return self.html

    def evaluate(self, _script: str) -> int:
        """Return the fake byte-length evaluation result."""
        return len(self.html.encode("utf-8"))


class FakeContext:
    """A fake Playwright browser context wrapping a single FakePage."""

    def __init__(self, page: FakePage) -> None:
        """Wrap ``page`` in a fake browser context."""
        self.page = page
        self.closed = False
        self.handlers: dict[str, Callable[..., object]] = {}
        self.route_handler: Callable[[FakeRoute], None] | None = None

    def route(self, _: str, handler: Callable[[FakeRoute], None]) -> None:
        """Register the route interception handler."""
        self.route_handler = handler

    def on(self, event: str, handler: Callable[..., object]) -> None:
        """Register an event handler."""
        self.handlers[event] = handler

    def new_page(self) -> FakePage:
        """Return this context's single fake page."""
        self.page.context = self
        return self.page

    def close(self) -> None:
        """Mark this context as closed."""
        self.closed = True


class FakeBrowser:
    """A fake Playwright browser wrapping a single FakeContext."""

    def __init__(self, context: FakeContext) -> None:
        """Wrap ``context`` in a fake browser."""
        self.context = context
        self.closed = False

    def new_context(self, **_: object) -> FakeContext:
        """Return this browser's single fake context."""
        return self.context

    def close(self) -> None:
        """Mark this browser as closed."""
        self.closed = True


class FakePlaywrightManager:
    """A fake `sync_playwright()` context manager."""

    def __init__(self, browser: FakeBrowser) -> None:
        """Wrap ``browser`` behind a fake ``sync_playwright()`` chromium launcher."""

        def launch(**_kwargs: object) -> FakeBrowser:
            return browser

        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    def __enter__(self) -> SimpleNamespace:
        """Return the fake playwright namespace."""
        return self.playwright

    def __exit__(self, *_: object) -> None:
        """No-op context manager exit."""
        return None


def playwright_modules(browser: FakeBrowser) -> dict[str, ModuleType]:
    """Build fake `playwright`/`playwright.sync_api` modules wrapping ``browser``.

    Returns:
        A ``sys.modules``-style mapping of the fake module names to modules.
    """
    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")

    def sync_playwright() -> FakePlaywrightManager:
        return FakePlaywrightManager(browser)

    setattr(sync_api, "Error", FakePlaywrightError)  # ruff: ignore[set-attr-with-constant]
    setattr(sync_api, "TimeoutError", FakePlaywrightTimeoutError)  # ruff: ignore[set-attr-with-constant]
    setattr(sync_api, "sync_playwright", sync_playwright)  # ruff: ignore[set-attr-with-constant]
    setattr(package, "sync_api", sync_api)  # ruff: ignore[set-attr-with-constant]
    return {"playwright": package, "playwright.sync_api": sync_api}


class BrowserFetcherTests(unittest.TestCase):
    """Tests for BrowserFetcherTests."""

    def run_fake(
        self,
        page: FakePage,
        *,
        config: BrowserFetchConfig | None = None,
    ) -> tuple[FetchResult, FakeContext, FakeBrowser]:
        """Run fetch_rendered against a fake browser stack and return its outputs.

        Returns:
            A tuple of (fetch_rendered's result, the fake context, the fake
            browser).
        """
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
        """Test that client rendered content and ephemeral cleanup."""
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
        """Test that final url fragment set by page js is canonicalized."""

        class FragmentPage(FakePage):
            """A FakePage variant whose final URL carries a fragment."""

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
        state = State.from_mapping({
            "target_id": "t1",
            "validated_url": result.final_url,
        })
        assert state.validated_url == "https://example.com/"

    def test_private_subresource_and_redirect_are_denied(self) -> None:
        """Test that private subresource and redirect are denied."""
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
        """Test that popup with private initial url is denied."""
        page = FakePage(
            html="<html></html>",
            requests=[FakeRequest("https://example.com")],
            popup_requests=[FakeRequest("http://169.254.169.254/latest/meta-data")],
        )
        with pytest.raises(MonitorError, match="non-public"):
            self.run_fake(page)

    def test_timeout_request_limit_and_rendered_size_fail(self) -> None:
        """Test that timeout request limit and rendered size fail."""
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
        """Test that browser mode fails closed without verified egress pinning."""
        with pytest.raises(MonitorError, match="browser_egress_not_verified"):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(),
                resolver=support.public_resolver,
            )

    def test_browser_mode_reports_missing_optional_runtime(self) -> None:
        """Test that browser mode reports missing optional runtime."""
        with (
            patch.dict(
                sys.modules,
                {"playwright": None, "playwright.sync_api": None},
            ),
            pytest.raises(
                MonitorError,
                match="browser mode requires the optional Playwright runtime",
            ),
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
        """Test that browser mode fails closed without verified memory bound."""
        with pytest.raises(MonitorError, match="browser_memory_bound_not_verified"):
            fetch_rendered(
                "https://example.com",
                config=BrowserFetchConfig(verified_egress_pinning=True),
                resolver=support.public_resolver,
            )

    def test_browser_mode_fails_closed_without_verified_execution_bound(self) -> None:
        """Test that browser mode fails closed without verified execution bound."""
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
