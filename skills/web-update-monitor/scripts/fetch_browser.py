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

`config.timeout_seconds` is only passed to `page.goto()`. The subsequent
`page.evaluate()` and `page.content()` calls are plain Playwright sync-API
calls with no `timeout` parameter of their own, so an unresponsive or
CPU-saturated renderer can occupy a Routine worker indefinitely after
navigation succeeds; there is no supported way to attach a wall-clock
deadline to those specific calls, and interrupting a blocked Playwright sync
call from a watchdog thread is not a documented/thread-safe operation. This
module cannot bound total execution time from the Playwright API, so
`fetch_rendered` also fails closed unless the operator explicitly sets
`BrowserFetchConfig.verified_execution_bound=True` after placing the browser
process under an external wall-clock/liveness supervisor (for example a
process-group timeout or container liveness probe that kills the process
tree if it runs past the configured deadline).
"""

from __future__ import annotations

import importlib
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from errors import MonitorError
from fetch import FetchResult
from models import utc_now
from network_policy import BrowserNetworkGuard, Resolver

if TYPE_CHECKING:
    from collections.abc import Callable

_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 120
_MIN_MAX_RENDERED_BYTES = 1_024
_MAX_MAX_RENDERED_BYTES = 50_000_000
_MIN_MAX_REQUESTS = 1
_MAX_MAX_REQUESTS = 1_000
_MIN_MAX_DECLARED_RESOURCE_BYTES = 1_024
_MAX_MAX_DECLARED_RESOURCE_BYTES = 100_000_000
_MAX_ALLOWED_HOSTS = 100
_MAX_BLOCK_RESOURCE_TYPES = 20
_HTTP_CLIENT_ERROR_STATUS = 400
_HTTP_RATE_LIMITED_STATUS = 429
_HTTP_SERVER_ERROR_STATUS = 500
_MAX_VALIDATOR_HEADER_LENGTH = 1_000
_MILLISECONDS_PER_SECOND = 1_000

_CHROMIUM_LAUNCH_ARGS = (
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    (
        "--disable-features=InterestFeedContentSuggestions,"
        "MediaRouter,OptimizationHints,Translate"
    ),
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
)


@dataclass(frozen=True, slots=True)
class BrowserFetchConfig:
    """Bounds and network policy for one optional browser-mode fetch."""

    timeout_seconds: float = 30.0
    max_rendered_bytes: int = 5_000_000
    max_requests: int = 100
    max_declared_resource_bytes: int = 10_000_000
    allowed_hosts: tuple[str, ...] = ()
    block_resource_types: tuple[str, ...] = ("font", "media")
    verified_egress_pinning: bool = False
    verified_memory_bound: bool = False
    verified_execution_bound: bool = False

    def __post_init__(self) -> None:
        """Validate that every bound falls within its allowed range.

        Raises:
            MonitorError: If any field is outside its allowed range.
        """
        if not _MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            msg = "invalid_configuration"
            raise MonitorError(msg, "browser timeout must be 1-120 seconds")
        if not (
            _MIN_MAX_RENDERED_BYTES
            <= self.max_rendered_bytes
            <= _MAX_MAX_RENDERED_BYTES
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "browser rendered size limit is invalid")
        if not _MIN_MAX_REQUESTS <= self.max_requests <= _MAX_MAX_REQUESTS:
            msg = "invalid_configuration"
            raise MonitorError(msg, "browser request limit is invalid")
        if not (
            _MIN_MAX_DECLARED_RESOURCE_BYTES
            <= self.max_declared_resource_bytes
            <= _MAX_MAX_DECLARED_RESOURCE_BYTES
        ):
            msg = "invalid_configuration"
            raise MonitorError(
                msg,
                "browser declared resource size limit is invalid",
            )
        if (
            len(self.allowed_hosts) > _MAX_ALLOWED_HOSTS
            or len(self.block_resource_types) > _MAX_BLOCK_RESOURCE_TYPES
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "browser host or resource policy is too large")


@dataclass(slots=True)
class _BrowserFetchState:
    """Mutable request/response-interception state shared across handlers."""

    request_count: int = 0
    declared_bytes: int = 0
    blocked_error: MonitorError | None = None


def _ensure_browser_mode_verified(active: BrowserFetchConfig) -> None:
    """Fail closed unless every unmitigated browser-mode risk is verified.

    Raises:
        MonitorError: If egress pinning, the memory bound, or the
            execution bound is not verified.
    """
    if not active.verified_egress_pinning:
        msg = "browser_egress_not_verified"
        raise MonitorError(
            msg,
            "browser mode is blocked until verified network-level egress "
            "pinning (e.g. an external proxy or --host-resolver-rules) is "
            "configured; see references/security.md",
        )
    if not active.verified_memory_bound:
        msg = "browser_memory_bound_not_verified"
        raise MonitorError(
            msg,
            "browser mode is blocked until the browser process runs under a "
            "verified external hard memory limit (e.g. a container/cgroup "
            "memory cap); the rendered-size guard cannot bound Chromium's "
            "own DOM memory before it is measured, see references/security.md",
        )
    if not active.verified_execution_bound:
        msg = "browser_execution_bound_not_verified"
        raise MonitorError(
            msg,
            "browser mode is blocked until the browser process runs under a "
            "verified external wall-clock/liveness supervisor; "
            "page.evaluate()/page.content() have no timeout of their own, "
            "so an unresponsive renderer can hang past config.timeout_seconds "
            "with no supported way to bound it from this module, see "
            "references/security.md",
        )


def _import_playwright() -> tuple[type[Exception], type[Exception], Callable[[], Any]]:
    """Import the optional Playwright sync API.

    Returns:
        A (``Error``, ``TimeoutError``, ``sync_playwright``) tuple from
        ``playwright.sync_api``.

    Raises:
        MonitorError: If Playwright is not installed.
    """
    try:
        playwright_sync_api = cast(
            "Any", importlib.import_module("playwright.sync_api")
        )
        error_type = playwright_sync_api.Error
        timeout_error_type = playwright_sync_api.TimeoutError
        sync_playwright = playwright_sync_api.sync_playwright
    except ImportError as exc:
        msg = "browser_runtime_unavailable"
        raise MonitorError(
            msg,
            "browser mode requires the optional Playwright runtime",
        ) from exc
    return error_type, timeout_error_type, sync_playwright


def _make_route_handler(
    guard: BrowserNetworkGuard, active: BrowserFetchConfig, state: _BrowserFetchState
) -> Callable[[Any], None]:
    """Build a per-request route handler enforcing the resource/network policy.

    Returns:
        The route handler to register on the browser context.
    """

    def handle_route(route: Any) -> None:  # ruff: ignore[any-type] -- Playwright is an
        # optional runtime dependency with no static Route type available
        # here without a hard import.
        state.request_count += 1
        if state.request_count > active.max_requests:
            state.blocked_error = MonitorError(
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
            state.blocked_error = exc
            route.abort("blockedbyclient")
            return
        route.continue_()

    return handle_route


def _read_response_peer_ip(response: Any) -> str:  # ruff: ignore[any-type] -- see
    # handle_route above: Playwright's Response type is not staticly
    # importable without a hard dependency on the optional runtime.
    """Read and validate the response's server peer IP address.

    Returns:
        The peer's IP address string.

    Raises:
        MonitorError: If the runtime does not expose the peer, or the peer
            is malformed.
    """
    server_address_reader = getattr(response, "server_addr", None)
    if not callable(server_address_reader):
        msg = "browser_peer_unavailable"
        raise MonitorError(
            msg,
            "browser runtime does not expose the response peer",
        )
    server_address = server_address_reader()
    if not isinstance(server_address, dict):
        msg = "browser_peer_unavailable"
        raise MonitorError(
            msg,
            "browser response peer is unavailable",
        )
    server_address = cast("dict[str, object]", server_address)
    ip_address = server_address.get("ipAddress")
    if not isinstance(ip_address, str) or not ip_address:
        msg = "browser_peer_unavailable"
        raise MonitorError(
            msg,
            "browser response peer is unavailable",
        )
    return ip_address


def _check_response_declared_size(
    response: Any,  # ruff: ignore[any-type] -- see handle_route above
    active: BrowserFetchConfig,
    state: _BrowserFetchState,
) -> None:
    """Accumulate and bound the response's declared Content-Length.

    Raises:
        ValueError: If the declared Content-Length is malformed.
    """
    raw_length = response.headers.get("content-length", "")
    if not raw_length:
        return
    length = int(raw_length)
    if length < 0:
        raise ValueError
    state.declared_bytes += length
    if state.declared_bytes > active.max_declared_resource_bytes:
        state.blocked_error = MonitorError(
            "browser_resource_limit",
            "browser declared resource size limit exceeded",
        )


def _make_response_handler(
    guard: BrowserNetworkGuard, active: BrowserFetchConfig, state: _BrowserFetchState
) -> Callable[[Any], None]:
    """Build a per-response handler enforcing the network/size policy.

    Returns:
        The response handler to register on the browser context.
    """

    def handle_response(response: Any) -> None:  # ruff: ignore[any-type] -- see
        # handle_route above.
        if state.blocked_error:
            return
        try:
            guard.validate_request(response.url)
            _check_response_declared_size(response, active, state)
            ip_address = _read_response_peer_ip(response)
            guard.validate_response_peer(response.url, ip_address)
        except (MonitorError, TypeError, ValueError) as exc:
            state.blocked_error = (
                exc
                if isinstance(exc, MonitorError)
                else MonitorError(
                    "malformed_response",
                    "browser received an invalid Content-Length header",
                )
            )

    return handle_response


def _classify_http_error_status(status: int) -> tuple[str, bool]:
    """Classify an HTTP error status into a (code, retryable) pair.

    Returns:
        A (MonitorError code, retryable) tuple.
    """
    if status == _HTTP_RATE_LIMITED_STATUS:
        return "http_rate_limited", True
    if status >= _HTTP_SERVER_ERROR_STATUS:
        return "http_server_error", True
    return "http_client_error", False


def _capture_rendered_page(
    page: Any,  # ruff: ignore[any-type] -- see handle_route above
    guard: BrowserNetworkGuard,
    active: BrowserFetchConfig,
    state: _BrowserFetchState,
    playwright_errors: tuple[type[Exception], type[Exception]],
) -> FetchResult:
    """Navigate ``page`` to its target and capture the rendered DOM.

    Returns:
        The successful fetch result.

    Raises:
        MonitorError: If navigation is blocked by policy, produces no
            response, returns an HTTP error status, or the rendered DOM
            exceeds the configured size limit.
    """
    playwright_error, playwright_timeout_error = playwright_errors
    try:
        response = page.goto(
            guard.initial.url,
            wait_until="networkidle",
            timeout=int(active.timeout_seconds * _MILLISECONDS_PER_SECOND),
        )
    except playwright_timeout_error as exc:
        msg = "fetch_timeout"
        raise MonitorError(
            msg,
            "browser execution exceeded its timeout",
            retryable=True,
        ) from exc
    except playwright_error as exc:
        msg = "browser_navigation_failed"
        raise MonitorError(msg, "browser navigation failed") from exc
    if state.blocked_error:
        raise state.blocked_error
    if response is None:
        msg = "browser_navigation_failed"
        raise MonitorError(msg, "browser produced no main response")
    validated = guard.validate_request(page.url)
    if response.status >= _HTTP_CLIENT_ERROR_STATUS:
        code, retryable = _classify_http_error_status(response.status)
        raise MonitorError(
            code,
            f"browser main response returned HTTP {response.status}",
            retryable=retryable,
        )
    try:
        dom_bytes = page.evaluate(
            "() => new Blob([document.documentElement.outerHTML]).size"
        )
        if not isinstance(dom_bytes, int) or dom_bytes > active.max_rendered_bytes:
            msg = "response_too_large"
            raise MonitorError(msg, "rendered DOM exceeds the size limit")
        rendered = page.content().encode("utf-8")
    except playwright_timeout_error as exc:
        msg = "fetch_timeout"
        raise MonitorError(
            msg,
            "browser execution exceeded its timeout",
            retryable=True,
        ) from exc
    except playwright_error as exc:
        msg = "browser_navigation_failed"
        raise MonitorError(msg, "browser navigation failed") from exc
    if len(rendered) > active.max_rendered_bytes:
        msg = "response_too_large"
        raise MonitorError(msg, "rendered DOM exceeds the size limit")
    return FetchResult(
        result="fetched",
        final_url=validated.url,
        status=response.status,
        content_type="text/html",
        charset="utf-8",
        content_length=len(rendered),
        etag=response.headers.get("etag", "")[:_MAX_VALIDATOR_HEADER_LENGTH],
        last_modified=response.headers.get("last-modified", "")[
            :_MAX_VALIDATOR_HEADER_LENGTH
        ],
        fetched_at=utc_now(),
        redirect_count=0,
        body=rendered,
    )


def fetch_rendered(
    url: str,
    *,
    config: BrowserFetchConfig | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> FetchResult:
    """Fetch and render ``url`` in an ephemeral, policy-constrained browser.

    See the module docstring for the verified-control requirements this
    fails closed on, and the request/response interception this applies.
    Denies (see the called helpers) if browser mode's unmitigated risks
    are not verified, Playwright is unavailable, or navigation/rendering
    fails or violates policy.

    Returns:
        The rendered fetch result.
    """
    active = config or BrowserFetchConfig()
    _ensure_browser_mode_verified(active)
    guard = BrowserNetworkGuard(
        url, allowed_hosts=active.allowed_hosts, resolver=resolver
    )
    playwright_error, playwright_timeout_error, sync_playwright = _import_playwright()
    state = _BrowserFetchState()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=list(_CHROMIUM_LAUNCH_ARGS)
        )
        context = browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )
        context.route("**/*", _make_route_handler(guard, active, state))
        context.on("response", _make_response_handler(guard, active, state))
        page = context.new_page()

        def close_popup(popup: Any) -> None:  # ruff: ignore[any-type] -- see handle_route above
            popup.close()

        page.on("popup", close_popup)
        try:
            return _capture_rendered_page(
                page, guard, active, state, (playwright_error, playwright_timeout_error)
            )
        finally:
            context.close()
            browser.close()
