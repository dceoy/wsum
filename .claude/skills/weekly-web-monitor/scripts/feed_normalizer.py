"""Safe, order-independent RSS and Atom normalization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit

from errors import MonitorError
from models import validate_http_url
from network_policy import canonicalize_url


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", " ".join(parser.parts)),
    ).strip()


def _children(element: ET.Element, *names: str) -> list[ET.Element]:
    allowed = set(names)
    return [child for child in element if _local_name(child.tag) in allowed]


def _first_text(element: ET.Element, *names: str) -> str:
    for child in _children(element, *names):
        value = "".join(child.itertext())
        if value.strip():
            return _clean(value)
    return ""


def _all_text(element: ET.Element, *names: str) -> str:
    seen: list[str] = []
    for child in _children(element, *names):
        value = "".join(child.itertext())
        if value.strip():
            cleaned = _clean(value)
            if cleaned not in seen:
                seen.append(cleaned)
    return " ".join(seen)


def _entry_link(element: ET.Element) -> str:
    for child in _children(element, "link"):
        href = child.attrib.get("href")
        if href is not None:
            # Atom defines an omitted rel as "alternate". Other relations
            # such as self and enclosure identify feed/API or media
            # resources rather than the entry's article destination.
            relation = child.attrib.get("rel", "alternate").strip().lower()
            if relation != "alternate":
                continue
            value = href.strip()
        else:
            # RSS uses the element text rather than an href attribute.
            value = "".join(child.itertext()).strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            # Relative IRIs (including those made absolute by xml:base) need
            # base-URI inheritance that this bounded normalizer does not
            # implement. Reject them explicitly instead of silently omitting
            # a destination whose later changes would then be invisible.
            raise MonitorError(
                "feed_relative_link",
                "feed entry link must be an absolute HTTP(S) URL",
            )
        try:
            return validate_http_url(value, "feed entry link")
        except MonitorError as exc:
            raise MonitorError(
                "feed_unsafe_link",
                "feed entry contains an unsafe link",
            ) from exc
    return ""


def _external_content_sources(element: ET.Element) -> tuple[str, ...]:
    sources: list[str] = []
    for child in _children(element, "content"):
        source = child.attrib.get("src")
        if source is None:
            continue
        value = source.strip()
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise MonitorError(
                "feed_unsafe_content_source",
                "feed entry contains an unsafe external content source",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            # Atom permits xml:base inheritance, but this bounded normalizer
            # does not implement base-URI inheritance. Reject relative
            # sources instead of silently omitting their identity.
            raise MonitorError(
                "feed_relative_content_source",
                "Atom content src must be an absolute HTTP(S) URL",
            )
        try:
            canonical, _ = canonicalize_url(value)
        except MonitorError as exc:
            raise MonitorError(
                "feed_unsafe_content_source",
                "feed entry contains an unsafe external content source",
            ) from exc
        if canonical not in sources:
            sources.append(canonical)
    return tuple(sources)


def normalize_feed(
    xml: bytes,
    *,
    max_entries: int = 1_000,
    max_input_bytes: int = 10_000_000,
    max_elements: int = 20_000,
) -> tuple[str, dict[str, str]]:
    if len(xml) > max_input_bytes:
        raise MonitorError("response_too_large", "feed exceeds the input size limit")
    if b"\x00" in xml[:512]:
        raise MonitorError(
            "feed_unsupported_encoding", "UTF-16/32 feeds are not supported"
        )
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise MonitorError(
            "feed_unsafe_xml", "DOCTYPE and entity declarations are forbidden"
        )
    try:
        parser = ET.XMLPullParser(events=("start",))
        root: ET.Element | None = None
        element_count = 0
        for offset in range(0, len(xml), 65_536):
            parser.feed(xml[offset : offset + 65_536])
            for _, element in parser.read_events():
                if root is None:
                    root = element
                element_count += 1
                if element_count > max_elements:
                    raise MonitorError(
                        "feed_element_limit",
                        "feed XML element count exceeds the limit",
                    )
        parser.close()
    except ET.ParseError as exc:
        raise MonitorError("feed_malformed", "feed XML is malformed") from exc
    if root is None:
        raise MonitorError("feed_malformed", "feed XML has no root element")
    root_name = _local_name(root.tag)
    if root_name in {"rss", "rdf"}:
        entries = [
            element for element in root.iter() if _local_name(element.tag) == "item"
        ]
        feed_kind = "rss"
    elif root_name == "feed":
        entries = [element for element in root if _local_name(element.tag) == "entry"]
        feed_kind = "atom"
    else:
        raise MonitorError("feed_unsupported", "XML document is not RSS or Atom")
    if len(entries) > max_entries:
        raise MonitorError("feed_entry_limit", "feed entry count exceeds the limit")
    if not entries:
        raise MonitorError("empty_extraction", "feed contains no entries")

    normalized_entries: list[tuple[str, tuple[str, ...]]] = []
    for entry in entries:
        title = _first_text(entry, "title")
        link = _entry_link(entry)
        stable_id = _first_text(entry, "guid", "id") or link
        published = _first_text(entry, "pubdate", "published")
        updated = _first_text(entry, "updated")
        content = _all_text(entry, "description", "summary", "content", "encoded")
        content_sources = (
            _external_content_sources(entry) if feed_kind == "atom" else ()
        )
        if not stable_id:
            source_identity = "\n".join(content_sources)
            stable_id = hashlib.sha256(
                f"{title}\n{published}\n{content}\n{source_identity}".encode()
            ).hexdigest()
        stable_id = stable_id[:1_000]
        fields = (
            f"ENTRY {stable_id}",
            f"TITLE {title}" if title else "",
            f"LINK {link}" if link else "",
            f"PUBLISHED {published}" if published else "",
            f"UPDATED {updated}" if updated else "",
            f"CONTENT {content}" if content else "",
            *(f"CONTENT_SRC {source}" for source in content_sources),
        )
        normalized_entries.append(
            (stable_id, tuple(field for field in fields if field))
        )
    normalized_entries.sort(key=lambda item: item[0])
    text = "\n".join(
        line for _, fields in normalized_entries for line in (*fields, "END ENTRY")
    )
    metadata = {
        "feed_kind": feed_kind,
        "entry_count": str(len(normalized_entries)),
    }
    return text, metadata
