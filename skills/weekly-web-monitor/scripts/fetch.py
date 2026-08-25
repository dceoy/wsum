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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, cast
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

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUPPORTED_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/pdf",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/plain",
        "text/xml",
    }
)


@dataclass(frozen=True, slots=True)
class FetchConfig:
    timeout_seconds: float = 15.0
    max_total_seconds: float = 60.0
    max_redirects: int = 5
    max_response_bytes: int = 5_000_000
    user_agent: str = "weekly-web-monitor/1.0"

    def __post_init__(self) -> None:
        if not 0.1 <= self.timeout_seconds <= 120:
            raise MonitorError(
                "invalid_configuration", "timeout must be 0.1-120 seconds"
            )
        if not 1.0 <= self.max_total_seconds <= 600.0:
            raise MonitorError(
                "invalid_configuration", "max_total_seconds must be 1-600 seconds"
            )
        if self.max_total_seconds < self.timeout_seconds:
            raise MonitorError(
                "invalid_configuration",
                "max_total_seconds must be at least timeout_seconds",
            )
        if not 0 <= self.max_redirects <= 10:
            raise MonitorError("invalid_configuration", "max_redirects must be 0-10")
        if not 1_024 <= self.max_response_bytes <= 50_000_000:
            raise MonitorError(
                "invalid_configuration", "max_response_bytes must be 1024-50000000"
            )
        if (
            not self.user_agent
            or len(self.user_agent) > 200
            or any(ord(char) < 32 for char in self.user_agent)
        ):
            raise MonitorError("invalid_configuration", "user_agent is invalid")


@dataclass(frozen=True, slots=True)
class FetchResult:
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
            raise TimeoutError("recv exceeded the fetch deadline")
        current = self.gettimeout()  # type: ignore[attr-defined]
        if current is None or current > remaining:
            self.settimeout(remaining)  # type: ignore[attr-defined]

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        self._clamp_to_deadline()
        return super().recv_into(*args, **kwargs)  # type: ignore[misc]

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
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
    """
    original_timeout = sock.gettimeout()
    sock.setblocking(False)
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("TLS handshake exceeded the fetch deadline")
            try:
                sock.do_handshake()
                return
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
    sock._deadline = deadline
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
        raw_socket = _connect_pinned_socket(
            self._address, self.port, self.timeout, self._source_address, self._deadline
        )
        wrapped: ssl.SSLSocket | None = None
        try:
            validate_peer_address(raw_socket.getpeername()[0], self._allowed_addresses)
            # ``wrap_socket`` detaches the raw fd into a brand new socket
            # object, so the raw socket's own deadline-checked recv is never
            # actually exercised over TLS; the wrapped object is what
            # ``http.client`` reads application data through afterward, so
            # that is where the deadline check must live instead. The
            # handshake itself is deferred (``do_handshake_on_connect=False``)
            # and driven separately under the same deadline, since it bypasses
            # this socket's recv overrides entirely (see
            # ``_do_handshake_with_deadline``).
            ssl_context = cast(ssl.SSLContext, self._context)  # pyright: ignore[reportAttributeAccessIssue]
            ssl_context.sslsocket_class = _DeadlineSSLSocket
            wrapped = ssl_context.wrap_socket(
                raw_socket, server_hostname=self.host, do_handshake_on_connect=False
            )
            if wrapped is None:
                raise OSError("TLS context returned no socket")
            wrapped._deadline = self._deadline  # type: ignore[attr-defined]
            _do_handshake_with_deadline(wrapped, self._deadline)
            self.sock = wrapped
            validate_peer_address(self.sock.getpeername()[0], self._allowed_addresses)
        except BaseException:
            # ``wrap_socket`` transfers ownership of the file descriptor to
            # the returned SSLSocket. Closing the detached raw socket after
            # that point is a no-op and leaks the wrapped descriptor when the
            # handshake or the post-handshake peer check fails.
            (wrapped if wrapped is not None else raw_socket).close()
            raise


def _request_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if any(char in path for char in "\r\n"):
        raise MonitorError("network_policy_denied", "request target is malformed")
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
    if status == 429:
        return MonitorError(
            "http_rate_limited", "server returned HTTP 429", retryable=True
        )
    if 500 <= status <= 599:
        return MonitorError(
            "http_server_error", f"server returned HTTP {status}", retryable=True
        )
    return MonitorError("http_client_error", f"server returned HTTP {status}")


def _safe_validator(value: str, field_name: str) -> str:
    bounded = str(value)[:1_000]
    if any(ord(char) < 32 or ord(char) == 127 for char in bounded):
        raise MonitorError(
            "invalid_validator", f"{field_name} contains forbidden control characters"
        )
    return bounded


def _extract_charset(raw_content_type: str) -> str:
    for part in raw_content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            return _safe_validator(value.strip().strip("\"'"), "charset")[:100]
    return ""


class _ResolverJob:
    __slots__ = ("args", "done", "error", "kwargs", "resolver", "result")

    def __init__(
        self, resolver: Resolver, args: tuple[Any, ...], kwargs: dict[str, Any]
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
            except BaseException as exc:  # noqa: BLE001 - returned to caller thread
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
    ) -> Any:
        if budget <= 0:
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        self._ensure_started()
        deadline = monotonic() + budget
        if not self._capacity.acquire(timeout=budget):
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        remaining = deadline - monotonic()
        if remaining <= 0:
            self._capacity.release()
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
        job = _ResolverJob(resolver, args, kwargs)
        self._jobs.put(job)
        if not job.done.wait(remaining):
            # The fixed worker may remain occupied by an uninterruptible
            # getaddrinfo call, but it retains the capacity slot until it exits.
            # Later callers therefore cannot create threads or queue work without
            # bound while the resolver is stuck.
            raise TimeoutError("DNS resolution exceeded the fetch deadline")
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
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return pool.resolve(resolver, args, kwargs, remaining())

    return wrapped


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
    """Fetch a URL while pinning each request to prevalidated public addresses."""

    active_config = config or FetchConfig()
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
        raise MonitorError(
            "invalid_configuration",
            "TLS context must require TLS 1.2+, certificates, "
            "and hostname verification",
        )
    deadline = monotonic() + active_config.max_total_seconds
    bounded_resolver = _bounded_resolver(resolver, lambda: deadline - monotonic())
    target = resolve_public_url(url, resolver=bounded_resolver)
    etag = _safe_validator(etag, "ETag")
    last_modified = _safe_validator(last_modified, "Last-Modified")
    redirects = 0
    while True:
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
                connection.request("GET", _request_path(target.url), headers=headers)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                # Capture the socket now: ``getresponse()`` below calls
                # ``connection.close()`` (nulling ``connection.sock``) as
                # soon as it sees the response will close the connection,
                # which happens on every request here since we always send
                # ``Connection: close``. The underlying socket itself stays
                # open for the response body to use; only the connection's
                # reference to it is cleared.
                sock = connection.sock
                if sock is None:
                    raise OSError("HTTP connection returned no socket")
                sock.settimeout(min(active_config.timeout_seconds, remaining))
                response = connection.getresponse()
                break
            except TimeoutError as exc:
                last_error = exc
                if connection:
                    connection.close()
            except (ssl.SSLError, http.client.HTTPException, OSError) as exc:
                last_error = exc
                if connection:
                    connection.close()
        if response is None or connection is None or sock is None:
            if isinstance(last_error, (TimeoutError, socket.timeout)):
                raise MonitorError(
                    "fetch_timeout", "request exceeded its timeout", retryable=True
                ) from last_error
            if isinstance(last_error, ssl.SSLError):
                raise MonitorError("tls_error", "TLS connection failed") from last_error
            raise MonitorError(
                "fetch_connection_failed",
                "connection failed for every validated address",
                retryable=True,
            ) from last_error
        try:
            status = response.status
            if status in REDIRECT_STATUSES:
                if redirects >= active_config.max_redirects:
                    raise MonitorError(
                        "redirect_limit_exceeded", "maximum redirects exceeded"
                    )
                target = validate_redirect(
                    target.url,
                    response.getheader("Location", ""),
                    resolver=bounded_resolver,
                )
                redirects += 1
                continue
            if status == 304:
                if not sent_conditional_request:
                    raise MonitorError(
                        "unexpected_not_modified",
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
            if not 200 <= status <= 299:
                raise _map_http_error(status)
            if status != 200:
                raise MonitorError(
                    "unsupported_http_status",
                    "GET returned an unsupported successful HTTP status",
                )
            raw_content_type = response.getheader("Content-Type", "")
            content_type = raw_content_type.split(";", 1)[0].strip().lower()
            charset = _extract_charset(raw_content_type)
            if content_type and content_type not in SUPPORTED_CONTENT_TYPES:
                raise MonitorError(
                    "unsupported_content_type",
                    "server returned an unsupported content type",
                )
            content_encoding = response.getheader("Content-Encoding", "")
            if content_encoding.strip().lower() not in {"", "identity"}:
                raise MonitorError(
                    "unsupported_content_encoding",
                    "compressed HTTP content encoding is not supported",
                )
            content_length_header = response.getheader("Content-Length")
            declared_length: int | None = None
            if content_length_header:
                try:
                    declared_length = int(content_length_header)
                except ValueError as exc:
                    raise MonitorError(
                        "malformed_response", "Content-Length header is invalid"
                    ) from exc
                if declared_length < 0:
                    raise MonitorError(
                        "malformed_response", "Content-Length header is invalid"
                    )
                if declared_length > active_config.max_response_bytes:
                    raise MonitorError(
                        "response_too_large", "declared response exceeds the size limit"
                    )
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
                    raise MonitorError(
                        "fetch_timeout",
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
                chunk = response.read1(
                    min(65_536, active_config.max_response_bytes - size + 1)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > active_config.max_response_bytes:
                    raise MonitorError(
                        "response_too_large", "response exceeds the size limit"
                    )
            if declared_length is not None and size != declared_length:
                raise MonitorError(
                    "malformed_response",
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
        except TimeoutError as exc:
            raise MonitorError(
                "fetch_timeout", "response read exceeded its timeout", retryable=True
            ) from exc
        except (ssl.SSLError, OSError) as exc:
            raise MonitorError(
                "fetch_connection_failed",
                "connection failed while reading the response body",
                retryable=True,
            ) from exc
        except http.client.HTTPException as exc:
            raise MonitorError(
                "malformed_response", "server returned a malformed HTTP response"
            ) from exc
        finally:
            connection.close()


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fetch.py URL", file=sys.stderr)
        return 2
    try:
        result = fetch_url(argv[1])
    except MonitorError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False))
        return 1
    print(json.dumps(result.metadata(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
