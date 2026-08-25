"""Secure deterministic static HTTP(S) fetcher with DNS and size controls."""

from __future__ import annotations

import http.client
import json
import queue
import select
import socket
import ssl
import sys
import threading
from dataclasses import asdict, dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, NoReturn, cast
from urllib.parse import urlsplit

from errors import MonitorError
from models import utc_now
from network_policy import (
    ResolvedTarget,
    Resolver,
    resolve_public_url,
    validate_peer_address,
    validate_redirect,
)

if TYPE_CHECKING:
    from collections.abc import Callable

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUPPORTED_CONTENT_TYPES = frozenset({
    "application/atom+xml",
    "application/pdf",
    "application/rss+xml",
    "application/xhtml+xml",
    "application/xml",
    "text/html",
    "text/plain",
    "text/xml",
})

_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 120
_MIN_TOTAL_SECONDS = 1.0
_MAX_TOTAL_SECONDS = 600.0
_MAX_REDIRECTS_LIMIT = 10
_MIN_RESPONSE_BYTES = 1_024
_MAX_RESPONSE_BYTES_LIMIT = 50_000_000
_MAX_USER_AGENT_LENGTH = 200
_MIN_PRINTABLE_CODEPOINT = 32
_DEL_CODEPOINT = 127
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 599
_HTTP_NOT_MODIFIED = 304
_HTTP_OK = 200
_HTTP_SUCCESS_MAX = 299


@dataclass(frozen=True, slots=True)
class FetchConfig:
    """Validated limits and identity used for a single ``fetch_url`` call."""

    timeout_seconds: float = 15.0
    max_total_seconds: float = 60.0
    max_redirects: int = 5
    max_response_bytes: int = 5_000_000
    user_agent: str = "weekly-web-monitor/1.0"

    def __post_init__(self) -> None:
        """Validate every field's bounds.

        Raises:
            MonitorError: If any field is outside its allowed bounds.
        """
        if not _MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            msg = "invalid_configuration"
            raise MonitorError(msg, "timeout must be 0.1-120 seconds")
        if not _MIN_TOTAL_SECONDS <= self.max_total_seconds <= _MAX_TOTAL_SECONDS:
            msg = "invalid_configuration"
            raise MonitorError(msg, "max_total_seconds must be 1-600 seconds")
        if self.max_total_seconds < self.timeout_seconds:
            msg = "invalid_configuration"
            raise MonitorError(
                msg,
                "max_total_seconds must be at least timeout_seconds",
            )
        if not 0 <= self.max_redirects <= _MAX_REDIRECTS_LIMIT:
            msg = "invalid_configuration"
            raise MonitorError(msg, "max_redirects must be 0-10")
        if not (
            _MIN_RESPONSE_BYTES <= self.max_response_bytes <= _MAX_RESPONSE_BYTES_LIMIT
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "max_response_bytes must be 1024-50000000")
        if (
            not self.user_agent
            or len(self.user_agent) > _MAX_USER_AGENT_LENGTH
            or any(ord(char) < _MIN_PRINTABLE_CODEPOINT for char in self.user_agent)
        ):
            msg = "invalid_configuration"
            raise MonitorError(msg, "user_agent is invalid")


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A single fetch attempt's outcome: a fresh body or an unchanged signal."""

    result: str
    final_url: str
    status: int
    content_type: str
    charset: str
    content_length: int
    etag: str
    last_modified: str
    fetched_at: str
    redirect_count: int
    body: bytes = b""

    def metadata(self) -> dict[str, Any]:
        """Return this result as JSON-serializable metadata, without the body.

        Returns:
            This result's fields, minus ``body`` (kept out of metadata so
            fetched content is never accidentally logged or persisted
            alongside bookkeeping).
        """
        value = asdict(self)
        value.pop("body")
        return value


class _DeadlineTrackingMixin:
    """Reject every underlying recv once the fetch's total deadline has passed.

    ``http.client``'s buffered response file performs its own internal
    ``readline()``/``read()`` loops when parsing headers, chunk-size lines,
    and trailers, independent of ``read1()``'s single-recv-per-call
    guarantee used for the body. Each of those internal recvs can complete
    well inside the per-op socket timeout, so a peer trickling bytes one at
    a time (e.g. a padded chunk-extension on the chunk-size line) can stall
    past ``max_total_seconds`` without ever tripping a single recv's
    timeout. Clamping the socket's own timeout to whatever remains of the
    deadline before every real recv closes that gap for headers, chunk
    framing, and trailers alike -- including a recv that blocks completely
    (no bytes at all), since that recv's own timeout can then never exceed
    the remaining budget, unlike a per-op timeout set once before a call
    that can perform many such recvs.
    """

    _deadline: float = float("inf")

    def _clamp_to_deadline(self) -> None:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            msg = "recv exceeded the fetch deadline"
            raise TimeoutError(msg)
        current = self.gettimeout()  # type: ignore[attr-defined]
        if current is None or current > remaining:
            self.settimeout(remaining)  # type: ignore[attr-defined]

    def recv_into(
        self,
        *args: Any,  # ruff: ignore[any-type] -- overrides socket.recv_into's own untyped signature
        **kwargs: Any,  # ruff: ignore[any-type]
    ) -> int:
        """Clamp the socket timeout to the deadline, then delegate to the base class.

        Returns:
            The number of bytes read, as returned by the base class.
        """
        self._clamp_to_deadline()
        return super().recv_into(*args, **kwargs)  # type: ignore[misc]

    def recv(
        self,
        *args: Any,  # ruff: ignore[any-type] -- overrides socket.recv's own untyped signature
        **kwargs: Any,  # ruff: ignore[any-type]
    ) -> bytes:
        """Clamp the socket timeout to the deadline, then delegate to the base class.

        Returns:
            The bytes read, as returned by the base class.
        """
        self._clamp_to_deadline()
        return super().recv(*args, **kwargs)  # type: ignore[misc]


class _DeadlineSocket(_DeadlineTrackingMixin, socket.socket):
    pass


class _DeadlineSSLSocket(_DeadlineTrackingMixin, ssl.SSLSocket):
    pass


def _do_handshake_with_deadline(sock: ssl.SSLSocket, deadline: float) -> None:
    """Drive the TLS handshake without letting it run past ``deadline``.

    ``SSLSocket.do_handshake()`` talks to the raw fd directly through
    OpenSSL, bypassing the ``recv``/``recv_into`` overrides that clamp
    every other read on this socket to the remaining fetch deadline. A
    peer that trickles handshake records so each individual read completes
    quickly could otherwise let a single blocking ``do_handshake()`` call
    run for the connection's whole per-op timeout regardless of how much
    of the total deadline is actually left. Driving the handshake through
    a manual non-blocking loop, reclamping the wait on every iteration,
    closes that gap the same way the recv overrides do for body reads.

    Raises:
        TimeoutError: If the handshake does not complete before ``deadline``.
    """
    original_timeout = sock.gettimeout()
    sock.setblocking(False)  # ruff: ignore[boolean-positional-value-in-call] -- stdlib API is positional-only
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                msg = "TLS handshake exceeded the fetch deadline"
                raise TimeoutError(msg)
            try:
                sock.do_handshake()
            except ssl.SSLWantReadError:
                readable, _, _ = select.select([sock], [], [], remaining)
                if not readable:
                    msg = "TLS handshake exceeded the fetch deadline"
                    raise TimeoutError(msg) from None
            except ssl.SSLWantWriteError:
                _, writable, _ = select.select([], [sock], [], remaining)
                if not writable:
                    msg = "TLS handshake exceeded the fetch deadline"
                    raise TimeoutError(msg) from None
            else:
                return
    finally:
        sock.settimeout(original_timeout)


def _connect_pinned_socket(
    address: str,
    port: int,
    timeout: float | None,
    source_address: tuple[str, int] | None,
    deadline: float,
) -> socket.socket:
    # ``address`` is always a pre-resolved, already-validated IP literal, so
    # this performs no network I/O; it mirrors ``socket.create_connection``'s
    # own family/sockaddr derivation (including IPv6 flowinfo/scope_id)
    # instead of guessing the family from the address string's shape.
    family, socktype, proto, _, sockaddr = socket.getaddrinfo(
        address, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
    )[0]
    sock = _DeadlineSocket(family, socktype, proto)
    sock._deadline = deadline  # ruff: ignore[private-member-access] -- own mixin attribute  # pyright: ignore[reportPrivateUsage]
    sock.settimeout(timeout)
    try:
        if source_address:
            sock.bind(source_address)
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        origin_host: str,
        address: str,
        port: int,
        timeout: float,
        allowed_addresses: tuple[str, ...],
        deadline: float,
    ) -> None:
        super().__init__(origin_host, port=port, timeout=timeout)
        self._address = address
        self._allowed_addresses = allowed_addresses
        self._deadline = deadline
        self._source_address: tuple[str, int] | None = None

    def connect(self) -> None:
        self.sock = _connect_pinned_socket(
            self._address, self.port, self.timeout, self._source_address, self._deadline
        )
        validate_peer_address(self.sock.getpeername()[0], self._allowed_addresses)


def _create_wrapped_socket(
    raw_socket: socket.socket,
    ssl_context: ssl.SSLContext,
    host: str,
) -> ssl.SSLSocket:
    """Wrap ``raw_socket`` in TLS with a deadline-checked recv, deferring the handshake.

    ``wrap_socket`` detaches the raw fd into a brand new socket object, so
    the raw socket's own deadline-checked recv is never actually exercised
    over TLS; the wrapped object is what ``http.client`` reads application
    data through afterward, so that is where the deadline check must live
    instead. The handshake itself is deferred (``do_handshake_on_connect=False``)
    to :func:`_handshake_wrapped_socket`, since a caller that has not yet
    seen this function return has no reference through which to close the
    detached descriptor on a handshake failure.

    Returns:
        The wrapped TLS socket, handshake not yet started.

    Raises:
        OSError: If the TLS context returns no socket.
    """
    ssl_context.sslsocket_class = _DeadlineSSLSocket
    wrapped = ssl_context.wrap_socket(
        raw_socket, server_hostname=host, do_handshake_on_connect=False
    )
    if (
        wrapped is None  # pyright: ignore[reportUnnecessaryComparison]
        # ``sslsocket_class`` is set just above, so runtime behavior is not
        # fully covered by typeshed's overloads for the default case; this
        # stays a load-bearing defensive check.
    ):
        msg = "TLS context returned no socket"
        raise OSError(msg)
    return wrapped


def _handshake_wrapped_socket(wrapped: ssl.SSLSocket, deadline: float) -> None:
    """Complete the deferred TLS handshake on an already-wrapped socket.

    Raises via :func:`_do_handshake_with_deadline` (``TimeoutError``) if the
    handshake does not complete before ``deadline``.
    """
    wrapped._deadline = deadline  # ruff: ignore[private-member-access] -- own mixin attribute  # type: ignore[attr-defined]
    _do_handshake_with_deadline(wrapped, deadline)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        origin_host: str,
        address: str,
        port: int,
        timeout: float,
        allowed_addresses: tuple[str, ...],
        context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        super().__init__(origin_host, port=port, timeout=timeout, context=context)
        self._address = address
        self._allowed_addresses = allowed_addresses
        self._deadline = deadline
        self._source_address: tuple[str, int] | None = None

    def connect(self) -> None:
        """Connect to a prevalidated address and complete a pinned TLS handshake.

        Denies (see :func:`validate_peer_address`) if the peer address is
        not among the allowed (prevalidated) addresses, and propagates
        whatever :func:`_create_wrapped_socket`/:func:`_handshake_wrapped_socket`
        raise for a failed wrap or handshake.
        """
        raw_socket = _connect_pinned_socket(
            self._address, self.port, self.timeout, self._source_address, self._deadline
        )
        try:
            validate_peer_address(raw_socket.getpeername()[0], self._allowed_addresses)
            ssl_context = cast(
                "ssl.SSLContext",
                self._context,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            )
            # Assigned before the handshake (not after) so that a handshake
            # failure still leaves ``self.sock`` pointing at the socket that
            # now solely owns the (by now detached) file descriptor -- the
            # except block below closes whichever of the two actually
            # received it.
            self.sock = _create_wrapped_socket(raw_socket, ssl_context, self.host)
            _handshake_wrapped_socket(self.sock, self._deadline)
            validate_peer_address(self.sock.getpeername()[0], self._allowed_addresses)
        except BaseException:
            (self.sock if self.sock is not None else raw_socket).close()
            raise


def _request_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if any(char in path for char in "\r\n"):
        msg = "network_policy_denied"
        raise MonitorError(msg, "request target is malformed")
    return path


def _host_header(target: ResolvedTarget) -> str:
    display_host = f"[{target.host}]" if ":" in target.host else target.host
    default_port = 443 if target.scheme == "https" else 80
    return (
        display_host if target.port == default_port else f"{display_host}:{target.port}"
    )


def _open_connection(
    target: ResolvedTarget,
    address: str,
    timeout: float,
    ssl_context: ssl.SSLContext,
    deadline: float,
) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return _PinnedHTTPSConnection(
            target.host,
            address,
            target.port,
            timeout,
            target.addresses,
            ssl_context,
            deadline,
        )
    return _PinnedHTTPConnection(
        target.host,
        address,
        target.port,
        timeout,
        target.addresses,
        deadline,
    )


def _map_http_error(status: int) -> MonitorError:
    """Map an unsuccessful HTTP status to the appropriate :class:`MonitorError`.

    Returns:
        A retryable error for HTTP 429 or 5xx, otherwise a non-retryable one.
    """
    if status == _HTTP_TOO_MANY_REQUESTS:
        return MonitorError(
            "http_rate_limited", "server returned HTTP 429", retryable=True
        )
    if _HTTP_SERVER_ERROR_MIN <= status <= _HTTP_SERVER_ERROR_MAX:
        return MonitorError(
            "http_server_error", f"server returned HTTP {status}", retryable=True
        )
    return MonitorError("http_client_error", f"server returned HTTP {status}")


def _safe_validator(value: str, field_name: str) -> str:
    """Bound and reject a validator header value containing control characters.

    Returns:
        ``value``, truncated to 1000 characters.

    Raises:
        MonitorError: If the bounded value contains a control character.
    """
    bounded = str(value)[:1_000]
    if any(
        ord(char) < _MIN_PRINTABLE_CODEPOINT or ord(char) == _DEL_CODEPOINT
        for char in bounded
    ):
        msg = "invalid_validator"
        raise MonitorError(msg, f"{field_name} contains forbidden control characters")
    return bounded


def _extract_charset(raw_content_type: str) -> str:
    for part in raw_content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            return _safe_validator(value.strip().strip("\"'"), "charset")[:100]
    return ""


class _ResolverJob:
    """A queued resolver call, its outcome, and the event signaling completion.

    ``resolver`` mirrors ``socket.getaddrinfo``'s own dynamically typed
    signature, so its args/kwargs/result stay ``Any`` here by necessity.
    """

    __slots__ = ("args", "done", "error", "kwargs", "resolver", "result")

    def __init__(
        self,
        resolver: Resolver,
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
    """A fixed daemon-worker pool with no unbounded pending-job queue."""

    def __init__(self, worker_count: int) -> None:
        self._worker_count = worker_count
        self._capacity = threading.BoundedSemaphore(worker_count)
        self._jobs: queue.SimpleQueue[_ResolverJob] = queue.SimpleQueue()
        self._workers: list[threading.Thread] = []
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
                    name=f"weekly-web-monitor-resolver-{index}",
                )
                worker.start()
                self._workers.append(worker)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job.result = job.resolver(*job.args, **job.kwargs)
            except BaseException as exc:  # ruff: ignore[blind-except] - returned to caller thread
                job.error = exc
            finally:
                job.done.set()
                self._capacity.release()

    def resolve(
        self,
        resolver: Resolver,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        budget: float,
    ) -> Any:  # ruff: ignore[any-type]
        """Run ``resolver(*args, **kwargs)`` on the pool within ``budget`` seconds.

        Returns:
            Whatever ``resolver`` returns.

        Raises:
            TimeoutError: If ``budget`` is exhausted before the pool has
                capacity, or before the call itself completes.
        """
        if budget <= 0:
            msg = "DNS resolution exceeded the fetch deadline"
            raise TimeoutError(msg)
        self._ensure_started()
        deadline = monotonic() + budget
        if not self._capacity.acquire(timeout=budget):
            msg = "DNS resolution exceeded the fetch deadline"
            raise TimeoutError(msg)
        remaining = deadline - monotonic()
        if remaining <= 0:
            self._capacity.release()
            msg = "DNS resolution exceeded the fetch deadline"
            raise TimeoutError(msg)
        job = _ResolverJob(resolver, args, kwargs)
        self._jobs.put(job)
        if not job.done.wait(remaining):
            # The fixed worker may remain occupied by an uninterruptible
            # getaddrinfo call, but it retains the capacity slot until it exits.
            # Later callers therefore cannot create threads or queue work without
            # bound while the resolver is stuck.
            msg = "DNS resolution exceeded the fetch deadline"
            raise TimeoutError(msg)
        if job.error is not None:
            raise job.error
        return job.result


_RESOLVER_POOL = _ResolverPool(worker_count=4)


def _bounded_resolver(
    resolver: Resolver,
    remaining: Callable[[], float],
    *,
    pool: _ResolverPool = _RESOLVER_POOL,
) -> Resolver:
    """Bound a resolver call to the fetch's total deadline.

    ``socket.getaddrinfo`` has no timeout parameter and cannot be interrupted.
    Calls run in a fixed process-wide daemon pool whose capacity stays occupied
    until a timed-out resolver really returns, so repeated timeouts cannot create
    an unbounded number of threads or queued jobs.

    Returns:
        A wrapped resolver with the same call signature as ``resolver``.
    """

    def wrapped(
        *args: Any,  # ruff: ignore[any-type]
        **kwargs: Any,  # ruff: ignore[any-type]
    ) -> Any:  # ruff: ignore[any-type]
        return pool.resolve(resolver, args, kwargs, remaining())

    return wrapped


def _resolve_tls_context(ssl_context: ssl.SSLContext | None) -> ssl.SSLContext:
    """Return a validated TLS context, building the secure default if unset.

    Returns:
        A TLS context that requires TLS 1.2+, certificates, and hostname
        verification.

    Raises:
        MonitorError: If a caller-supplied context does not meet that bar.
    """
    if ssl_context is None:
        tls_context = ssl.create_default_context()
        # Python/OpenSSL builds differ in how they report the default lower
        # bound. Set the policy explicitly so secure defaults are portable.
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    else:
        tls_context = ssl_context
    if (
        tls_context.verify_mode != ssl.CERT_REQUIRED
        or not tls_context.check_hostname
        or tls_context.minimum_version < ssl.TLSVersion.TLSv1_2
    ):
        msg = "invalid_configuration"
        raise MonitorError(
            msg,
            "TLS context must require TLS 1.2+, certificates, "
            "and hostname verification",
        )
    return tls_context


def _build_request_headers(
    target: ResolvedTarget,
    active_config: FetchConfig,
    validated_url: str,
    etag: str,
    last_modified: str,
) -> tuple[dict[str, str], bool]:
    """Build this request's headers, adding conditional headers when eligible.

    Returns:
        The headers, and whether a conditional (If-None-Match /
        If-Modified-Since) request was sent.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/pdf,"
        "application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Host": _host_header(target),
        "User-Agent": active_config.user_agent,
    }
    if target.url == validated_url:
        if etag:
            headers["If-None-Match"] = etag[:1_000]
        if last_modified:
            headers["If-Modified-Since"] = last_modified[:1_000]
    sent_conditional_request = (
        "If-None-Match" in headers or "If-Modified-Since" in headers
    )
    return headers, sent_conditional_request


def _check_time_remaining(remaining: float) -> None:
    """Raise if no time remains before the fetch deadline.

    Raises:
        TimeoutError: If ``remaining`` is not positive.
    """
    if remaining <= 0:
        raise TimeoutError


def _require_connection_socket(sock: socket.socket | None) -> socket.socket:
    """Return the connection's socket, or raise if it is unexpectedly absent.

    Returns:
        The non-None socket.

    Raises:
        OSError: If the connection returned no socket.
    """
    if sock is None:
        msg = "HTTP connection returned no socket"
        raise OSError(msg)
    return sock


def _send_request_and_get_response(
    connection: http.client.HTTPConnection,
    target: ResolvedTarget,
    headers: dict[str, str],
    active_config: FetchConfig,
    deadline: float,
) -> tuple[http.client.HTTPResponse, socket.socket]:
    """Send the GET request on ``connection`` and capture its response and socket.

    Raises via :func:`_check_time_remaining` if the deadline is exceeded
    before the request completes, and via :func:`_require_connection_socket`
    if the connection unexpectedly has no socket.

    Returns:
        The response and the socket it was read from.
    """
    connection.request("GET", _request_path(target.url), headers=headers)
    remaining = deadline - monotonic()
    _check_time_remaining(remaining)
    # Capture the socket now: ``getresponse()`` below calls
    # ``connection.close()`` (nulling ``connection.sock``) as
    # soon as it sees the response will close the connection,
    # which happens on every request here since we always send
    # ``Connection: close``. The underlying socket itself stays
    # open for the response body to use; only the connection's
    # reference to it is cleared.
    sock = _require_connection_socket(connection.sock)
    sock.settimeout(min(active_config.timeout_seconds, remaining))
    response = connection.getresponse()
    return response, sock


def _connect_for_request(
    target: ResolvedTarget,
    headers: dict[str, str],
    active_config: FetchConfig,
    tls_context: ssl.SSLContext,
    deadline: float,
) -> tuple[
    http.client.HTTPResponse | None,
    http.client.HTTPConnection | None,
    socket.socket | None,
    Exception | None,
]:
    """Try every prevalidated address in turn until one connects and responds.

    Returns:
        The response, connection, and socket for whichever address
        succeeded first (all ``None`` if every attempt failed), and the
        last error seen (``None`` only if an attempt succeeded).
    """
    response: http.client.HTTPResponse | None = None
    connection: http.client.HTTPConnection | None = None
    sock: socket.socket | None = None
    last_error: Exception | None = None
    for address in target.addresses:
        remaining = deadline - monotonic()
        if remaining <= 0:
            last_error = TimeoutError()
            break
        try:
            connection = _open_connection(
                target,
                address,
                min(active_config.timeout_seconds, remaining),
                tls_context,
                deadline,
            )
            response, sock = _send_request_and_get_response(
                connection, target, headers, active_config, deadline
            )
            break
        except TimeoutError as exc:
            last_error = exc
            if connection:
                connection.close()
        except (ssl.SSLError, http.client.HTTPException, OSError) as exc:
            last_error = exc
            if connection:
                connection.close()
    return response, connection, sock, last_error


def _raise_connection_failure(last_error: Exception | None) -> NoReturn:
    """Raise the appropriate MonitorError for a connection that never succeeded.

    Raises:
        MonitorError: Always -- classified by the type of ``last_error``.
    """
    if isinstance(last_error, (TimeoutError, socket.timeout)):
        msg = "fetch_timeout"
        raise MonitorError(
            msg, "request exceeded its timeout", retryable=True
        ) from last_error
    if isinstance(last_error, ssl.SSLError):
        msg = "tls_error"
        raise MonitorError(msg, "TLS connection failed") from last_error
    msg = "fetch_connection_failed"
    raise MonitorError(
        msg,
        "connection failed for every validated address",
        retryable=True,
    ) from last_error


@dataclass(slots=True)
class _RedirectOutcome:
    """A followed redirect: the new target and the updated redirect count."""

    target: ResolvedTarget
    redirects: int


def _handle_redirect(
    response: http.client.HTTPResponse,
    target: ResolvedTarget,
    redirects: int,
    active_config: FetchConfig,
    bounded_resolver: Resolver,
) -> _RedirectOutcome:
    """Validate and follow one HTTP redirect.

    Returns:
        The new target and incremented redirect count.

    Raises:
        MonitorError: If the redirect limit has already been reached, or
            (via :func:`validate_redirect`) if the redirect target itself
            fails network-policy validation.
    """
    if redirects >= active_config.max_redirects:
        msg = "redirect_limit_exceeded"
        raise MonitorError(msg, "maximum redirects exceeded")
    new_target = validate_redirect(
        target.url,
        response.getheader("Location", ""),
        resolver=bounded_resolver,
    )
    return _RedirectOutcome(target=new_target, redirects=redirects + 1)


def _handle_not_modified(
    response: http.client.HTTPResponse,
    status: int,
    target: ResolvedTarget,
    etag: str,
    last_modified: str,
    *,
    sent_conditional_request: bool,
    redirects: int,
) -> FetchResult:
    """Build the "unchanged" result for an HTTP 304 response.

    Returns:
        A :class:`FetchResult` with ``result="unchanged"``.

    Raises:
        MonitorError: If a 304 arrives without having sent a conditional
            request.
    """
    if not sent_conditional_request:
        msg = "unexpected_not_modified"
        raise MonitorError(
            msg,
            "server returned HTTP 304 without a conditional request",
        )
    return FetchResult(
        result="unchanged",
        final_url=target.url,
        status=status,
        content_type="",
        charset="",
        content_length=0,
        etag=_safe_validator(response.getheader("ETag", etag), "ETag"),
        last_modified=_safe_validator(
            response.getheader("Last-Modified", last_modified),
            "Last-Modified",
        ),
        fetched_at=utc_now(),
        redirect_count=redirects,
    )


def _check_success_status(status: int) -> None:
    """Raise unless ``status`` is a supported successful (200) HTTP status.

    Raises:
        _map_http_error: If ``status`` is unsuccessful.
        MonitorError: If ``status`` is successful but unsupported (any
            2xx other than 200).
    """
    if not _HTTP_OK <= status <= _HTTP_SUCCESS_MAX:
        raise _map_http_error(status)
    if status != _HTTP_OK:
        msg = "unsupported_http_status"
        raise MonitorError(
            msg,
            "GET returned an unsupported successful HTTP status",
        )


def _parse_response_headers(response: http.client.HTTPResponse) -> tuple[str, str]:
    """Parse and validate the response's Content-Type and Content-Encoding.

    Returns:
        The lowercase content type (without parameters) and its charset.

    Raises:
        MonitorError: If the content type or content encoding is unsupported.
    """
    raw_content_type = response.getheader("Content-Type", "")
    content_type = raw_content_type.split(";", 1)[0].strip().lower()
    charset = _extract_charset(raw_content_type)
    if content_type and content_type not in SUPPORTED_CONTENT_TYPES:
        msg = "unsupported_content_type"
        raise MonitorError(
            msg,
            "server returned an unsupported content type",
        )
    content_encoding = response.getheader("Content-Encoding", "")
    if content_encoding.strip().lower() not in {"", "identity"}:
        msg = "unsupported_content_encoding"
        raise MonitorError(
            msg,
            "compressed HTTP content encoding is not supported",
        )
    return content_type, charset


def _parse_declared_length(
    response: http.client.HTTPResponse, active_config: FetchConfig
) -> int | None:
    """Parse and validate the Content-Length header, if present.

    Returns:
        The declared length, or ``None`` if the header is absent.

    Raises:
        MonitorError: If the header is malformed, negative, or exceeds the
            configured response size limit.
    """
    content_length_header = response.getheader("Content-Length")
    if not content_length_header:
        return None
    try:
        declared_length = int(content_length_header)
    except ValueError as exc:
        msg = "malformed_response"
        raise MonitorError(msg, "Content-Length header is invalid") from exc
    if declared_length < 0:
        msg = "malformed_response"
        raise MonitorError(msg, "Content-Length header is invalid")
    if declared_length > active_config.max_response_bytes:
        msg = "response_too_large"
        raise MonitorError(msg, "declared response exceeds the size limit")
    return declared_length


def _read_response_body(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    active_config: FetchConfig,
    deadline: float,
) -> tuple[list[bytes], int]:
    """Read the response body under the fetch deadline and size limit.

    Returns:
        The body's chunks and their total size.

    Raises:
        MonitorError: If the read exceeds the deadline, or the response
            exceeds the configured size limit.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        # ``read1()`` closes the response (and its socket) as soon
        # as the last byte of a known Content-Length is consumed,
        # not on a subsequent empty read. Check first so the
        # deadline check and settimeout below never touch a socket
        # the response has already torn down.
        if response.isclosed():
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            msg = "fetch_timeout"
            raise MonitorError(
                msg,
                "response read exceeded the total request deadline",
                retryable=True,
            )
        sock.settimeout(min(active_config.timeout_seconds, remaining))
        # ``read()`` loops internally (via the buffered socket file
        # object) until the requested amount is filled or EOF, which
        # can span many socket recvs without ever rechecking the
        # deadline below. ``read1()`` performs at most one
        # underlying recv and returns whatever is currently
        # available, so the deadline is rechecked after every actual
        # socket read.
        chunk = response.read1(min(65_536, active_config.max_response_bytes - size + 1))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > active_config.max_response_bytes:
            msg = "response_too_large"
            raise MonitorError(msg, "response exceeds the size limit")
    return chunks, size


def _build_fetched_result(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    target: ResolvedTarget,
    status: int,
    active_config: FetchConfig,
    deadline: float,
    redirects: int,
) -> FetchResult:
    """Read and validate a successful (HTTP 200) response into a FetchResult.

    Returns:
        The fetched result, including its body.

    Raises:
        MonitorError: If any header or the body fails validation (via
            :func:`_parse_response_headers`, :func:`_parse_declared_length`,
            and :func:`_read_response_body`), or the body's actual length
            does not match a declared Content-Length.
    """
    content_type, charset = _parse_response_headers(response)
    declared_length = _parse_declared_length(response, active_config)
    chunks, size = _read_response_body(response, sock, active_config, deadline)
    if declared_length is not None and size != declared_length:
        msg = "malformed_response"
        raise MonitorError(
            msg,
            "response body length does not match declared Content-Length",
            retryable=True,
        )
    return FetchResult(
        result="fetched",
        final_url=target.url,
        status=status,
        content_type=content_type,
        charset=charset,
        content_length=size,
        etag=_safe_validator(response.getheader("ETag", ""), "ETag"),
        last_modified=_safe_validator(
            response.getheader("Last-Modified", ""), "Last-Modified"
        ),
        fetched_at=utc_now(),
        redirect_count=redirects,
        body=b"".join(chunks),
    )


@dataclass(slots=True)
class _ResponseContext:
    """Everything needed to classify one HTTP response, besides the response itself."""

    target: ResolvedTarget
    redirects: int
    active_config: FetchConfig
    etag: str
    last_modified: str
    sent_conditional_request: bool
    bounded_resolver: Resolver
    deadline: float


def _process_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    sock: socket.socket,
    ctx: _ResponseContext,
) -> FetchResult | _RedirectOutcome:
    """Classify and handle one HTTP response: redirect, unchanged, or fetched.

    Always closes ``connection`` before returning or raising.

    Returns:
        A :class:`_RedirectOutcome` to follow, or the final
        :class:`FetchResult`.

    Raises:
        MonitorError: If the response is a redirect past the limit, an
            invalid 304, an unsuccessful or unsupported status, or the body
            read fails or times out.
    """
    try:
        status = response.status
        if status in REDIRECT_STATUSES:
            return _handle_redirect(
                response,
                ctx.target,
                ctx.redirects,
                ctx.active_config,
                ctx.bounded_resolver,
            )
        if status == _HTTP_NOT_MODIFIED:
            return _handle_not_modified(
                response,
                status,
                ctx.target,
                ctx.etag,
                ctx.last_modified,
                sent_conditional_request=ctx.sent_conditional_request,
                redirects=ctx.redirects,
            )
        _check_success_status(status)
        return _build_fetched_result(
            response,
            sock,
            ctx.target,
            status,
            ctx.active_config,
            ctx.deadline,
            ctx.redirects,
        )
    except TimeoutError as exc:
        msg = "fetch_timeout"
        raise MonitorError(
            msg, "response read exceeded its timeout", retryable=True
        ) from exc
    except (ssl.SSLError, OSError) as exc:
        msg = "fetch_connection_failed"
        raise MonitorError(
            msg,
            "connection failed while reading the response body",
            retryable=True,
        ) from exc
    except http.client.HTTPException as exc:
        msg = "malformed_response"
        raise MonitorError(msg, "server returned a malformed HTTP response") from exc
    finally:
        connection.close()


def fetch_url(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    validated_url: str = "",
    config: FetchConfig | None = None,
    resolver: Resolver = socket.getaddrinfo,
    ssl_context: ssl.SSLContext | None = None,
) -> FetchResult:
    """Fetch a URL while pinning each request to prevalidated public addresses.

    Propagates a ``MonitorError`` (see :func:`_resolve_tls_context`) if the
    TLS context is misconfigured, from address resolution if it fails, from
    :func:`_raise_connection_failure` if every connection attempt fails, and
    from :func:`_process_response` if the response is a redirect past the
    limit, an invalid 304, unsuccessful, unsupported, malformed, or exceeds
    size/time limits.

    Returns:
        The fetch outcome: freshly fetched content, or an "unchanged"
        signal for a valid conditional (ETag/Last-Modified) request.
    """
    active_config = config or FetchConfig()
    tls_context = _resolve_tls_context(ssl_context)
    deadline = monotonic() + active_config.max_total_seconds
    bounded_resolver = _bounded_resolver(resolver, lambda: deadline - monotonic())
    target = resolve_public_url(url, resolver=bounded_resolver)
    etag = _safe_validator(etag, "ETag")
    last_modified = _safe_validator(last_modified, "Last-Modified")
    redirects = 0
    while True:
        headers, sent_conditional_request = _build_request_headers(
            target, active_config, validated_url, etag, last_modified
        )
        response, connection, sock, last_error = _connect_for_request(
            target, headers, active_config, tls_context, deadline
        )
        if response is None or connection is None or sock is None:
            _raise_connection_failure(last_error)
        outcome = _process_response(
            response,
            connection,
            sock,
            _ResponseContext(
                target=target,
                redirects=redirects,
                active_config=active_config,
                etag=etag,
                last_modified=last_modified,
                sent_conditional_request=sent_conditional_request,
                bounded_resolver=bounded_resolver,
                deadline=deadline,
            ),
        )
        if isinstance(outcome, _RedirectOutcome):
            target = outcome.target
            redirects = outcome.redirects
            continue
        return outcome


_EXPECTED_ARGC = 2


def _main(argv: list[str]) -> int:
    """Run the CLI entry point: fetch the URL named in ``argv[1]``.

    On success, writes the JSON-encoded :meth:`FetchResult.metadata` to
    stdout. On a handled failure, writes ``{"error": ...}`` JSON to stdout
    instead. Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if the fetch fails, 2 for incorrect CLI usage.
    """
    if len(argv) != _EXPECTED_ARGC:
        sys.stderr.write("usage: fetch.py URL\n")
        return 2
    try:
        result = fetch_url(argv[1])
    except MonitorError as exc:
        json.dump({"error": exc.as_dict()}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    json.dump(result.metadata(), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
