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
        "id-token",
        "oauth-token",
        "password",
        "refresh-token",
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
_MAX_NESTED_URL_DEPTH = 5


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


def _is_webhook_credential_host_path(host: str, decoded_path: str) -> bool:
    return (
        host == "hooks.slack.com"
        and decoded_path.startswith("/services/")
        or host in {"discord.com", "discordapp.com"}
        and "/api/webhooks/" in decoded_path
    )


def _split_nested_url(
    value: str, *, max_layers: int, allow_path_relative: bool = False
) -> tuple[SplitResult, bool] | None:
    """Best-effort parse of ``value`` as a nested absolute or relative URL.

    Returns the parsed ``SplitResult`` together with a flag saying whether
    the match relied on the ambiguous path-relative branch (see below), or
    ``None`` if ``value`` doesn't look like a nested URL at all.

    A single ``parse_qsl`` decode can still leave the nested URL encoded
    (e.g. ``%253A`` decodes to a literal ``%3A``, still hiding the URL's own
    ``:``), so this unquotes up to ``max_layers`` further times looking for
    either an explicit ``http``/``https`` scheme, a scheme-relative
    network-path reference (``//host/...``, which carries no scheme of its
    own but is still fetched as the current scheme by browsers and HTTP
    clients), an absolute-path relative reference (e.g.
    ``/callback?access_token=secret``, the common shape of an OAuth or
    signed-URL redirect target), or, when ``allow_path_relative`` is set, a
    path-relative reference with no leading ``/`` at all (e.g.
    ``callback?access_token=secret``) before giving up.

    The path-relative branch is opt-in and restricted to callers that
    already isolated ``value`` as a single ``parse_qsl``-parsed parameter
    value, because applying it to a whole raw query *string* would treat an
    ordinary value that merely contains a literal, unencoded ``?`` (legal in
    a query per RFC 3986, and not a nested URL at all) as one layer of
    nested reference per ``?`` and walk the recursion depth bound into a
    false "credential-like" denial. A malformed candidate, such as an
    unbalanced IPv6-literal-style host, is treated as an opaque non-URL
    value rather than raised.
    """
    candidate = value
    for _ in range(max_layers + 1):
        try:
            split = urlsplit(candidate)
        except ValueError:
            return None
        scheme = split.scheme.lower()
        if scheme in {"http", "https"} and split.hostname:
            return split, False
        if not scheme and split.netloc and split.hostname:
            return split, False
        if not scheme and not split.netloc and (split.query or split.fragment):
            if not split.path or split.path.startswith("/"):
                return split, False
            if allow_path_relative:
                return split, True
        decoded = unquote(candidate)
        if decoded == candidate:
            return None
        candidate = decoded
    return None


def _nested_url_has_credential(
    value: str, *, depth: int, allow_path_relative: bool = False
) -> bool:
    match = _split_nested_url(
        value, max_layers=_MAX_NESTED_URL_DEPTH, allow_path_relative=allow_path_relative
    )
    if match is None:
        return False
    nested, _was_path_relative = match
    nested_host = (nested.hostname or "").rstrip(".").lower()
    nested_path = unquote(nested.path)
    # Whether this hop matched via the ambiguous path-relative branch or
    # confirmed scheme/host or absolute-path evidence, the same
    # ``allow_path_relative`` permission a caller granted for the outer
    # value carries forward to every deeper hop: a value already isolated
    # via ``parse_qsl`` is exactly as trustworthy one hop down as it is
    # here, so breaking the chain after a single ambiguous hop would accept
    # a credential hidden behind two (or more) path-relative hops in a row.
    # The bounded recursion depth in ``has_credential_bearing_query`` -- not
    # this flag -- is what keeps an arbitrarily long chain from recursing
    # unboundedly.
    return (
        nested.username is not None
        or nested.password is not None
        or _is_webhook_credential_host_path(nested_host, nested_path)
        or has_credential_bearing_query(
            nested.fragment,
            depth=depth + 1,
            allow_path_relative=allow_path_relative,
        )
        or has_credential_bearing_query(
            nested.query,
            depth=depth + 1,
            allow_path_relative=allow_path_relative,
        )
    )


def has_credential_bearing_query(
    query: str, *, depth: int = 0, allow_path_relative: bool
) -> bool:
    """Bounded recursive check for credential-bearing query values.

    A benign outer parameter name (e.g. "redirect") can carry a nested,
    URL-encoded HTTP(S), scheme-relative (network-path), or scheme-less
    relative-reference URL whose own query, fragment, embedded userinfo, or
    host/path carries the credential (e.g.
    "?redirect=https%3A%2F%2Fidp.example%2Fcb%3Faccess_token%3Dsecret",
    "?redirect=%2Fcallback%3Faccess_token%3Dsecret" with no host at all,
    "?redirect=callback%3Faccess_token%3Dsecret" with no leading slash
    either, a nested Slack/Discord webhook URL possibly carried after a "#"
    instead of a "?", or the same nested URL under an extra layer of
    percent-encoding), which ``parse_qsl`` decodes into the value but a flat
    exact-name/suffix check never re-inspects. A chain of path-relative
    hops is trusted for as many hops as ``allow_path_relative`` was granted
    for, not just the first one -- see ``_nested_url_has_credential`` --
    since the depth bound below, not a one-hop chain-break, is what keeps
    recursion bounded.

    ``allow_path_relative`` has no default and every caller must state it:
    entry points that validate a real, directly-supplied URL or fragment
    (``canonicalize_url``, ``canonicalize_fragment_identity``,
    ``validate_http_url``) pass ``True``, since there the "outer query" is
    the thing actually being fetched or stored, not a heuristically-detected
    nested guess.

    ``allow_path_relative`` gates only the ambiguous no-leading-slash
    relative-reference match in ``_split_nested_url``.

    A fragment can also be the nested URL itself rather than key/value pairs
    (e.g. "...#https%3A%2F%2Fuser%3Apass%40example.com/"), in which case
    ``parse_qsl`` decodes it into a single blank-valued parameter *name* and
    never hands the encoded URL to ``_split_nested_url`` at all, so the raw
    string is also checked as a candidate nested URL before it is split into
    pairs. That whole-string check is always strict (``allow_path_relative``
    left at its default ``False`` in ``_split_nested_url``) because a raw,
    unparsed query string is exactly the "ordinary value with a literal ?"
    case the ambiguous branch must not be applied to.

    Once the recursion budget (``depth`` past ``_MAX_NESTED_URL_DEPTH``) is
    spent, there is no more budget left to keep unwrapping candidate nested
    URLs, but the remaining text is still flat-scanned once for a sensitive
    parameter name, and denied outright if it still looks like it could
    contain further nested-URL structure we can no longer afford to
    unwrap -- exhausting the budget on an ordinary, fully-decoded value
    (e.g. the tail end of a query value that merely contains several
    literal, unencoded "?" characters, RFC 3986-legal and not a nested URL
    at all) is not itself evidence of a hidden credential, but exhausting it
    on text that *still* parses as a candidate nested URL is exactly the
    "we could not finish checking" case a credential boundary must fail
    closed on.
    """
    if depth > _MAX_NESTED_URL_DEPTH:
        if _split_nested_url(
            query, max_layers=_MAX_NESTED_URL_DEPTH, allow_path_relative=True
        ):
            return True
        return any(
            is_sensitive_query_name(name)
            for name, _ in parse_qsl(query, keep_blank_values=True)
        )
    if _nested_url_has_credential(query, depth=depth):
        return True
    for name, value in parse_qsl(query, keep_blank_values=True):
        if is_sensitive_query_name(name):
            return True
        if _nested_url_has_credential(
            value, depth=depth, allow_path_relative=allow_path_relative
        ):
            return True
    return False


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
    if has_credential_bearing_query(parsed.query, allow_path_relative=True):
        raise _deny("credential-like URL query parameters are forbidden")
    host = _normalize_host(parsed.hostname)
    decoded_path = unquote(parsed.path)
    if _is_webhook_credential_host_path(host, decoded_path):
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
    if has_credential_bearing_query(fragment, allow_path_relative=True):
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
