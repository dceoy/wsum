"""Safe, order-independent RSS and Atom normalization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from errors import MonitorError
from models import validate_http_url
from network_policy import canonicalize_fragment_identity, canonicalize_url

MAX_STABLE_ID_CHARS = 1_000

# Bound on link-destination annotations embedded in feed content fields
# (see ``_content_link_destination``), shared across the whole feed rather
# than per entry: the feed is normalized into a single stored/hashed/diffed
# blob, so the aggregate annotation output is what must stay bounded, not
# just any one entry's contribution. Sized above the default max_entries
# (1,000) so an ordinary feed with one link per entry never trips it.
MAX_CONTENT_LINK_ANNOTATIONS = 5_000
MAX_CONTENT_LINK_URL_CHARS = 300


class _ContentLinkBudget:
    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining


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


def _content_link_destination(
    href: str, base_url: str, budget: _ContentLinkBudget
) -> str:
    # A destination-only change inside embedded HTML (e.g. an <a href> in a
    # <description>/<content:encoded> body) must not be silently absorbed
    # into an identical normalized text/hash just because the anchor's
    # visible text is unchanged. Mirrors html_normalizer._link_destination:
    # resolve against the entry's (or feed's) validated link when available
    # and reject credential-bearing destinations via canonicalize_url.
    # Non-web schemes (mailto:, tel:, ...) are omitted, but a relative href
    # with no base to resolve against fails closed instead of silently
    # discarding what may be an http(s) destination update.
    #
    # canonicalize_url always strips the fragment, so a fragment-only href
    # (e.g. "#open") or a fragment-only destination change (e.g.
    # "/apply#step1" -> "/apply#step2") would otherwise normalize
    # identically to the unchanged content. The fragment is folded back
    # into the identity/hash via canonicalize_fragment_identity, which also
    # fails closed on credential-like fragments.
    value = href.strip()
    if not value:
        return ""
    resolved = urljoin(base_url, value) if base_url else value
    try:
        canonical, _ = canonicalize_url(resolved)
    except MonitorError:
        try:
            scheme = urlsplit(resolved).scheme.lower()
        except ValueError:
            scheme = resolved.partition(":")[0].lower()
        if not base_url and not scheme:
            raise MonitorError(
                "feed_content_relative_link",
                "feed entry content link has no base URL to resolve against",
            ) from None
        if scheme not in {"http", "https"}:
            return ""
        raise
    if budget.remaining <= 0:
        raise MonitorError(
            "feed_too_large", "feed entry has too many link destinations"
        )
    budget.remaining -= 1
    fragment = canonicalize_fragment_identity(urlsplit(resolved).fragment)
    identity = f"{canonical}#{fragment}" if fragment else canonical
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{identity[:MAX_CONTENT_LINK_URL_CHARS]} [sha256:{digest}]"


class _ContentTextParser(HTMLParser):
    """Like ``_TextParser`` but also annotates anchor destinations.

    RSS/Atom description/content fields commonly embed HTML as escaped
    text; ``_TextParser`` strips that markup down to visible text only, so
    a change confined to an <a href> (e.g. a link target bumped from
    ``/apply-v1`` to ``/apply-v2``) with unchanged link text would
    otherwise vanish from the normalized representation.
    """

    def __init__(self, base_url: str, budget: _ContentLinkBudget) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._base_url = base_url
        self._budget = budget
        self._anchor_hrefs: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = next(
                (value for name, value in attrs if name.lower() == "href"), None
            )
            self._anchor_hrefs.append(href)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "a":
            self._close_anchor()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_hrefs:
            self._close_anchor()

    def _close_anchor(self) -> None:
        href = self._anchor_hrefs.pop()
        if not href:
            return
        destination = _content_link_destination(href, self._base_url, self._budget)
        if destination:
            self.parts.append(f"[{destination}]")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def _clean_with_links(value: str, base_url: str, budget: _ContentLinkBudget) -> str:
    parser = _ContentTextParser(base_url, budget)
    parser.feed(value)
    parser.close()
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", " ".join(parser.parts)),
    ).strip()


def _element_link_destinations(
    element: ET.Element, base_url: str, budget: _ContentLinkBudget
) -> list[str]:
    # Atom permits inline markup as real XML children (<content
    # type="xhtml"><div>...<a href="...">...</a></div></content>), not just
    # HTML escaped into text. ``child.itertext()`` used above only sees text
    # nodes and drops attributes, so a real <a> element's href would
    # otherwise never reach ``_ContentTextParser`` at all. Walk the actual
    # element tree for any real anchor elements to cover that case too.
    destinations: list[str] = []
    for node in element.iter():
        if node is element or _local_name(node.tag) != "a":
            continue
        href = node.attrib.get("href")
        if href is None:
            continue
        destination = _content_link_destination(href, base_url, budget)
        if destination:
            destinations.append(destination)
    return destinations


def _children(element: ET.Element, *names: str) -> list[ET.Element]:
    allowed = set(names)
    return [child for child in element if _local_name(child.tag) in allowed]


def _first_text(element: ET.Element, *names: str) -> str:
    for child in _children(element, *names):
        value = "".join(child.itertext())
        if value.strip():
            return _clean(value)
    return ""


def _all_text_with_links(
    element: ET.Element, base_url: str, budget: _ContentLinkBudget, *names: str
) -> str:
    seen: list[str] = []
    for child in _children(element, *names):
        value = "".join(child.itertext())
        cleaned = _clean_with_links(value, base_url, budget) if value.strip() else ""
        structural = [
            f"[{destination}]"
            for destination in _element_link_destinations(child, base_url, budget)
        ]
        combined = " ".join(part for part in (cleaned, *structural) if part)
        if combined and combined not in seen:
            seen.append(combined)
    return " ".join(seen)


def _bounded_stable_id(value: str) -> str:
    """Keep a bounded feed-entry key without losing long-ID identity."""
    if len(value) <= MAX_STABLE_ID_CHARS:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    suffix = f" [sha256:{digest}]"
    prefix_limit = MAX_STABLE_ID_CHARS - len(suffix)
    return value[:prefix_limit] + suffix


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


def _feed_base_link(container: ET.Element) -> str:
    # A fallback base for entries that omit their own <link>: the feed/
    # channel's own alternate link is the "applicable inherited feed base"
    # for resolving relative content links (see _content_link_destination).
    # An invalid channel-level link should not fail the whole feed when
    # individual entries carry their own valid links, so treat it as "no
    # fallback available" rather than propagating the error here.
    try:
        return _entry_link(container)
    except MonitorError:
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
        channels = _children(root, "channel")
        feed_link = _feed_base_link(channels[0]) if channels else ""
    elif root_name == "feed":
        entries = [element for element in root if _local_name(element.tag) == "entry"]
        feed_kind = "atom"
        feed_link = _feed_base_link(root)
    else:
        raise MonitorError("feed_unsupported", "XML document is not RSS or Atom")
    if len(entries) > max_entries:
        raise MonitorError("feed_entry_limit", "feed entry count exceeds the limit")
    if not entries:
        raise MonitorError("empty_extraction", "feed contains no entries")

    link_budget = _ContentLinkBudget(MAX_CONTENT_LINK_ANNOTATIONS)
    normalized_entries: list[tuple[str, tuple[str, ...]]] = []
    for entry in entries:
        title = _first_text(entry, "title")
        link = _entry_link(entry)
        stable_id = _first_text(entry, "guid", "id") or link
        published = _first_text(entry, "pubdate", "published")
        updated = _first_text(entry, "updated")
        content = _all_text_with_links(
            entry,
            link or feed_link,
            link_budget,
            "description",
            "summary",
            "content",
            "encoded",
        )
        content_sources = (
            _external_content_sources(entry) if feed_kind == "atom" else ()
        )
        if not stable_id:
            source_identity = "\n".join(content_sources)
            stable_id = hashlib.sha256(
                f"{title}\n{published}\n{content}\n{source_identity}".encode()
            ).hexdigest()
        stable_id = _bounded_stable_id(stable_id)
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
