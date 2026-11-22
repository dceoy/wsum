"""Deterministic core for the Gemini hybrid web-update skill.

This module is a near-verbatim port of wsum's ``monitor.py`` pure
computations -- content-type sniffing and decoding, HTML / feed / PDF
normalization, navigation-link handling, SHA-256 hashing, and bounded
unified diffs -- plus the target-validation and grouping rules of
``workspace.py``. Section banners mark the ported ranges.

Deliberate exclusions, imposed by the skill-script sandbox (no
network egress, stateless, time-bounded):

- The fetch layer (DNS resolution with SSRF guards, pinned sockets,
  redirects, response reading) is NOT ported. Fetching is prompt
  based: Gemini browsing retrieves each page and passes its bytes
  (``body_base64``) or its extracted text (``text``) to ``normalize``.
- Filesystem state (snapshots, pending transactions, recovery) is
  NOT ported. State lives in Google Sheets / Drive; every invocation
  is one pure computation over stdin JSON.

``pypdf`` is assumed to be preinstalled in the execution environment
(the core's only third-party dependency); PDF normalization fails
with the core's own error when it is missing.

Interface: one subcommand per invocation, JSON in on stdin, JSON out
on stdout.

    validate          {"rows": [[header...], [row...], ...]}
                      or {"csv": "<csv text>"}
    normalize         {"body_base64": "...", "content_type": "...",
                       "charset": null, "source_url": "https://..."}
                      or {"text": "...", "content_type": "...",
                       "source_url": "https://..."}
                      (either form may add "navigation_links": [...])
    compare           {"current": "<normalized text>",
                       "previous": "<normalized text>" | null,
                       "compare_navigation_markers": false}
    extract-pdf-text  {"pdf_base64": "..."}

Hard failures print ``{"error": "..."}`` and exit with status 2.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import csv
import hashlib
import io
import json
import re
import sys
import threading
import xml.etree.ElementTree as ET  # ruff: ignore[suspicious-xml-etree-import]
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import unified_diff
from email.message import Message
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

VERSION = "0.3.1-hybrid.2"

# Hybrid transport guard for CLI payloads (not a core algorithm limit):
# one normalize/compare input may carry at most this many characters.
MAX_PAYLOAD_CHARS = 2_000_000
MAX_CSV_BYTES = 1024 * 1024

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_DIFF_LINES = 200
_DEFAULT_MAX_DIFF_BYTES = 65_536
_DEFAULT_MAX_DIFF_COMPLEXITY = 4_000_000
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_REDIRECTS = 10
_VERSION = "0.1.0"
_USER_AGENT = f"wsum/{_VERSION}"
_MAX_NAVIGATION_LINKS_FILE_BYTES = 512 * 1024
_MAX_NAVIGATION_LINKS = 500
_NAVIGATION_LINK_MARKER_RE = re.compile(
    r"^\[(?:a|area):href:sha256:([a-f0-9]{64})\]$", re.MULTILINE
)
_DEFAULT_MAX_PDF_DECOMPRESSED_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_PDF_EXTRACTED_CHARS = 10 * 1024 * 1024
_DEFAULT_MAX_PDF_PAGES = 1_000
_DEFAULT_MAX_PDF_OBJECTS = 10_000
_DEFAULT_MAX_PDF_ANNOTATIONS = 500
_DEFAULT_MAX_PDF_FONTS = 500
_DEFAULT_MAX_SNAPSHOT_BYTES = 40 * 1024 * 1024
_DEFAULT_MAX_XML_CHARS = 10 * 1024 * 1024
_DEFAULT_MAX_XML_DEPTH = 200
_DEFAULT_MAX_XML_BASE_URL_CHARS = 4_096
_DEFAULT_MAX_XML_BASE_STACK_BYTES = 1_000_000
_RESOLVER_WORKERS = 4
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_HTML_DESTINATION_ATTRS = {
    "a": ("href",),
    "area": ("href",),
    "button": ("formaction",),
    "form": ("action",),
    "input": ("formaction",),
}
_HTML_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_HTML_LINE_BREAK_TAGS = {"br"}
_MAX_HTML_DESTINATIONS = 500
_SENSITIVE_QUERY_NAMES = frozenset({
    "access-token",
    "api-key",
    "api-token",
    "apikey",
    "auth",
    "auth-token",
    "authorization",
    "awsaccesskeyid",
    "credential",
    "id-token",
    "key",
    "oauth-token",
    "password",
    "refresh-token",
    "secret",
    "sig",
    "signature",
    "subscription-key",
    "token",
    "x-api-key",
})
_SENSITIVE_QUERY_SUFFIXES = (
    "credential",
    "secret",
    "signature",
    "security-token",
    "session-token",
)
_QUERY_SEPARATOR_RE = re.compile(r"[\s._-]+")
_MAX_NESTED_URL_DEPTH = 5
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_FEED_CONTENT_TYPES = {"application/atom+xml", "application/rss+xml"}
_XML_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_SUPPORTED_TEXT_ENCODINGS = {
    "ascii",
    "big5",
    "cp932",
    "cp1252",
    "euc_jp",
    "euc_kr",
    "gb18030",
    "gbk",
    "iso2022_jp",
    "iso8859-1",
    "shift_jis",
    "utf-16",
    "utf-16-be",
    "utf-16-le",
    "utf-32",
    "utf-32-be",
    "utf-32-le",
    "utf-8",
    "utf-8-sig",
}
_BOM_ENCODINGS = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
_HTML_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})",
    re.IGNORECASE,
)
_HTML_CONTENT_TYPE_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*\bcontent\s*=\s*[\"'][^\"']*?\bcharset\s*=\s*"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})",
    re.IGNORECASE,
)
_XML_ENCODING_RE = re.compile(
    rb"<\?xml\b[^>]*\bencoding\s*=\s*[\"']"
    rb"([A-Za-z][A-Za-z0-9._:-]{0,63})[\"']",
    re.IGNORECASE,
)
_PYPDF_LIMIT_NAMES = (
    "maximum_declared_stream_length",
    "array_based_stream_maximum_output_length",
    "jbig2_maximum_output_length",
    "lzw_maximum_output_length",
    "run_length_maximum_output_length",
    "zlib_maximum_output_length",
    "image_maximum_buffer_size",
    "zlib_maximum_recovery_input_length",
)

_PDF_LIMIT_LOCK = threading.Lock()


class MonitorError(RuntimeError):
    """Expected input, network, or normalization failure."""


class HTTPStatusError(MonitorError):
    """HTTP response rejected because its status is not successful."""

    def __init__(self, status: int, *, source_url: str = "") -> None:
        """Create an error for one rejected HTTP status."""
        self.status = status
        self.source_url = source_url
        super().__init__(f"fetch failed: HTTP {status}")


@dataclass(frozen=True, slots=True)
class Document:
    """Fetched or local document bytes plus source metadata."""

    body: bytes
    source_url: str
    content_type: str
    charset: str | None = None


class LinkCollection(dict[str, str]):  # ruff: ignore[subclass-builtin]
    """Collect bounded navigation links and hashes of links omitted by size."""

    def __init__(self) -> None:
        """Create empty link and omission collections."""
        super().__init__()
        self.omitted_hashes: set[str] = set()
        self.overflow_count = 0


def _merge_link_collection(target: dict[str, str], source: LinkCollection) -> None:
    target.update(source)
    if isinstance(target, LinkCollection):
        target.omitted_hashes.update(source.omitted_hashes)
        target.overflow_count += source.overflow_count


class _TextExtractor(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._document_url = source_url
        self._base_url = source_url
        self._base_href_seen = False
        self._skip_depth = 0
        self._destination_count = 0
        self._omitted = hashlib.sha256()
        self._omitted_count = 0
        self.parts: list[str] = []
        self.links = LinkCollection()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HTML_LINE_BREAK_TAGS:
            self.parts.append("\n")
        if tag in _HTML_BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        values = {name.lower(): value for name, value in attrs if name and value}
        if tag == "base":
            base_href = values.get("href")
            if base_href and not self._base_href_seen:
                self._base_url = urljoin(self._document_url, base_href.strip())
                self._base_href_seen = True
            return
        destinations = _HTML_DESTINATION_ATTRS.get(tag, ())
        if not destinations:
            return
        for name in destinations:
            value = values.get(name)
            if value:
                self._record_destination(tag, name, value)

    def _record_destination(self, tag: str, name: str, value: str) -> None:
        self._destination_count += 1
        destination = urljoin(self._base_url, value.strip())
        if _destination_has_credentials(destination):
            raise MonitorError("HTML destination contains credentials")
        if self._destination_count > _MAX_HTML_DESTINATIONS:
            # Keep the first destinations; flag the rest as omitted so the
            # review is marked incomplete instead of failing the target.
            digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
            self._omitted.update(f"{tag}:{name}:{digest}\n".encode())
            self._omitted_count += 1
            self.links.overflow_count += 1
            return
        if tag in {"a", "area"}:
            _collect_link(self.links, destination)
        digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
        self.parts.append(f"\n[{tag}:{name}:sha256:{digest}]\n")

    def close(self) -> None:
        """Finish parsing and record omitted destinations as one bounded marker."""
        super().close()
        if self._omitted_count:
            # Keeps destination-only changes beyond the limit visible to the diff.
            self.parts.append(
                f"\n[omitted-destinations:{self._omitted_count}"
                f":sha256:{self._omitted.hexdigest()}]\n"
            )
            self._omitted_count = 0

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


_EMBEDDED_HTML_START_RE = re.compile(r"</?[A-Za-z]")


def _collect_link(links: dict[str, str], destination: str) -> None:
    """Collect HTTP(S) navigation identities without fragments or credentials."""
    try:
        parsed = urlsplit(destination)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return
        port = parsed.port
        if port is not None and port <= 0:
            return
    except ValueError:
        return
    digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
    if len(destination.encode("utf-8")) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
        if isinstance(links, LinkCollection):
            links.omitted_hashes.add(digest)
        return
    links[digest] = parsed._replace(fragment="").geturl()


def _canonical_navigation_url(value: object) -> str:
    """Validate and canonicalize one absolute HTTP(S) navigation URL."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonitorError("--navigation-links contains an invalid URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MonitorError("--navigation-links contains an invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MonitorError("--navigation-links contains an invalid URL")
    if _destination_has_credentials(value):
        raise MonitorError("--navigation-links contains a credential-bearing URL")
    if port is not None and port <= 0:
        raise MonitorError("--navigation-links contains an invalid URL")
    destination = parsed.geturl()
    if len(destination.encode("utf-8")) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
        raise MonitorError("--navigation-links contains an oversized URL")
    return destination


def _validate_navigation_links(value: object) -> list[str]:
    """Validate a JSON array of absolute HTTP(S) navigation URLs."""
    if not isinstance(value, list):
        raise MonitorError("navigation_links must be a JSON array")
    items = cast("list[object]", value)
    if len(items) > _MAX_NAVIGATION_LINKS:
        raise MonitorError("navigation_links must contain at most 500 URLs")

    destinations: list[str] = []
    seen: set[str] = set()
    for item in items:
        destination = _canonical_navigation_url(item)
        if destination not in seen:
            seen.add(destination)
            destinations.append(destination)
    return destinations


def _include_navigation_links(
    text: str, links: LinkCollection, navigation_links: list[str]
) -> str:
    """Add externally extracted navigation URLs to normalized text and links."""
    if not navigation_links:
        return text
    existing_hashes = set(_NAVIGATION_LINK_MARKER_RE.findall(text))
    markers: list[str] = []
    for destination in navigation_links:
        _collect_link(links, destination)
        digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
        if digest not in existing_hashes:
            markers.append(f"[a:href:sha256:{digest}]")
            existing_hashes.add(digest)
    if not markers:
        return text
    return _normalize_whitespace(f"{text}\n" + "\n".join(markers))


def _navigation_comparison_view(
    text: str,
) -> tuple[str, str, frozenset[str], tuple[tuple[str, int], ...], bool]:
    """Return a stable comparison view and navigation-marker metadata."""
    matches = list(_NAVIGATION_LINK_MARKER_RE.finditer(text))
    if not matches:
        return text, text, frozenset(), (), False

    body = _normalize_whitespace(_NAVIGATION_LINK_MARKER_RE.sub("", text))
    identities = frozenset(match.group(1) for match in matches)
    markers = [f"[a:href:sha256:{identity}]" for identity in sorted(identities)]
    comparison_text = _normalize_whitespace(f"{body}\n" + "\n".join(markers))
    placements = tuple(
        sorted(
            (
                match.group(1),
                len(_NAVIGATION_LINK_MARKER_RE.sub("", text[: match.start()]).split()),
            )
            for match in matches
        )
    )

    trailing_count = 0
    for line in reversed(text.splitlines()):
        if _NAVIGATION_LINK_MARKER_RE.fullmatch(line):
            trailing_count += 1
        elif not line.strip():
            continue
        else:
            break
    is_manifest_suffix = trailing_count == len(matches)
    return body, comparison_text, identities, placements, is_manifest_suffix


def _navigation_comparison_views_match(
    current: tuple[str, str, frozenset[str], tuple[tuple[str, int], ...], bool],
    previous: tuple[str, str, frozenset[str], tuple[tuple[str, int], ...], bool],
) -> bool:
    """Return whether navigation markers can be compared as a set."""
    if current[4] and previous[4]:
        return True
    if current[4] == previous[4] or not current[2]:
        return False
    return all((
        current[2] == previous[2],
        current[0].split() == previous[0].split(),
        current[3] != previous[3],
    ))


def _navigation_comparison_pair(current: str, previous: str) -> tuple[str, str] | None:
    """Return marker-normalized texts when marker placement is incidental."""
    views = (
        _navigation_comparison_view(current),
        _navigation_comparison_view(previous),
    )
    if _navigation_comparison_views_match(*views):
        return views[0][1], views[1][1]
    return None


def _normalize_html_fragment(
    value: str, base_url: str, links: dict[str, str] | None = None
) -> str:
    """Normalize HTML embedded in an XML text field."""
    tag_start = _EMBEDDED_HTML_START_RE.search(value)
    if tag_start is None or ">" not in value[tag_start.end() :]:
        return value
    parser = _TextExtractor(base_url)
    parser.feed(value)
    parser.close()
    if links is not None:
        _merge_link_collection(links, parser.links)
    return "".join(parser.parts)


def _is_sensitive_query_name(name: str) -> bool:
    normalized = _QUERY_SEPARATOR_RE.sub("-", name.strip().lower())
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
        _SENSITIVE_QUERY_SUFFIXES
    )


def _is_webhook_credential_path(host: str, path: str) -> bool:
    normalized_host = host.rstrip(".").lower()
    return (normalized_host == "hooks.slack.com" and path.startswith("/services/")) or (
        normalized_host in {"discord.com", "discordapp.com"}
        and "/api/webhooks/" in path
    )


def _nested_url_has_credentials(value: str, *, depth: int) -> bool:
    if depth > _MAX_NESTED_URL_DEPTH:
        return True
    candidate = value
    for _ in range(_MAX_NESTED_URL_DEPTH + 1):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return True
        # NOTE: SplitResult.hostname cannot raise ValueError on CPython (it
        # only splits/lowercases the netloc); bracket validation already
        # happens in urlsplit() above, so no try/except is needed here.
        host = parsed.hostname or ""
        has_credentials = (
            parsed.username is not None
            or parsed.password is not None
            or _is_webhook_credential_path(host, unquote(parsed.path))
            or _query_has_credentials(parsed.query, depth=depth + 1)
            or _query_has_credentials(parsed.fragment, depth=depth + 1)
        )
        if has_credentials:
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def _query_has_credentials(query: str, *, depth: int) -> bool:
    if depth > _MAX_NESTED_URL_DEPTH:
        return True
    # Treat the legacy semicolon separator as equivalent to '&'.
    candidate = query
    for _ in range(_MAX_NESTED_URL_DEPTH - depth + 1):
        for name, value in parse_qsl(
            candidate.lstrip("?").replace(";", "&"), keep_blank_values=True
        ):
            if _is_sensitive_query_name(name):
                return True
            if value and (
                _nested_url_has_credentials(value, depth=depth)
                or _query_has_credentials(unquote(value), depth=depth + 1)
            ):
                return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def _normalize_whitespace(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def _parse_content_type(value: str) -> tuple[str, str | None]:
    message = Message()
    try:
        message["Content-Type"] = value
    except ValueError as exc:
        raise MonitorError("content type is invalid") from exc
    raw_charset = message.get_param("charset")
    charset = raw_charset if isinstance(raw_charset, str) else None
    return message.get_content_type().lower(), charset


def _sniff_text_content_type(sample: str) -> str:
    """Detect structured content from a decoded bounded prefix."""
    if re.search(r"<(?:\w+:)?(?:rss|feed|rdf)\b", sample):
        return "application/rss+xml"
    xml_declaration = re.match(r"<\?xml\b[^>]*\?>", sample)
    if xml_declaration is not None:
        after_declaration = sample[xml_declaration.end() :].lstrip()
        if re.match(r"<(?:!doctype\s+html|(?:\w+:)?html)\b", after_declaration):
            return "text/html"
        return "application/xml"
    if re.search(
        r"<(?:!doctype\s+html|html|head|body|main|article|section|div|"
        r"h[1-6]|p|table|script|a|area|form|button|input)\b",
        sample,
    ):
        return "text/html"
    return "text/plain"


def _sniff_content_type(body: bytes, *, charset: str | None = None) -> str:
    """Detect the structural document type from a bounded byte prefix."""
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    encoding = _bom_encoding(body)
    if encoding is None and charset:
        try:
            encoding = _encoding_name(charset)
        except MonitorError:
            encoding = None
    sample_bytes = body[:8_192]
    try:
        sample = (
            # final=False tolerates a multibyte character cut by the prefix bound.
            codecs.getincrementaldecoder(encoding)(errors="strict").decode(
                sample_bytes, final=False
            )
            if encoding is not None
            else sample_bytes.decode("latin-1", errors="strict")
        )
    except UnicodeDecodeError:
        return "text/plain"
    return _sniff_text_content_type(sample.lstrip().lower())


def _normalization_content_type(document: Document) -> str:
    """Cross-check declared MIME type with bytes and select the parser type."""
    declared = document.content_type
    sniffed = _sniff_content_type(document.body, charset=document.charset)
    supported = {
        "application/pdf",
        "text/plain",
        *_XML_CONTENT_TYPES,
        *_HTML_CONTENT_TYPES,
    }
    if declared not in supported:
        raise MonitorError("declared content type is unsupported")
    if declared == "text/plain":
        return sniffed
    if declared in _XML_CONTENT_TYPES and sniffed in _XML_CONTENT_TYPES:
        return sniffed
    if declared in _HTML_CONTENT_TYPES and sniffed in _HTML_CONTENT_TYPES:
        return sniffed
    if declared == sniffed:
        return sniffed
    raise MonitorError("declared content type does not match detected document type")


def _encoding_name(value: str) -> str:
    candidate = value.strip().strip("\"'")
    if not candidate or len(candidate) > 64:
        raise MonitorError("document encoding is unsupported")
    try:
        name = codecs.lookup(candidate).name
    except LookupError as exc:
        message = f"document encoding is unsupported: {candidate}"
        raise MonitorError(message) from exc
    if name not in _SUPPORTED_TEXT_ENCODINGS:
        message = f"document encoding is unsupported: {candidate}"
        raise MonitorError(message)
    return name


def _bom_encoding(body: bytes) -> str | None:
    for marker, encoding in _BOM_ENCODINGS:
        if body.startswith(marker):
            return encoding
    return None


def _in_document_encoding(body: bytes, content_type: str) -> str | None:
    prefix = body[:4_096]
    match: re.Match[bytes] | None
    if content_type in _XML_CONTENT_TYPES:
        match = _XML_ENCODING_RE.search(prefix)
    elif content_type in _HTML_CONTENT_TYPES:
        match = _HTML_CHARSET_RE.search(prefix)
        if match is None:
            match = _HTML_CONTENT_TYPE_CHARSET_RE.search(prefix)
    else:
        match = None
    return match.group(1).decode("ascii") if match else None


def _decode_document(document: Document) -> str:
    encoding = _bom_encoding(document.body)
    if encoding is None and document.charset:
        encoding = _encoding_name(document.charset)
    if encoding is None:
        declared = _in_document_encoding(document.body, document.content_type)
        encoding = _encoding_name(declared) if declared else "utf-8"
    try:
        return document.body.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        message = f"document could not be decoded as {encoding}"
        raise MonitorError(message) from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class _FeedTextTarget:
    """Collect bounded feed text, identities, and link destinations."""

    _ENTRY_TAGS = frozenset({"entry", "item"})
    _IDENTITY_TAGS = frozenset({"guid", "id", "link"})
    _EMBEDDED_TEXT_TAGS = frozenset({"content", "description", "encoded", "summary"})
    _ENTRY_MARKER = "\x00wsum-entry-placeholder\x00"

    def __init__(
        self, max_chars: int, *, preserve_structure: bool, base_url: str
    ) -> None:
        if len(base_url.encode("utf-8")) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
            raise MonitorError("XML base URL exceeds the size limit")
        self._max_chars = max_chars
        self._preserve_structure = preserve_structure
        self._base_url = base_url
        self._base_stack: list[str] = []
        self._base_stack_bytes = 0
        self._tag_stack: list[str] = []
        self._text_destination_stack: list[list[str] | None] = []
        self._identity_frames: list[tuple[str, list[str], str | None]] = []
        self._entry_parts: list[str] | None = None
        self._entry_level = 0
        self._entry_identity: dict[str, str] = {}
        self._entries: list[tuple[str, str]] = []
        self._entry_marker_added = False
        self._size = 0
        self._parts: list[str] = []
        self._embedded_text_frames: list[list[str] | None] = []
        self._buffered_text_chars = 0
        self.links = LinkCollection()

    def _append(self, value: str) -> None:
        self._size += len(value)
        if self._size > self._max_chars:
            raise MonitorError("XML extracted text exceeds the size limit")
        if self._entry_parts is not None:
            self._entry_parts.append(value)
        else:
            self._parts.append(value)

    def _start_entry(self) -> None:
        if self._entry_parts is not None:
            return
        self._entry_parts = []
        self._entry_level = 0
        self._entry_identity = {}
        if not self._entry_marker_added:
            self._parts.append(self._ENTRY_MARKER)
            self._entry_marker_added = True

    @staticmethod
    def _identity_value(local_tag: str, attrs: dict[str, str]) -> str | None:
        if local_tag == "link":
            relation = next(
                (
                    value.strip().lower()
                    for name, value in attrs.items()
                    if _local_name(name) == "rel" and value
                ),
                "alternate",
            )
            if relation != "alternate":
                return None
            return next(
                (
                    value.strip()
                    for name, value in attrs.items()
                    if _local_name(name) == "href" and value
                ),
                None,
            )
        return None

    def _push_base(self, local_tag: str, attrs: dict[str, str]) -> None:
        if len(self._base_stack) >= _DEFAULT_MAX_XML_DEPTH:
            raise MonitorError("XML nesting exceeds the size limit")
        base_url = self._base_stack[-1] if self._base_stack else self._base_url
        for name, value in attrs.items():
            if (
                name == "{http://www.w3.org/XML/1998/namespace}base"
                or name.lower() == "xml:base"
            ):
                if value:
                    base_value = value.strip()
                    if len(base_value) > _DEFAULT_MAX_XML_BASE_URL_CHARS:
                        raise MonitorError("XML base URL exceeds the size limit")
                    base_url = urljoin(base_url, base_value)
                break
        base_size = len(base_url.encode("utf-8"))
        if base_size > _DEFAULT_MAX_XML_BASE_URL_CHARS:
            raise MonitorError("XML base URL exceeds the size limit")
        if self._base_stack_bytes + base_size > _DEFAULT_MAX_XML_BASE_STACK_BYTES:
            raise MonitorError("XML base stack exceeds the size limit")
        self._base_stack.append(base_url)
        self._base_stack_bytes += base_size
        self._tag_stack.append(local_tag)
        self._embedded_text_frames.append(
            []
            if self._preserve_structure and local_tag in self._EMBEDDED_TEXT_TAGS
            else None
        )

    def _flush_embedded_text(self) -> None:
        if not self._embedded_text_frames:
            return
        parts = self._embedded_text_frames[-1]
        if parts is None or not parts:
            return
        value = "".join(parts)
        parts.clear()
        self._buffered_text_chars -= len(value)
        base_url = self._base_stack[-1] if self._base_stack else self._base_url
        self._append(_normalize_html_fragment(value, base_url, self.links))

    def _append_start(self, local_tag: str, attrs: dict[str, str]) -> None:
        destinations = sorted(
            (
                (_local_name(name), value)
                for name, value in attrs.items()
                if _local_name(name) in {"href", "url"} and value
            )
        )
        is_text_destination = (
            self._preserve_structure and local_tag == "link" and not destinations
        )
        self._text_destination_stack.append([] if is_text_destination else None)
        if not self._preserve_structure:
            return
        self._append(f"\n[{local_tag}:start]\n")
        if local_tag not in {"a", "enclosure", "link"}:
            return
        for name, value in destinations:
            self._append_destination(local_tag, name, value)
            if local_tag == "a" or (
                local_tag == "link" and attrs.get("rel", "alternate") == "alternate"
            ):
                base_url = self._base_stack[-1] if self._base_stack else self._base_url
                _collect_link(self.links, urljoin(base_url, value.strip()))

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self._flush_embedded_text()
        local_tag = _local_name(tag)
        if self._preserve_structure and local_tag in self._ENTRY_TAGS:
            self._start_entry()
        if self._entry_parts is not None:
            self._entry_level += 1
        self._push_base(local_tag, attrs)
        identity_value = self._identity_value(local_tag, attrs)
        if self._entry_parts is not None and local_tag in self._IDENTITY_TAGS:
            self._identity_frames.append((local_tag, [], identity_value))
        self._append_start(local_tag, attrs)

    def _finish_identity(self, field: str, value: str) -> None:
        if not value:
            return
        if field not in self._entry_identity:
            self._entry_identity[field] = value
        if field in {"guid", "id"}:
            self._append(_feed_identity_token(field, value, self._base_url))

    def end(self, tag: str) -> None:
        self._flush_embedded_text()
        local_tag = _local_name(tag)
        text_destination = (
            self._text_destination_stack.pop() if self._text_destination_stack else None
        )
        if text_destination is not None:
            value = "".join(text_destination).strip()
            if value:
                self._append_destination("link", "href", value)
                base_url = self._base_stack[-1] if self._base_stack else self._base_url
                _collect_link(self.links, urljoin(base_url, value))

        if self._identity_frames and self._identity_frames[-1][0] == local_tag:
            field, values, attribute_value = self._identity_frames.pop()
            value = attribute_value or "".join(values).strip()
            self._finish_identity(field, value)
        if self._preserve_structure:
            self._append(f"\n[{local_tag}:end]\n")

        if self._entry_parts is not None:
            self._entry_level -= 1
            if self._entry_level == 0:
                entry_text = "".join(self._entry_parts)
                identity = next(
                    (
                        self._entry_identity[field]
                        for field in ("guid", "id", "link")
                        if self._entry_identity.get(field)
                    ),
                    entry_text,
                )
                key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                self._entries.append((key, entry_text))
                self._entry_parts = None
                self._entry_identity = {}
                self._identity_frames.clear()

        if self._tag_stack:
            self._tag_stack.pop()
        if self._embedded_text_frames:
            self._embedded_text_frames.pop()
        if self._base_stack:
            self._base_stack_bytes -= len(self._base_stack.pop().encode("utf-8"))

    def data(self, data: str) -> None:
        if self._identity_frames:
            for _, values, _ in self._identity_frames:
                values.append(data)
            if self._identity_frames[-1][0] in {"guid", "id"}:
                return
        if (
            self._text_destination_stack
            and self._text_destination_stack[-1] is not None
        ):
            self._text_destination_stack[-1].append(data)  # type: ignore[union-attr]
            return
        if (
            self._preserve_structure
            and self._embedded_text_frames
            and self._embedded_text_frames[-1] is not None
        ):
            if self._size + self._buffered_text_chars + len(data) > self._max_chars:
                raise MonitorError("XML extracted text exceeds the size limit")
            self._embedded_text_frames[-1].append(data)  # type: ignore[union-attr]
            self._buffered_text_chars += len(data)
            return
        self._append(data)

    def _append_destination(self, tag: str, name: str, value: str) -> None:
        base_url = self._base_stack[-1] if self._base_stack else self._base_url
        destination = urljoin(base_url, value.strip())
        if _destination_has_credentials(destination):
            raise MonitorError("feed destination contains credentials")
        digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
        self._append(f"[{tag}:{name}:sha256:{digest}]\n")

    def close(self) -> str:
        entries = "\n".join(entry_text for _, entry_text in sorted(self._entries))
        return "".join(self._parts).replace(self._ENTRY_MARKER, entries)

    @staticmethod
    def doctype(name: str, public_id: str, system_id: str) -> None:
        del name, public_id, system_id
        raise MonitorError("DOCTYPE declarations are not supported")


def _destination_has_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        return True
    return (
        parsed.username is not None
        or parsed.password is not None
        or _query_has_credentials(parsed.fragment, depth=0)
        or _is_webhook_credential_path(host, unquote(parsed.path))
        or _query_has_credentials(parsed.query, depth=0)
    )


def url_has_credentials(value: str) -> bool:
    """Return whether a URL contains embedded or query credentials."""
    return _destination_has_credentials(value)


def _feed_identity_token(field: str, value: str, base_url: str) -> str:
    """Return bounded feed identity text without exposing URI-shaped values."""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise MonitorError("feed identity is invalid") from exc
    if not parsed.scheme and not parsed.netloc:
        return value
    identity = value
    if parsed.scheme.lower() in {"http", "https"} or parsed.netloc:
        identity = urljoin(base_url, value)
        if _destination_has_credentials(identity):
            raise MonitorError("feed identity contains credentials")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"[{field}:sha256:{digest}]"


def _normalize_feed(
    text: str,
    *,
    max_chars: int,
    preserve_structure: bool,
    base_url: str,
    links: dict[str, str] | None = None,
) -> str:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise MonitorError("DOCTYPE and entity declarations are not supported")
    target = _FeedTextTarget(
        max_chars, preserve_structure=preserve_structure, base_url=base_url
    )
    parser = ET.XMLParser(target=target)  # ruff: ignore[suspicious-xml-element-tree-usage]
    try:
        parser.feed(text)
        parsed = parser.close()
    except MonitorError:
        raise
    except ET.ParseError as exc:
        raise MonitorError("XML document could not be parsed") from exc
    if not isinstance(parsed, str):
        raise MonitorError("XML parser returned an invalid result")
    if links is not None:
        _merge_link_collection(links, target.links)
    return _normalize_whitespace(parsed)


@contextmanager
def _pypdf_output_limits(limit: int) -> Generator[Callable[[int], None], None, None]:
    """Temporarily cap pypdf stream expansion while holding a process lock.

    Yields:
        A setter for reducing the remaining output budget during extraction.
    """
    try:
        from pypdf import (  # ruff: ignore[import-outside-top-level]
            apply_configuration,
            filters,
            overwrite_configuration,
        )
    except ImportError as exc:
        raise MonitorError("PDF normalization requires pypdf") from exc
    with (
        _PDF_LIMIT_LOCK,
        apply_configuration(**dict.fromkeys(_PYPDF_LIMIT_NAMES, limit)),
    ):

        def set_limit(value: int) -> None:
            overwrite_configuration(**dict.fromkeys(_PYPDF_LIMIT_NAMES, value))

        original_decompress = filters.decompress

        def bounded_decompress(data: bytes) -> bytes:
            if len(data) > limit:
                raise MonitorError("PDF Flate recovery input exceeds the size limit")
            return original_decompress(data)

        filters.decompress = bounded_decompress
        try:
            yield set_limit
        finally:
            filters.decompress = original_decompress


def _resolve_pdf_object(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _pdf_values(value: Any) -> list[Any]:
    resolved = _resolve_pdf_object(value)
    if resolved is None:
        return []
    if isinstance(resolved, list):
        return cast("list[Any]", resolved).copy()
    if isinstance(resolved, tuple):
        return list(cast("tuple[Any, ...]", resolved))
    return [resolved]


def _consume_pdf_object(object_count: list[int]) -> None:
    object_count[0] += 1
    if object_count[0] > _DEFAULT_MAX_PDF_OBJECTS:
        raise MonitorError("PDF object traversal exceeds the size limit")


def _preflight_pdf_pages(reader: Any) -> list[int]:
    """Bound page-tree traversal before pypdf materializes the page list."""
    object_count = [0]
    trailer = getattr(reader, "trailer", {})
    root = _resolve_pdf_object(trailer.get("/Root"))
    pages_root = _resolve_pdf_object(root.get("/Pages"))
    declared_count = pages_root.get("/Count")
    if isinstance(declared_count, int) and declared_count > _DEFAULT_MAX_PDF_PAGES:
        raise MonitorError("PDF page count exceeds the size limit")
    seen: set[int] = set()
    page_count = 0

    def visit(node: Any) -> None:
        nonlocal page_count
        resolved = _resolve_pdf_object(node)
        identity = id(resolved)
        if identity in seen:
            return
        seen.add(identity)
        _consume_pdf_object(object_count)
        get_value = getattr(resolved, "get", None)
        if not callable(get_value):
            return
        kids = _resolve_pdf_object(get_value("/Kids"))
        if isinstance(kids, (list, tuple)):
            typed_kids = cast("list[Any] | tuple[Any, ...]", kids)
            for child in typed_kids:
                visit(child)
        else:
            page_count += 1
            if page_count > _DEFAULT_MAX_PDF_PAGES:
                raise MonitorError("PDF page count exceeds the size limit")

    visit(pages_root)
    return object_count


def _append_form_xobjects(
    resources: Any,
    streams: list[Any],
    seen: set[int],
    object_count: list[int],
) -> None:
    resources = _resolve_pdf_object(resources)
    get_value = getattr(resources, "get", None)
    if not callable(get_value):
        return
    xobjects = _resolve_pdf_object(get_value("/XObject"))
    if not isinstance(xobjects, Mapping):
        return
    typed_xobjects = cast("Mapping[object, Any]", xobjects)
    for candidate in typed_xobjects.values():
        _consume_pdf_object(object_count)
        form = _resolve_pdf_object(candidate)
        form_get = getattr(form, "get", None)
        if not callable(form_get) or form_get("/Subtype") != "/Form":
            continue
        identity = id(form)
        if identity in seen:
            continue
        seen.add(identity)
        streams.append(form)
        _append_form_xobjects(form_get("/Resources"), streams, seen, object_count)


def _page_content_streams(page: Any, object_count: list[int]) -> list[Any]:
    streams: list[Any] = []
    seen: set[int] = set()
    for value in _pdf_values(page.get("/Contents")):
        _consume_pdf_object(object_count)
        stream = _resolve_pdf_object(value)
        if id(stream) not in seen:
            seen.add(id(stream))
            streams.append(stream)
    _append_form_xobjects(page.get("/Resources"), streams, seen, object_count)
    return streams


def _bound_page_fonts(
    page: Any,
    *,
    used: int,
    maximum: int,
    set_limit: Callable[[int], None],
    object_count: list[int],
    font_count: list[int],
    seen_cmaps: set[int],
) -> int:
    """Charge page fonts and their ToUnicode streams to the PDF budget."""
    resources = _resolve_pdf_object(page.get("/Resources"))
    get_value = getattr(resources, "get", None)
    if not callable(get_value):
        return used
    fonts = _resolve_pdf_object(get_value("/Font"))
    if not isinstance(fonts, Mapping):
        return used
    typed_fonts = cast("Mapping[object, Any]", fonts)
    for candidate in typed_fonts.values():
        font_count[0] += 1
        if font_count[0] > _DEFAULT_MAX_PDF_FONTS:
            raise MonitorError("PDF font resources exceed the size limit")
        _consume_pdf_object(object_count)
        font = _resolve_pdf_object(candidate)
        font_get = getattr(font, "get", None)
        if not callable(font_get):
            continue
        cmap = _resolve_pdf_object(font_get("/ToUnicode"))
        if cmap is None or id(cmap) in seen_cmaps:
            continue
        seen_cmaps.add(id(cmap))
        _consume_pdf_object(object_count)
        get_data = getattr(cmap, "get_data", None)
        if not callable(get_data):
            continue
        remaining = maximum - used
        set_limit(max(1, remaining))
        data = get_data()
        if not isinstance(data, bytes) or len(data) > remaining:
            raise MonitorError("PDF font mappings exceed the size limit")
        used += len(data)
    return used


def _bound_page_content(
    page: Any,
    *,
    used: int,
    maximum: int,
    set_limit: Callable[[int], None],
    object_count: list[int],
) -> int:
    """Expand each page content stream under a cumulative decompression budget."""
    for stream in _page_content_streams(page, object_count):
        get_data = getattr(stream, "get_data", None)
        if not callable(get_data):
            continue
        remaining = maximum - used
        if remaining <= 0:
            set_limit(1)
        else:
            set_limit(remaining)
        data = get_data()
        if not isinstance(data, bytes) or len(data) > remaining:
            raise MonitorError("PDF decompressed streams exceed the size limit")
        used += len(data)
    return used


def _pdf_link_destination(uri: str) -> str:
    """Return an opaque identity for one absolute HTTP(S) PDF link."""
    try:
        parsed = urlsplit(uri.strip())
    except ValueError as exc:
        raise MonitorError("PDF link destination is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        if parsed.scheme:
            return ""
        raise MonitorError("PDF relative link destinations are unsupported")
    if _destination_has_credentials(uri):
        raise MonitorError("PDF link destination contains credentials")
    return hashlib.sha256(uri.strip().encode("utf-8")).hexdigest()


def _pdf_annotation_link(annotation: Any, object_count: list[int]) -> str:
    _consume_pdf_object(object_count)
    obj = _resolve_pdf_object(annotation)
    if not isinstance(obj, Mapping):
        return ""
    typed_obj = cast("Mapping[object, Any]", obj)
    if typed_obj.get("/Subtype") != "/Link":
        return ""
    action = _resolve_pdf_object(typed_obj.get("/A"))
    if not isinstance(action, Mapping):
        return ""
    typed_action = cast("Mapping[object, Any]", action)
    if typed_action.get("/S") != "/URI":
        return ""
    uri = _resolve_pdf_object(typed_action.get("/URI"))
    return _pdf_link_destination(uri) if isinstance(uri, str) else ""


def _page_link_destinations(
    page: Any, object_count: list[int], annotation_count: list[int]
) -> list[str]:
    """Extract bounded opaque identities from page URI annotations."""
    annotations = _resolve_pdf_object(page.get("/Annots"))
    if annotations is None:
        return []
    if not isinstance(annotations, (list, tuple)):
        raise MonitorError("PDF annotations have an invalid structure")
    typed_annotations = cast("list[Any] | tuple[Any, ...]", annotations)
    if len(typed_annotations) > _DEFAULT_MAX_PDF_ANNOTATIONS - annotation_count[0]:
        raise MonitorError("PDF annotations exceed the size limit")
    lines: list[str] = []
    for annotation in typed_annotations:
        annotation_count[0] += 1
        digest = _pdf_annotation_link(annotation, object_count)
        if digest:
            lines.append(f"[link:sha256:{digest}]")
    return lines


def _extract_page_text(page: Any, *, extracted: int, maximum: int) -> tuple[str, int]:
    page_chars = 0

    def visitor_text(text: str, *args: Any) -> None:
        del args
        nonlocal page_chars
        page_chars += len(text)
        if extracted + page_chars > maximum:
            raise MonitorError("PDF extracted text exceeds the size limit")

    page_text = page.extract_text(visitor_text=visitor_text) or ""
    page_size = max(page_chars, len(page_text))
    if extracted + page_size > maximum:
        raise MonitorError("PDF extracted text exceeds the size limit")
    return page_text, page_size


def _normalize_pdf_content(
    body: bytes, *, max_decompressed_bytes: int, max_extracted_chars: int
) -> str:
    try:
        from pypdf import PdfReader  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        raise MonitorError("PDF normalization requires pypdf") from exc

    with _pypdf_output_limits(max_decompressed_bytes) as set_limit:
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise MonitorError("encrypted PDFs are not supported")
        object_count = _preflight_pdf_pages(reader)
        fragments: list[str] = []
        decompressed = 0
        extracted = 0
        font_count = [0]
        seen_cmaps: set[int] = set()
        annotation_count = [0]
        for page_number, page in enumerate(reader.pages, start=1):
            if page_number > _DEFAULT_MAX_PDF_PAGES:
                raise MonitorError("PDF page count exceeds the size limit")
            decompressed = _bound_page_content(
                page,
                used=decompressed,
                maximum=max_decompressed_bytes,
                set_limit=set_limit,
                object_count=object_count,
            )
            decompressed = _bound_page_fonts(
                page,
                used=decompressed,
                maximum=max_decompressed_bytes,
                set_limit=set_limit,
                object_count=object_count,
                font_count=font_count,
                seen_cmaps=seen_cmaps,
            )
            link_destinations = _page_link_destinations(
                page, object_count, annotation_count
            )
            page_text, page_size = _extract_page_text(
                page, extracted=extracted, maximum=max_extracted_chars
            )
            extracted += page_size
            fragments.append(page_text)
            fragments.extend(link_destinations)
        return _normalize_whitespace("\n".join(fragments))


def _normalize_pdf(
    body: bytes, *, max_decompressed_bytes: int, max_extracted_chars: int
) -> str:
    try:
        return _normalize_pdf_content(
            body,
            max_decompressed_bytes=max_decompressed_bytes,
            max_extracted_chars=max_extracted_chars,
        )
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError("PDF could not be normalized safely") from exc


def normalize_document(
    document: Document,
    *,
    max_pdf_decompressed_bytes: int = _DEFAULT_MAX_PDF_DECOMPRESSED_BYTES,
    max_pdf_extracted_chars: int = _DEFAULT_MAX_PDF_EXTRACTED_CHARS,
    links: dict[str, str] | None = None,
) -> str:
    """Convert HTML/XML, text, or PDF bytes into stable plain text."""
    if max_pdf_decompressed_bytes <= 0 or max_pdf_extracted_chars <= 0:
        raise MonitorError("numeric limits must be positive")
    content_type = _normalization_content_type(document)
    normalized_document = Document(
        document.body,
        document.source_url,
        content_type,
        document.charset,
    )
    if content_type == "application/pdf":
        return _normalize_pdf(
            document.body,
            max_decompressed_bytes=max_pdf_decompressed_bytes,
            max_extracted_chars=max_pdf_extracted_chars,
        )

    text = _decode_document(normalized_document)
    if content_type in _XML_CONTENT_TYPES:
        return _normalize_feed(
            text,
            max_chars=_DEFAULT_MAX_XML_CHARS,
            preserve_structure=content_type in _FEED_CONTENT_TYPES,
            base_url=document.source_url,
            links=links,
        )
    if content_type in _HTML_CONTENT_TYPES:
        parser = _TextExtractor(document.source_url)
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
        if links is not None:
            _merge_link_collection(links, parser.links)
    return _normalize_whitespace(text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return the longest prefix of ``value`` that fits a UTF-8 byte limit."""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    prefix = encoded[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return ""


def _bounded_diff(
    current: str,
    previous: str,
    *,
    max_diff_lines: int,
    max_diff_bytes: int,
) -> tuple[str, bool]:
    """Render a unified diff under output and pre-computation complexity bounds."""
    previous_line_count = _line_count(previous)
    current_line_count = _line_count(current)
    if previous_line_count * current_line_count > _DEFAULT_MAX_DIFF_COMPLEXITY:
        marker = "\n".join(
            [
                "--- previous",
                "+++ current",
                "@@ diff omitted: complexity limit exceeded @@",
            ][:max_diff_lines]
        )
        return _utf8_prefix(marker, max_diff_bytes), True
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()

    parts: list[str] = []
    size = 0
    for emitted_lines, line in enumerate(
        unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    ):
        if emitted_lines >= max_diff_lines:
            return "".join(parts), True
        separator = "" if not parts else "\n"
        candidate = f"{separator}{line}"
        candidate_size = len(candidate.encode("utf-8"))
        remaining = max_diff_bytes - size
        if candidate_size > remaining:
            return "".join(parts) + _utf8_prefix(candidate, remaining), True
        parts.append(candidate)
        size += candidate_size
    return "".join(parts), False


def _line_count(value: str) -> int:
    """Count lines using the same boundary characters as ``str.splitlines``."""
    count = 0
    last_end = 0
    for match in _LINE_BREAK_RE.finditer(value):
        count += 1
        last_end = match.end()
    if value and last_end != len(value):
        count += 1
    return count


def compare_text(
    current: str,
    previous: str | None,
    *,
    max_diff_lines: int,
    max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
    compare_navigation_markers: bool = False,
) -> dict[str, object]:
    """Return baseline/unchanged/changed metadata and a bounded unified diff."""
    if max_diff_lines <= 0 or max_diff_bytes <= 0:
        raise MonitorError("numeric limits must be positive")
    current_hash = _sha256(current)
    if previous is None:
        return {
            "status": "baseline",
            "sha256": current_hash,
            "previous_sha256": "",
            "diff": "",
            "diff_truncated": False,
        }

    previous_hash = _sha256(previous)
    comparison_current = current
    comparison_previous = previous
    if compare_navigation_markers:
        navigation_pair = _navigation_comparison_pair(current, previous)
        if navigation_pair is not None:
            comparison_current, comparison_previous = navigation_pair

    if comparison_previous == comparison_current:
        return {
            "status": "unchanged",
            "sha256": current_hash,
            "previous_sha256": previous_hash,
            "diff": "",
            "diff_truncated": False,
        }

    bounded, truncated = _bounded_diff(
        comparison_current,
        comparison_previous,
        max_diff_lines=max_diff_lines,
        max_diff_bytes=max_diff_bytes,
    )
    return {
        "status": "changed",
        "sha256": current_hash,
        "previous_sha256": previous_hash,
        "diff": bounded,
        "diff_truncated": truncated,
    }


# --- Hybrid CLI layer (workspace.py validation/grouping + JSON transport) ---


class WsumError(RuntimeError):
    """Expected CLI-layer input failure."""


_CONFIG_FIELDS = (
    "name",
    "url",
    "publisher",
    "category",
    "keywords",
    "priority",
    "enabled",
)
_MANAGED_FIELDS = (
    "last_checked_at",
    "last_hash",
    "last_snapshot_id",
    "last_summary",
    "last_material_at",
    "last_report_url",
    "last_status",
)
_ALLOWED_FIELDS = frozenset(_CONFIG_FIELDS + _MANAGED_FIELDS)
_INTEREST_TEXT_FIELDS = {"name", "publisher", "category", "keywords"}
_DITTO_TOKENS = {'"', "〃", "同上", "同左"}
_TARGET_ID_PART_RE = re.compile(r"[^a-z0-9]+")


def _validate_url(value: str) -> str:
    """workspace.py _validate_url, unchanged."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError as exc:
        raise WsumError("url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise WsumError("url must be an absolute HTTP(S) URL")
    if parsed.fragment:
        raise WsumError("url must not contain a fragment")
    if url_has_credentials(value):
        raise WsumError("url must not contain credentials")
    return value


def _target_id(url: str) -> str:
    """workspace.py _target_id, unchanged."""
    try:
        host = urlsplit(url).hostname or "target"
    except ValueError:
        host = "target"
    prefix = _TARGET_ID_PART_RE.sub("-", host.lower()).strip("-") or "target"
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"{prefix[:48]}-{digest}"


def _parse_enabled(value: str, row_number: int) -> bool:
    """workspace.py _parse_enabled, unchanged."""
    normalized = value.strip().lower()
    if normalized in {"", "true"}:
        return True
    if normalized == "false":
        return False
    raise WsumError(f"row {row_number}: enabled must be true or false")


def _cell_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise WsumError("cells must be scalar values")


def _normalize_target_row(
    header: list[str], raw_row: list[object], row_number: int
) -> tuple[str, dict[str, object]] | None:
    """workspace.py _normalize_target_row, adapted to in-memory rows."""
    if len(raw_row) > len(header):
        raise WsumError(f"row {row_number}: too many columns")
    values: dict[str, str] = {}
    for key, value in zip(header, raw_row, strict=False):
        values[key] = _cell_text(value).strip()
    if not any(values.values()):
        return None
    for field in _INTEREST_TEXT_FIELDS | {"url"}:
        if values.get(field) in _DITTO_TOKENS:
            raise WsumError(
                f"row {row_number}: {field}: replace ditto with an explicit value"
            )
    if not values.get("name"):
        raise WsumError(f"row {row_number}: name must be non-empty")
    try:
        url = _validate_url(values.get("url", ""))
    except WsumError as exc:
        raise WsumError(f"row {row_number}: {exc}") from exc
    priority_text = values.get("priority", "")
    priority = None
    if priority_text:
        if not re.fullmatch(r"[0-9]+", priority_text):
            raise WsumError(
                f"row {row_number}: priority must be a positive ASCII decimal integer"
            )
        priority = int(priority_text)
        if priority <= 0:
            raise WsumError(
                f"row {row_number}: priority must be a positive ASCII decimal integer"
            )
    interest: dict[str, object] = {
        field: values.get(field, "") for field in _INTEREST_TEXT_FIELDS
    }
    interest["priority"] = priority
    interest["enabled"] = _parse_enabled(values.get("enabled", ""), row_number)
    interest["row"] = row_number
    return url, interest


def _group_target_interests(
    rows: list[tuple[str, dict[str, object]]],
) -> list[dict[str, object]]:
    """workspace.py _group_target_interests, unchanged."""
    groups: dict[str, list[dict[str, object]]] = {}
    for url, interest in rows:
        groups.setdefault(url, []).append(interest)
    targets: list[dict[str, object]] = []
    target_ids: set[str] = set()
    for url, interests in groups.items():
        target_id = _target_id(url)
        if target_id in target_ids:
            raise WsumError("duplicate_target_id")
        target_ids.add(target_id)
        selected = next((item for item in interests if item["enabled"]), interests[0])
        enabled = any(item["enabled"] for item in interests)
        targets.append({
            "target_id": target_id,
            "name": selected["name"],
            "url": url,
            "enabled": enabled,
            "action": "monitor" if enabled else "skip_disabled",
            "interests": interests,
        })
    return targets


def validate_payload(payload: dict[str, object]) -> dict[str, object]:
    rows_value = payload.get("rows")
    csv_value = payload.get("csv")
    if rows_value is None and isinstance(csv_value, str):
        if len(csv_value.encode("utf-8")) > MAX_CSV_BYTES:
            raise WsumError("targets CSV exceeds the 1 MiB limit")
        rows_value = list(csv.reader(io.StringIO(csv_value), strict=True))
    if not isinstance(rows_value, list):
        raise WsumError("input must provide rows (array of arrays) or csv (text)")
    rows: list[list[object]] = []
    for r in cast("list[object]", rows_value):
        if not isinstance(r, list):
            raise WsumError("input must provide rows (array of arrays) or csv (text)")
        rows.append(cast("list[object]", r))
    if not rows:
        raise WsumError("targets CSV contains no header row")
    header = [_cell_text(v).strip() for v in rows[0]]
    if len(header) != len(set(header)):
        raise WsumError("targets CSV must have a unique header row")
    fields = set(header)
    if not {"name", "url"}.issubset(fields):
        raise WsumError("targets CSV requires name and url columns")
    if fields - _ALLOWED_FIELDS:
        raise WsumError("targets CSV contains unsupported columns")
    normalized: list[tuple[str, dict[str, object]]] = []
    for row_number, raw_row in enumerate(rows[1:], start=2):
        row = _normalize_target_row(header, raw_row, row_number)
        if row is not None:
            normalized.append(row)
    if not normalized:
        raise WsumError("targets CSV contains no targets")
    return {"ok": True, "errors": [], "targets": _group_target_interests(normalized)}


def _links_output(links: LinkCollection) -> dict[str, object]:
    return {
        "links": sorted(links.values()),
        "links_omitted": links.overflow_count + len(links.omitted_hashes),
    }


def normalize_payload(payload: dict[str, object]) -> dict[str, object]:
    source_url = str(payload.get("source_url", ""))
    content_type = str(payload.get("content_type", ""))
    charset = payload.get("charset")
    links = LinkCollection()
    body_b64 = payload.get("body_base64")
    text_value = payload.get("text")
    if isinstance(body_b64, str):
        try:
            body = base64.b64decode(body_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WsumError("body_base64 is not valid Base64") from exc
        if len(body) > _DEFAULT_MAX_BYTES:
            raise MonitorError("document exceeds the size limit")
        parsed_type, header_charset = _parse_content_type(content_type)
        document = Document(
            body=body,
            source_url=source_url,
            content_type=parsed_type,
            charset=charset if isinstance(charset, str) else header_charset,
        )
        normalized = normalize_document(document, links=links)
    elif isinstance(text_value, str):
        # Prompt-based fetch supplied already-decoded text: run the same
        # post-decode stages of normalize_document on it.
        text = text_value
        if len(text) > MAX_PAYLOAD_CHARS:
            raise WsumError(f"text exceeds the {MAX_PAYLOAD_CHARS} character limit")
        parsed_type, _ = _parse_content_type(content_type or "text/plain")
        if parsed_type in _XML_CONTENT_TYPES:
            normalized = _normalize_feed(
                text,
                max_chars=_DEFAULT_MAX_XML_CHARS,
                preserve_structure=parsed_type in _FEED_CONTENT_TYPES,
                base_url=source_url,
                links=links,
            )
        elif parsed_type in _HTML_CONTENT_TYPES:
            parser = _TextExtractor(source_url)
            parser.feed(text)
            parser.close()
            normalized = _normalize_whitespace("".join(parser.parts))
            _merge_link_collection(links, parser.links)
        elif parsed_type == "application/pdf":
            raise MonitorError(
                "PDF input requires body_base64 so the pypdf pipeline "
                "can run; prompt-extracted PDF text must be passed as "
                "text/plain instead"
            )
        else:
            normalized = _normalize_whitespace(text)
    else:
        raise WsumError("normalize requires body_base64 or text")
    if payload.get("navigation_links") is not None:
        navigation_links = _validate_navigation_links(payload["navigation_links"])
        normalized = _include_navigation_links(normalized, links, navigation_links)
    return {
        "normalized": normalized,
        "sha256": _sha256(normalized),
        "chars": len(normalized),
        **_links_output(links),
    }


def _payload_int(payload: dict[str, object], key: str, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WsumError(f"{key} must be an integer")
    return value


def compare_payload(payload: dict[str, object]) -> dict[str, object]:
    current = payload.get("current")
    if not isinstance(current, str):
        raise WsumError("compare requires current (normalized text)")
    if len(current) > MAX_PAYLOAD_CHARS:
        raise WsumError(f"current exceeds the {MAX_PAYLOAD_CHARS} character limit")
    previous = payload.get("previous")
    if previous is not None and not isinstance(previous, str):
        raise WsumError("previous must be normalized text or null")
    if isinstance(previous, str) and len(previous) > MAX_PAYLOAD_CHARS:
        raise WsumError(f"previous exceeds the {MAX_PAYLOAD_CHARS} character limit")
    max_diff_lines = _payload_int(payload, "max_diff_lines", _DEFAULT_MAX_DIFF_LINES)
    max_diff_bytes = _payload_int(payload, "max_diff_bytes", _DEFAULT_MAX_DIFF_BYTES)
    return compare_text(
        current,
        previous,
        max_diff_lines=max_diff_lines,
        max_diff_bytes=max_diff_bytes,
        compare_navigation_markers=bool(payload.get("compare_navigation_markers")),
    )


def extract_pdf_payload(payload: dict[str, object]) -> dict[str, object]:
    encoded = payload.get("pdf_base64")
    if not isinstance(encoded, str):
        raise WsumError("extract-pdf-text requires pdf_base64 (string)")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WsumError("pdf_base64 is not valid Base64") from exc
    document = Document(
        body=body,
        source_url=str(payload.get("source_url", "")),
        content_type="application/pdf",
    )
    normalized = normalize_document(document)
    return {
        "normalized": normalized,
        "sha256": _sha256(normalized),
        "chars": len(normalized),
    }


def _read_payload() -> dict[str, object]:
    raw: object = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise WsumError("stdin must be a JSON object")
    return cast("dict[str, object]", raw)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core", description=__doc__)
    parser.add_argument(
        "command",
        choices=("validate", "normalize", "compare", "extract-pdf-text"),
    )
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    handlers: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
        "validate": validate_payload,
        "normalize": normalize_payload,
        "compare": compare_payload,
        "extract-pdf-text": extract_pdf_payload,
    }
    try:
        result = handlers[args.command](_read_payload())
    except (MonitorError, WsumError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
