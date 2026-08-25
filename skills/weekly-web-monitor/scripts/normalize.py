"""Content-type detection, normalization, and versioned SHA-256 hashing."""

from __future__ import annotations

import codecs
import hashlib
import json
import pathlib
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from errors import MonitorError
from feed_normalizer import normalize_feed
from html_normalizer import normalize_html
from pdf_normalizer import extract_pdf_text

if TYPE_CHECKING:
    from collections.abc import Iterable

NORMALIZATION_VERSION = "2026-01"
HASH_ALGORITHM = "sha256"
CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)""", re.IGNORECASE)

_MIN_INPUT_BYTES = 1_024
_MAX_INPUT_BYTES = 50_000_000


@dataclass(frozen=True, slots=True)
class NormalizedContent:
    """A validated, versioned, content-hashed normalization result."""

    kind: str
    text: str
    normalized_hash: str
    normalization_version: str
    hash_algorithm: str
    metadata: dict[str, str]

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable representation of this content.

        Returns:
            The record's fields, minus ``text`` when ``include_text`` is
            false (e.g. for audit metadata that must stay content-free).
        """
        value = asdict(self)
        if not include_text:
            value.pop("text")
        return value


def sniff_content_kind(body: bytes) -> str:
    """Detect a document's kind (pdf/feed/html/xml/text) from its bytes.

    Returns:
        One of ``"pdf"``, ``"feed"``, ``"html"``, ``"xml"``, ``"text"``.

    Raises:
        MonitorError: If ``body`` is empty.
    """
    sample = body[:8_192]
    sample = sample.removeprefix(codecs.BOM_UTF8)
    sample = sample.lstrip()
    lowered = sample.lower()
    if body.startswith(b"%PDF-"):
        return "pdf"
    if re.search(rb"<(?:\w+:)?(?:rss|feed|rdf)\b", lowered):
        return "feed"
    xml_declaration = re.match(rb"<\?xml\b[^>]*\?>", lowered)
    if xml_declaration is not None:
        after_declaration = lowered[xml_declaration.end() :].lstrip()
        if re.match(rb"<(?:!doctype\s+html|(?:\w+:)?html)\b", after_declaration):
            return "html"
        return "xml"
    if re.search(
        rb"<(?:!doctype\s+html|html|head|body|main|article|section|div|h[1-6]|p|table)\b",
        lowered,
    ):
        return "html"
    if sample:
        return "text"
    msg = "empty_response"
    raise MonitorError(msg, "response body is empty")


def declared_content_kind(content_type: str) -> str:
    """Map an HTTP Content-Type header to a normalization kind.

    Returns:
        One of ``"pdf"``, ``"feed"``, ``"html"``, ``"xml"``, ``"text"``, or
        ``"unsupported"``.
    """
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized == "application/pdf":
        return "pdf"
    if normalized in {"application/rss+xml", "application/atom+xml"}:
        return "feed"
    if normalized in {"text/html", "application/xhtml+xml"}:
        return "html"
    if normalized in {"application/xml", "text/xml"}:
        return "xml"
    if normalized == "text/plain" or not normalized:
        return "text"
    return "unsupported"


_ALLOWED_CHARSET_CODECS = frozenset(
    {
        "cp932",
        "cp1252",
        "euc_jp",
        "iso8859-1",
        "shift_jis",
        "utf-8",
        "utf-8-sig",
    }
)


def _decode_strict(body: bytes, codec_name: str) -> str:
    # errors="replace" would map distinct invalid byte sequences to the same
    # U+FFFD filler, so two different malformed responses could normalize to
    # identical text and hashes and silently mask a real change. Decoding
    # strictly and failing closed keeps a malformed body from ever being
    # treated as equivalent to another one.
    try:
        return body.decode(codec_name)
    except UnicodeDecodeError as exc:
        msg = "malformed_text"
        raise MonitorError(
            msg, "document bytes are not valid for the detected charset"
        ) from exc


def _decode_text(body: bytes, charset: str = "") -> str:
    declared = charset.strip()
    declared_unsupported = False
    if declared:
        try:
            codec = codecs.lookup(declared)
        except LookupError:
            codec = None
        # An unresolvable declared charset (garbage name) is treated as
        # absent rather than fatal, so a page that decoded fine via
        # BOM/body sniffing before the charset was threaded through does
        # not start hard-failing.
        if codec is not None:
            if codec.name in _ALLOWED_CHARSET_CODECS:
                return _decode_strict(body, codec.name)
            # A declared charset that *does* resolve to a real codec but
            # isn't allowlisted (e.g. iso-2022-jp) must not silently fall
            # through to the UTF-8 default below -- that would decode
            # legacy-encoded bytes as UTF-8 replacement garbage instead of
            # failing closed. BOM and in-body sniffing still get a chance
            # to rescue it first, same as a genuinely absent charset.
            declared_unsupported = True
    if body.startswith(codecs.BOM_UTF8):
        return _decode_strict(body, "utf-8-sig")
    if body.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        return _decode_strict(body, "utf-16")
    match = CHARSET_RE.search(body[:4_096])
    if match is None and declared_unsupported:
        msg = "unsupported_charset"
        raise MonitorError(msg, "document charset is unsupported")
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        codec = codecs.lookup(encoding)
    except LookupError as exc:
        msg = "unsupported_charset"
        raise MonitorError(
            msg, "document charset is unsupported"
        ) from exc
    if codec.name not in _ALLOWED_CHARSET_CODECS:
        msg = "unsupported_charset"
        raise MonitorError(msg, "document charset is unsupported")
    return _decode_strict(body, codec.name)


def _normalize_plain_text(body: bytes, charset: str = "") -> str:
    text = unicodedata.normalize("NFKC", _decode_text(body, charset))
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ]
    normalized = "\n".join(line for line in lines if line)
    if not normalized:
        msg = "empty_extraction"
        raise MonitorError(msg, "text extraction produced no content")
    return normalized


def hash_normalized(kind: str, text: str) -> str:
    """Compute the versioned SHA-256 hash of normalized content.

    Returns:
        The hex-encoded digest of ``kind`` and ``text`` under the current
        :data:`NORMALIZATION_VERSION`.

    Raises:
        MonitorError: If ``kind`` is invalid or ``text`` is empty.
    """
    if kind not in {"feed", "html", "pdf", "text"}:
        msg = "invalid_content"
        raise MonitorError(msg, "normalized content kind is invalid")
    if (
        not isinstance(text, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        # text ultimately originates from extraction/decoding helpers whose
        # own contracts are not statically enforced across module
        # boundaries; this stays a load-bearing runtime check.
        or not text
    ):
        msg = "empty_extraction"
        raise MonitorError(msg, "normalized text is empty")
    return hashlib.sha256(
        f"{NORMALIZATION_VERSION}\n{kind}\n{text}".encode()
    ).hexdigest()


def normalize_content(
    body: bytes,
    *,
    content_type: str = "",
    charset: str = "",
    base_url: str = "",
    include_selector: str = "",
    exclude_selectors: Iterable[str] = (),
    strict_selectors: bool = True,
    max_input_bytes: int = 10_000_000,
) -> NormalizedContent:
    """Detect, decode, extract, and hash ``body`` into normalized content.

    Returns:
        The validated, versioned, hashed normalization result.

    Raises:
        MonitorError: If ``body`` is not bytes, exceeds the input size
            limit, its declared and detected content types mismatch, its
            declared type is unsupported, or extraction/decoding fails.
    """
    if not isinstance(
        body, bytes
    ):  # pyright: ignore[reportUnnecessaryIsInstance]
        # body ultimately originates from an untrusted HTTP fetch; callers
        # may pass a non-bytes value at runtime despite the declared type.
        msg = "invalid_content"
        raise MonitorError(msg, "content must be bytes")
    if not _MIN_INPUT_BYTES <= max_input_bytes <= _MAX_INPUT_BYTES:
        msg = "invalid_configuration"
        raise MonitorError(
            msg, "normalization input limit is invalid"
        )
    if len(body) > max_input_bytes:
        msg = "response_too_large"
        raise MonitorError(
            msg, "content exceeds the normalization input limit"
        )
    sniffed = sniff_content_kind(body)
    declared = declared_content_kind(content_type)
    if declared == "unsupported":
        msg = "unsupported_content_type"
        raise MonitorError(
            msg, "declared content type is unsupported"
        )
    compatible = declared in {"text", sniffed} or (
        declared == "xml" and sniffed in {"feed", "xml"}
    )
    if not compatible:
        msg = "content_type_mismatch"
        raise MonitorError(
            msg,
            f"declared {declared} content does not match detected {sniffed} content",
        )
    metadata: dict[str, str] = {}
    if sniffed == "pdf":
        text, metadata = extract_pdf_text(body)
    elif sniffed == "feed":
        text, metadata = normalize_feed(body, base_url=base_url)
    elif sniffed == "xml":
        msg = "feed_unsupported"
        raise MonitorError(msg, "XML document is not RSS or Atom")
    elif sniffed == "html":
        text = normalize_html(
            _decode_text(body, charset),
            base_url=base_url,
            include_selector=include_selector,
            exclude_selectors=exclude_selectors,
            strict_selectors=strict_selectors,
        )
    else:
        text = _normalize_plain_text(body, charset)
    digest = hash_normalized(sniffed, text)
    return NormalizedContent(
        kind=sniffed,
        text=text,
        normalized_hash=digest,
        normalization_version=NORMALIZATION_VERSION,
        hash_algorithm=HASH_ALGORITHM,
        metadata=metadata,
    )


_MIN_ARGC = 2
_MAX_ARGC = 3


def _main(argv: list[str]) -> int:
    """Run the CLI entry point: normalize the file named in ``argv[1]``.

    On success, writes the JSON-encoded :class:`NormalizedContent` to
    stdout. On a handled failure, writes ``{"error": ...}`` JSON to stdout
    instead. Incorrect usage writes a usage message to stderr.

    Returns:
        0 on success, 1 if the input could not be read or normalized, 2
        for incorrect CLI usage.
    """
    if len(argv) not in {_MIN_ARGC, _MAX_ARGC}:
        sys.stderr.write("usage: normalize.py INPUT [CONTENT_TYPE]\n")
        return 2
    try:
        result = normalize_content(
            pathlib.Path(argv[1]).read_bytes(),
            content_type=argv[2] if len(argv) == _MAX_ARGC else "",
        )
    except (OSError, MonitorError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, MonitorError)
            else {
                "code": "input_read_failed",
                "message": "input file could not be read",
                "retryable": False,
            }
        )
        json.dump({"error": error}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 1
    json.dump(result.as_dict(), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
