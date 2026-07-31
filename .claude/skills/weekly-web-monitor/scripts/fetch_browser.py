"""Optional explicit browser-mode fetcher.

Playwright is an optional runtime dependency. Browser mode is never selected
automatically, and every request is checked by the same public-network policy.
Route and response interception is registered at the browser-context level so
popups inherit the same checks from their first navigation. Chromium still
performs its own DNS resolution when a route is allowed to continue, so a
DNS-rebinding attacker can still reach a private address between the guard's
check and Chromium's connection; see references/security.md. Because that gap
has no verified mitigation yet, `fetch_rendered` fails closed unless the
operator explicitly sets `BrowserFetchConfig.verified_egress_pinning=True`
after configuring a verified pinning mechanism.

The rendered-size guard reads `document.documentElement.outerHTML` inside the
page to measure it before materializing it in the Routine process, but
Chromium/Blink still has to build that string in the renderer first; a page
that balloons its own DOM can exhaust the browser process before the guard
ever runs. This module cannot bound in-renderer memory from the Playwright
API, so `fetch_rendered` also fails closed unless the operator explicitly sets
`BrowserFetchConfig.verified_memory_bound=True` after placing the browser
process under an external hard memory limit (for example a container/cgroup
memory cap that kills the process before host memory is exhausted).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from errors import MonitorError
from fetch import FetchResult
from network_policy import BrowserNetworkGuard, Resolver


@dataclass(frozen=True, slots=True)
class BrowserFetchConfig:
    timeout_seconds: float = 30.0
    max_rendered_bytes: int = 5_000_000
    max_requests: int = 100
    max_declared_resource_bytes: int = 10_000_000
    allowed_hosts: tuple[str, ...] = ()
    block_resource_types: tuple[str, ...] = ("font", "media")
    verified_egress_pinning: bool = False
    verified_memory_bound: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 120:
            raise MonitorError(
                "invalid_configuration", "browser timeout must be 1-120 seconds"
            )
        if not 1_024 <= self.max_rendered_bytes <= 50_000_000:
            raise MonitorError(
                "invalid_configuration", "browser rendered size limit is invalid"
            )
        if not 1 <= self.max_requests <= 1_000:
            raise MonitorError(
                "invalid_configuration", "browser request limit is invalid"
            )
        if not 1_024 <= self.max_declared_resource_bytes <= 100_000_000:
            raise MonitorError(
                "invalid_configuration",
                "browser declared resource size limit is invalid",
            )
        if len(self.allowed_hosts) > 100 or len(self.block_resource_types) > 20:
            raise MonitorError(
                "invalid_configuration", "browser host or resource policy is too large"
            )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def fetch_rendered(
    url: str,
    *,
    config: BrowserFetchConfig | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> FetchResult:
    active = config or BrowserFetchConfig()
    if not active.verified_egress_pinning:
        raise MonitorError(
            "browser_egress_not_verified",
            "browser mode is blocked until verified network-level egress "
            "pinning (e.g. an external proxy or --host-resolver-rules) is "
            "configured; see references/security.md",
        )
    if not active.verified_memory_bound:
        raise MonitorError(
            "browser_memory_bound_not_verified",
            "browser mode is blocked until the browser process runs under a "
            "verified external hard memory limit (e.g. a container/cgroup "
            "memory cap); the rendered-size guard cannot bound Chromium's "
            "own DOM memory before it is measured, see references/security.md",
        )
    guard = BrowserNetworkGuard(
        url, allowed_hosts=active.allowed_hosts, resolver=resolver
    )
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MonitorError(
            "browser_runtime_unavailable",
            "browser mode requires the optional Playwright runtime",
        ) from exc

    request_count = 0
    declared_bytes = 0
    blocked_error: MonitorError | None = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-features=InterestFeedContentSuggestions,"
                "MediaRouter,OptimizationHints,Translate",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
            ],
        )
        context = browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )

        def handle_route(route: Any) -> None:
            nonlocal request_count, blocked_error
            request_count += 1
            if request_count > active.max_requests:
                blocked_error = MonitorError(
                    "browser_resource_limit", "browser request limit exceeded"
                )
                route.abort("blockedbyclient")
                return
            if route.request.resource_type in active.block_resource_types:
                route.abort("blockedbyclient")
                return
            try:
                guard.validate_request(route.request.url)
            except MonitorError as exc:
                blocked_error = exc
                route.abort("blockedbyclient")
                return
            route.continue_()

        def handle_response(response: Any) -> None:
            nonlocal declared_bytes, blocked_error
            if blocked_error:
                return
            try:
                guard.validate_request(response.url)
                raw_length = response.headers.get("content-length", "")
                if raw_length:
                    length = int(raw_length)
                    if length < 0:
                        raise ValueError
                    declared_bytes += length
                    if declared_bytes > active.max_declared_resource_bytes:
                        blocked_error = MonitorError(
                            "browser_resource_limit",
                            "browser declared resource size limit exceeded",
                        )
                server_address_reader = getattr(response, "server_addr", None)
                if not callable(server_address_reader):
                    raise MonitorError(
                        "browser_peer_unavailable",
                        "browser runtime does not expose the response peer",
                    )
                server_address = server_address_reader()
                if not server_address or not server_address.get("ipAddress"):
                    raise MonitorError(
                        "browser_peer_unavailable",
                        "browser response peer is unavailable",
                    )
                guard.validate_response_peer(response.url, server_address["ipAddress"])
            except (MonitorError, TypeError, ValueError) as exc:
                blocked_error = (
                    exc
                    if isinstance(exc, MonitorError)
                    else MonitorError(
                        "malformed_response",
                        "browser received an invalid Content-Length header",
                    )
                )

        context.route("**/*", handle_route)
        context.on("response", handle_response)
        page = context.new_page()
        page.on("popup", lambda popup: popup.close())
        try:
            response = page.goto(
                guard.initial.url,
                wait_until="networkidle",
                timeout=int(active.timeout_seconds * 1_000),
            )
            if blocked_error:
                raise blocked_error
            if response is None:
                raise MonitorError(
                    "browser_navigation_failed", "browser produced no main response"
                )
            guard.validate_request(page.url)
            if response.status >= 400:
                retryable = response.status == 429 or response.status >= 500
                code = (
                    "http_rate_limited"
                    if response.status == 429
                    else "http_server_error"
                    if response.status >= 500
                    else "http_client_error"
                )
                raise MonitorError(
                    code,
                    f"browser main response returned HTTP {response.status}",
                    retryable=retryable,
                )
            dom_bytes = page.evaluate(
                "() => new Blob([document.documentElement.outerHTML]).size"
            )
            if not isinstance(dom_bytes, int) or dom_bytes > active.max_rendered_bytes:
                raise MonitorError(
                    "response_too_large", "rendered DOM exceeds the size limit"
                )
            rendered = page.content().encode("utf-8")
            if len(rendered) > active.max_rendered_bytes:
                raise MonitorError(
                    "response_too_large", "rendered DOM exceeds the size limit"
                )
            return FetchResult(
                result="fetched",
                final_url=page.url,
                status=response.status,
                content_type="text/html",
                charset="utf-8",
                content_length=len(rendered),
                etag=response.headers.get("etag", "")[:1_000],
                last_modified=response.headers.get("last-modified", "")[:1_000],
                fetched_at=_utc_now(),
                redirect_count=0,
                body=rendered,
            )
        except PlaywrightTimeoutError as exc:
            raise MonitorError(
                "fetch_timeout",
                "browser execution exceeded its timeout",
                retryable=True,
            ) from exc
        except PlaywrightError as exc:
            raise MonitorError(
                "browser_navigation_failed", "browser navigation failed"
            ) from exc
        finally:
            context.close()
            browser.close()
