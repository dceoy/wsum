"""Deny-by-default URL and address validation for untrusted targets."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import (
    SplitResult,
    parse_qsl,
    quote,
    unquote,
    urljoin,
    urlsplit,
    urlunsplit,
)

from errors import MonitorError

Resolver = Callable[..., Sequence[tuple]]

_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access-token",
        "api-key",
        "apikey",
        "auth",
        "auth-token",
        "authorization",
        "awsaccesskeyid",
        "credential",
        "password",
        "secret",
        "sig",
        "signature",
        "subscription-key",
        "token",
        "x-api-key",
    }
)
_SENSITIVE_QUERY_SUFFIXES = (
    "credential",
    "secret",
    "signature",
    "security-token",
    "session-token",
)


_SEPARATOR_RUN = re.compile(r"[\s._-]+")


def is_sensitive_query_name(name: str) -> bool:
    """True for exact credential names and provider-prefixed signed-URL params.

    Provider signed-URL schemes namespace their credential/signature params
    under a prefix (``X-Amz-Credential``, ``X-Amz-Signature``,
    ``X-Goog-Signature``, ...) that an exact-name check misses entirely, so
    those are matched by suffix instead of by exact name. Runs of whitespace,
    dots, underscores, and hyphens are collapsed to a single ``-`` before
    both checks so ``client_secret``, ``client-secret``, ``client secret``,
    and ``x.api.key`` are all treated as the same name as the underscore/
    hyphen form the set happens to spell out. ``parse_qsl`` decodes
    ``%20`` to a literal space, so this also covers percent-encoded
    separators without any extra decoding step.
    """
    normalized = _SEPARATOR_RUN.sub("-", name.strip().lower())
    if normalized in _SENSITIVE_QUERY_NAMES:
        return True
    return normalized.endswith(_SENSITIVE_QUERY_SUFFIXES)


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


def _deny(message: str) -> MonitorError:
    return MonitorError("network_policy_denied", message)


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global) and not any(
        (
            address.is_link_local,
            address.is_loopback,
            address.is_multicast,
            address.is_private,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _normalize_host(host: str) -> str:
    host = host.rstrip(".").lower()
    if not host:
        raise _deny("URL host is empty")
    try:
        normalized = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _deny("URL host is not valid IDNA") from exc
    if len(normalized) > 253 or any(len(label) > 63 for label in normalized.split(".")):
        raise _deny("URL host is too long")
    return normalized


def canonicalize_url(value: str) -> tuple[str, SplitResult]:
    if not isinstance(value, str) or len(value) > 4_096:
        raise _deny("URL must be a string no longer than 4096 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _deny("URL contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _deny("URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _deny("only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname:
        raise _deny("URL host is required")
    if parsed.username is not None or parsed.password is not None:
        raise _deny("embedded URL credentials are forbidden")
    if any(
        is_sensitive_query_name(name)
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise _deny("credential-like URL query parameters are forbidden")
    host = _normalize_host(parsed.hostname)
    decoded_path = unquote(parsed.path)
    if (
        host == "hooks.slack.com"
        and decoded_path.startswith("/services/")
        or host in {"discord.com", "discordapp.com"}
        and "/api/webhooks/" in decoded_path
    ):
        raise _deny("webhook credential URLs are forbidden")
    port = port or (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise _deny("URL port is invalid")
    default_port = 443 if scheme == "https" else 80
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port == default_port else f"{display_host}:{port}"
    path = quote(parsed.path or "/", safe="/:@!$&'()*+,;=-._~%")
    query = quote(parsed.query, safe="=&?/:@!$'()*+,;[]~-._%")
    canonical = urlunsplit((scheme, netloc, path, query, ""))
    return canonical, urlsplit(canonical)


MAX_FRAGMENT_IDENTITY_CHARS = 200


def canonicalize_fragment_identity(fragment: str) -> str:
    """Bound and validate a URL fragment for use in stored link identity.

    ``canonicalize_url`` always strips the fragment because it is never
    sent to the server, so callers that need a same-page destination change
    (``#step1`` -> ``#step2``) to remain visible in normalized output must
    track it separately. OAuth implicit-flow tokens (``#access_token=...``)
    and similar credentials can appear here too, so the same credential-name
    check used for query parameters applies and fails closed rather than
    let a credential enter a stored/hashed artifact.

    The returned value is bounded to ``MAX_FRAGMENT_IDENTITY_CHARS`` for
    display, but a fragment longer than that bound gets a SHA-256 digest of
    its *complete* validated text appended so two fragments sharing a long
    common prefix still produce distinct identities and hashes.
    """
    if not fragment:
        return ""
    if len(fragment) > 4_096 or any(
        ord(char) < 32 or ord(char) == 127 for char in fragment
    ):
        raise _deny("URL fragment is malformed")
    if any(
        is_sensitive_query_name(name)
        for name, _ in parse_qsl(fragment, keep_blank_values=True)
    ):
        raise _deny("credential-like URL fragment is forbidden")
    if len(fragment) <= MAX_FRAGMENT_IDENTITY_CHARS:
        return fragment
    digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    suffix = f" [sha256:{digest}]"
    prefix_limit = MAX_FRAGMENT_IDENTITY_CHARS - len(suffix)
    return fragment[:prefix_limit] + suffix


def _addresses_from_resolution(
    host: str, port: int, resolver: Resolver
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            answers = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise MonitorError(
                "dns_resolution_failed",
                "target host could not be resolved",
                retryable=True,
            ) from exc
        addresses = tuple(
            sorted(
                {str(answer[4][0]).split("%", 1)[0] for answer in answers},
                key=lambda item: ipaddress.ip_address(item).packed,
            )
        )
    if not addresses:
        raise MonitorError(
            "dns_resolution_failed",
            "target host resolved to no addresses",
            retryable=True,
        )
    disallowed = [address for address in addresses if not is_public_address(address)]
    if disallowed:
        raise _deny("target host resolves to a non-public address")
    return addresses


def resolve_public_url(
    value: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
    verify_stable_dns: bool = True,
) -> ResolvedTarget:
    canonical, parsed = canonicalize_url(value)
    host = _normalize_host(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    first = _addresses_from_resolution(host, port, resolver)
    if verify_stable_dns:
        second = _addresses_from_resolution(host, port, resolver)
        if first != second:
            raise _deny("target DNS answers changed during validation")
    return ResolvedTarget(canonical, parsed.scheme, host, port, first)


def validate_peer_address(peer_address: str, allowed: Iterable[str]) -> None:
    peer = str(ipaddress.ip_address(peer_address.split("%", 1)[0]))
    normalized_allowed = {
        str(ipaddress.ip_address(item.split("%", 1)[0])) for item in allowed
    }
    if peer not in normalized_allowed or not is_public_address(peer):
        raise _deny("connected peer does not match the validated public address set")


def validate_redirect(
    current_url: str,
    location: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> ResolvedTarget:
    if not location or len(location) > 4_096:
        raise MonitorError(
            "redirect_missing_location", "redirect has no usable Location header"
        )
    return resolve_public_url(urljoin(current_url, location), resolver=resolver)


class BrowserNetworkGuard:
    """Request policy used by the optional ephemeral browser fetcher."""

    def __init__(
        self,
        initial_url: str,
        *,
        allowed_hosts: Iterable[str] = (),
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        initial = resolve_public_url(initial_url, resolver=resolver)
        self._resolver = resolver
        self._allowed_hosts = {
            initial.host,
            *(_normalize_host(host) for host in allowed_hosts),
        }
        self.initial = initial

    def validate_request(self, url: str) -> ResolvedTarget:
        target = resolve_public_url(url, resolver=self._resolver)
        if target.host not in self._allowed_hosts:
            raise _deny("browser request host is not explicitly allowed")
        return target

    def validate_response_peer(self, url: str, peer_address: str) -> None:
        target = self.validate_request(url)
        validate_peer_address(peer_address, target.addresses)
