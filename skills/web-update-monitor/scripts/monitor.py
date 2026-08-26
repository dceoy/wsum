"""Fetch or read a document, normalize it, and compare it with a snapshot."""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import re
import socket
import sys
from dataclasses import dataclass
from difflib import unified_diff
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_DIFF_LINES = 200
_DEFAULT_TIMEOUT = 30.0
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")


class MonitorError(RuntimeError):
    """Expected input, network, or normalization failure."""


@dataclass(frozen=True, slots=True)
class Document:
    """Fetched or local document bytes plus source metadata."""

    body: bytes
    source_url: str
    content_type: str
    charset: str | None = None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        target = urljoin(req.full_url, newurl)
        _validate_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MonitorError("source must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MonitorError("embedded URL credentials are not allowed")

    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = {literal}
    else:
        try:
            default_port = 443 if parsed.scheme == "https" else 80
            infos = socket.getaddrinfo(parsed.hostname, parsed.port or default_port)
        except socket.gaierror as exc:
            raise MonitorError("hostname resolution failed") from exc
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}

    if not addresses or any(not address.is_global for address in addresses):
        raise MonitorError("source must resolve only to public IP addresses")


def _read_response_limited(response: Any, max_bytes: int) -> bytes:
    reader = getattr(response, "read", None)
    if reader is None:
        raise MonitorError("HTTP response is not readable")
    payload = reader(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise MonitorError("HTTP response did not return bytes")
    if len(payload) > max_bytes:
        raise MonitorError("document exceeds --max-bytes")
    return payload


def fetch_document(url: str, *, timeout: float, max_bytes: int) -> Document:
    """Fetch one public HTTP(S) document with bounded redirects and bytes."""
    _validate_public_url(url)
    opener = build_opener(_ValidatedRedirectHandler())
    request = Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "wsum/0.1 (+https://github.com/dceoy/wsum)",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            final_url = response.geturl()
            _validate_public_url(final_url)
            body = _read_response_limited(response, max_bytes)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset()
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError(f"fetch failed: {type(exc).__name__}") from exc
    return Document(body, final_url, content_type, charset)


def read_document(
    path: Path, *, source_url: str, content_type: str, max_bytes: int
) -> Document:
    """Read a local document supplied by the caller."""
    if path.is_symlink() or not path.is_file():
        raise MonitorError("--input must be a regular file")
    body = path.read_bytes()
    if len(body) > max_bytes:
        raise MonitorError("document exceeds --max-bytes")
    inferred = (content_type or _guess_content_type(path)).split(";", 1)[0].strip().lower()
    return Document(body, source_url or path.resolve().as_uri(), inferred)


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".html", ".htm", ".xml", ".rss", ".atom"}:
        return "text/html"
    return "text/plain"


def _normalize_whitespace(text: str) -> str:
    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def normalize_document(document: Document) -> str:
    """Convert HTML/XML, text, or PDF bytes into stable plain text."""
    if document.content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(document.body))
        return _normalize_whitespace(
            "\n".join(page.extract_text() or "" for page in reader.pages)
        )

    text = document.body.decode(document.charset or "utf-8", errors="replace")
    if document.content_type in {
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
    }:
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    return _normalize_whitespace(text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def compare_text(
    current: str, previous: str | None, *, max_diff_lines: int
) -> dict[str, object]:
    """Return baseline/unchanged/changed metadata and a bounded unified diff."""
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
    if previous_hash == current_hash:
        return {
            "status": "unchanged",
            "sha256": current_hash,
            "previous_sha256": previous_hash,
            "diff": "",
            "diff_truncated": False,
        }

    diff_lines = list(
        unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    truncated = len(diff_lines) > max_diff_lines
    bounded = diff_lines[:max_diff_lines]
    return {
        "status": "changed",
        "sha256": current_hash,
        "previous_sha256": previous_hash,
        "diff": "\n".join(bounded),
        "diff_truncated": truncated,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch/read, normalize, and diff one monitored document."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="public HTTP(S) URL to fetch")
    source.add_argument("--input", type=Path, help="local/rendered document")
    parser.add_argument(
        "--source-url", default="", help="canonical URL when using --input"
    )
    parser.add_argument("--content-type", default="", help="override input MIME type")
    parser.add_argument("--previous", type=Path, help="previous normalized snapshot")
    parser.add_argument("--output", type=Path, help="write current normalized snapshot")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--max-diff-lines", type=int, default=_DEFAULT_MAX_DIFF_LINES
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    """Execute one comparison and return its JSON-ready result."""
    if args.timeout <= 0 or args.max_bytes <= 0 or args.max_diff_lines <= 0:
        raise MonitorError("numeric limits must be positive")

    document = (
        fetch_document(args.url, timeout=args.timeout, max_bytes=args.max_bytes)
        if args.url
        else read_document(
            args.input,
            source_url=args.source_url,
            content_type=args.content_type,
            max_bytes=args.max_bytes,
        )
    )
    current = normalize_document(document)
    if not current:
        raise MonitorError("normalization produced empty content")

    previous = args.previous.read_text() if args.previous else None
    result = compare_text(current, previous, max_diff_lines=args.max_diff_lines)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(current)
    return {
        "source_url": document.source_url,
        "content_type": document.content_type,
        **result,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    try:
        result = run(_parser().parse_args(argv))
    except (MonitorError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
