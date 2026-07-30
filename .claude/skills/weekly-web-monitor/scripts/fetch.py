"""Secure deterministic static HTTP(S) fetcher with DNS and size controls."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from errors import MonitorError
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
    max_redirects: int = 5
    max_response_bytes: int = 5_000_000
    user_agent: str = "weekly-web-monitor/1.0"

    def __post_init__(self) -> None:
        if not 0.1 <= self.timeout_seconds <= 120:
            raise MonitorError(
                "invalid_configuration", "timeout must be 0.1-120 seconds"
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


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        origin_host: str,
        address: str,
        port: int,
        timeout: float,
        allowed_addresses: tuple[str, ...],
    ) -> None:
        super().__init__(origin_host, port=port, timeout=timeout)
        self._address = address
        self._allowed_addresses = allowed_addresses

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
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
    ) -> None:
        super().__init__(origin_host, port=port, timeout=timeout, context=context)
        self._address = address
        self._allowed_addresses = allowed_addresses

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )
        try:
            validate_peer_address(raw_socket.getpeername()[0], self._allowed_addresses)
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
            validate_peer_address(self.sock.getpeername()[0], self._allowed_addresses)
        except BaseException:
            raw_socket.close()
            raise


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
    config: FetchConfig,
    ssl_context: ssl.SSLContext,
) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return _PinnedHTTPSConnection(
            target.host,
            address,
            target.port,
            config.timeout_seconds,
            target.addresses,
            ssl_context,
        )
    return _PinnedHTTPConnection(
        target.host,
        address,
        target.port,
        config.timeout_seconds,
        target.addresses,
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


def fetch_url(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
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
    target = resolve_public_url(url, resolver=resolver)
    etag = _safe_validator(etag, "ETag")
    last_modified = _safe_validator(last_modified, "Last-Modified")
    initial_origin = target.origin
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
        if target.origin == initial_origin:
            if etag:
                headers["If-None-Match"] = etag[:1_000]
            if last_modified:
                headers["If-Modified-Since"] = last_modified[:1_000]
        response: http.client.HTTPResponse | None = None
        connection: http.client.HTTPConnection | None = None
        last_error: Exception | None = None
        for address in target.addresses:
            try:
                connection = _open_connection(
                    target, address, active_config, tls_context
                )
                connection.request("GET", _request_path(target.url), headers=headers)
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
        if response is None or connection is None:
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
                    target.url, response.getheader("Location", ""), resolver=resolver
                )
                redirects += 1
                continue
            if status == 304:
                if not etag and not last_modified:
                    raise MonitorError(
                        "unexpected_not_modified",
                        "server returned HTTP 304 without a conditional request",
                    )
                return FetchResult(
                    result="unchanged",
                    final_url=target.url,
                    status=status,
                    content_type="",
                    content_length=0,
                    etag=_safe_validator(response.getheader("ETag", etag), "ETag"),
                    last_modified=_safe_validator(
                        response.getheader("Last-Modified", last_modified),
                        "Last-Modified",
                    ),
                    fetched_at=_utc_now(),
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
                chunk = response.read(
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
            return FetchResult(
                result="fetched",
                final_url=target.url,
                status=status,
                content_type=content_type,
                content_length=size,
                etag=_safe_validator(response.getheader("ETag", ""), "ETag"),
                last_modified=_safe_validator(
                    response.getheader("Last-Modified", ""), "Last-Modified"
                ),
                fetched_at=_utc_now(),
                redirect_count=redirects,
                body=b"".join(chunks),
            )
        except TimeoutError as exc:
            raise MonitorError(
                "fetch_timeout", "response read exceeded its timeout", retryable=True
            ) from exc
        except http.client.HTTPException as exc:
            raise MonitorError(
                "malformed_response", "server returned a malformed HTTP response"
            ) from exc
        finally:
            connection.close()


def fetch_to_workspace(
    url: str,
    workspace: Path,
    **kwargs: Any,
) -> tuple[FetchResult, Path | None]:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise MonitorError(
            "workspace_invalid", "ephemeral workspace must already be a directory"
        )
    result = fetch_url(url, **kwargs)
    if not result.body:
        return result, None
    output = workspace / "response.bin"
    output.write_bytes(result.body)
    return result, output


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
