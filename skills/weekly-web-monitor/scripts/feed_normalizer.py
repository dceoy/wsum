"""Safe, order-independent RSS and Atom normalization."""

from __future__ import annotations

import hashlib
import operator
import re
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlsplit

from errors import MonitorError
from models import validate_http_url
from network_policy import canonicalize_fragment_identity, canonicalize_url

if TYPE_CHECKING:
    from collections.abc import Iterator

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


_XML_BASE_ATTR = "{http://www.w3.org/XML/1998/namespace}base"


def _xml_base_scope(element: ET.Element, parent_base: str) -> str:
    """Resolve the base URI in effect at ``element`` against its xml:base.

    XML Base lets ``xml:base`` be set on the feed root, the channel/feed
    container, an entry, or an individual content element to override the
    base URI used for resolving relative references in that scope,
    independent of any <link>. Only a present xml:base changes the
    inherited base, so a feed with no xml:base anywhere resolves exactly as
    it did before this attribute was recognized (``link``/the feed's own
    alternate link stays the sole base).
    """
    value = element.attrib.get(_XML_BASE_ATTR, "").strip()
    if not value:
        return parent_base
    return urljoin(parent_base, value) if parent_base else value


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
            msg = "feed_content_relative_link"
            raise MonitorError(
                msg,
                "feed entry content link has no base URL to resolve against",
            ) from None
        if scheme not in {"http", "https"}:
            return ""
        raise
    if budget.remaining <= 0:
        msg = "feed_too_large"
        raise MonitorError(
            msg, "feed entry has too many link destinations"
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

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "a":
            self._close_anchor()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_hrefs:
            self._close_anchor()

    def _close_anchor(self) -> None:
        self._emit_anchor(self._anchor_hrefs.pop())

    def _emit_anchor(self, href: str | None) -> None:
        if not href:
            return
        destination = _content_link_destination(href, self._base_url, self._budget)
        if destination:
            self.parts.append(f"[{destination}]")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def close(self) -> None:
        # HTMLParser.close() does not synthesize missing end-tag events, so
        # a malformed anchor with no closing </a> (common in embedded feed
        # HTML, e.g. "<a href="/apply-v1">Apply") would otherwise leave its
        # href stuck in _anchor_hrefs and never reach _content_link_destination.
        # Flush any still-open anchors in document order instead of silently
        # dropping their destinations.
        super().close()
        while self._anchor_hrefs:
            self._emit_anchor(self._anchor_hrefs.pop(0))


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
    #
    # XML Base can be overridden on any descendant, not just re-declared at
    # this content element, so the effective base must be recomputed while
    # walking (an ``xml:base`` on an intermediate wrapper, or on the anchor
    # itself, changes only that subtree's resolution) rather than resolving
    # every anchor against a single content-level ``base_url``. An explicit
    # stack keeps this bounded by ``max_elements`` rather than by the
    # interpreter's call-stack recursion limit, which a deeply nested (but
    # otherwise small) content tree could otherwise exceed.
    destinations: list[str] = []
    stack: list[tuple[ET.Element, str]] = [
        (child, _xml_base_scope(child, base_url)) for child in reversed(list(element))
    ]
    while stack:
        node, base = stack.pop()
        if _local_name(node.tag) == "a":
            href = node.attrib.get("href")
            if href is not None:
                destination = _content_link_destination(href, base, budget)
                if destination:
                    destinations.append(destination)
        stack.extend(
            (child, _xml_base_scope(child, base)) for child in reversed(list(node))
        )
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
        # A <description>/<content:encoded>/... child can carry its own
        # xml:base distinct from its entry's, so the effective base must be
        # recomputed per child rather than reusing the entry-level base for
        # all of them.
        child_base = _xml_base_scope(child, base_url)
        value = "".join(child.itertext())
        cleaned = _clean_with_links(value, child_base, budget) if value.strip() else ""
        structural = [
            f"[{destination}]"
            for destination in _element_link_destinations(child, child_base, budget)
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
            msg = "feed_relative_link"
            raise MonitorError(
                msg,
                "feed entry link must be an absolute HTTP(S) URL",
            )
        try:
            return validate_http_url(value, "feed entry link")
        except MonitorError as exc:
            msg = "feed_unsafe_link"
            raise MonitorError(
                msg,
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
            msg = "feed_unsafe_content_source"
            raise MonitorError(
                msg,
                "feed entry contains an unsafe external content source",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            # Atom permits xml:base inheritance, but this bounded normalizer
            # does not implement base-URI inheritance. Reject relative
            # sources instead of silently omitting their identity.
            msg = "feed_relative_content_source"
            raise MonitorError(
                msg,
                "Atom content src must be an absolute HTTP(S) URL",
            )
        try:
            canonical, _ = canonicalize_url(value)
        except MonitorError as exc:
            msg = "feed_unsafe_content_source"
            raise MonitorError(
                msg,
                "feed entry contains an unsafe external content source",
            ) from exc
        # canonicalize_url always strips the fragment, so a content src
        # differing only by fragment (e.g. "#v1" -> "#v2") must not collapse
        # to the same stored identity. Same fragment-identity handling as
        # the embedded content-link destinations above.
        fragment = canonicalize_fragment_identity(parsed.fragment)
        identity = f"{canonical}#{fragment}" if fragment else canonical
        if identity not in sources:
            sources.append(identity)
    return tuple(sources)


def normalize_feed(
    xml: bytes,
    *,
    base_url: str = "",
    max_entries: int = 1_000,
    max_input_bytes: int = 10_000_000,
    max_elements: int = 20_000,
) -> tuple[str, dict[str, str]]:
    if len(xml) > max_input_bytes:
        msg = "response_too_large"
        raise MonitorError(msg, "feed exceeds the input size limit")
    if b"\x00" in xml[:512]:
        msg = "feed_unsupported_encoding"
        raise MonitorError(
            msg, "UTF-16/32 feeds are not supported"
        )
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        msg = "feed_unsafe_xml"
        raise MonitorError(
            msg, "DOCTYPE and entity declarations are forbidden"
        )
    try:
        parser = ET.XMLPullParser(events=("start",))
        root: ET.Element | None = None
        element_count = 0
        for offset in range(0, len(xml), 65_536):
            parser.feed(xml[offset : offset + 65_536])
            events = cast("Iterator[tuple[str, ET.Element]]", parser.read_events())
            for _, element in events:
                if root is None:
                    root = element
                element_count += 1
                if element_count > max_elements:
                    msg = "feed_element_limit"
                    raise MonitorError(
                        msg,
                        "feed XML element count exceeds the limit",
                    )
        parser.close()
    except ET.ParseError as exc:
        msg = "feed_malformed"
        raise MonitorError(msg, "feed XML is malformed") from exc
    if root is None:
        msg = "feed_malformed"
        raise MonitorError(msg, "feed XML has no root element")
    root_name = _local_name(root.tag)
    channels: list[ET.Element] = []
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
        msg = "feed_unsupported"
        raise MonitorError(msg, "XML document is not RSS or Atom")
    if len(entries) > max_entries:
        msg = "feed_entry_limit"
        raise MonitorError(msg, "feed entry count exceeds the limit")
    if not entries:
        msg = "empty_extraction"
        raise MonitorError(msg, "feed contains no entries")

    link_budget = _ContentLinkBudget(MAX_CONTENT_LINK_ANNOTATIONS)
    normalized_entries: list[tuple[str, tuple[str, ...]]] = []
    for entry in entries:
        title = _first_text(entry, "title")
        link = _entry_link(entry)
        stable_id = _first_text(entry, "guid", "id") or link
        published = _first_text(entry, "pubdate", "published")
        updated = _first_text(entry, "updated")
        # xml:base on the feed root, channel/feed container, or entry
        # overrides the base URI used to resolve relative content links,
        # independent of <link> (see _xml_base_scope). The document's own
        # fetched URL is the base a present root-level xml:base resolves
        # against; with no root-level xml:base, `base_url` must not replace
        # `link or feed_link` as the inherited base, or a feed with no
        # xml:base anywhere would resolve every entry against the
        # unchanging document URL and silently miss a destination change
        # confined to the channel/feed <link>. `base_url` is still the
        # right fallback when there is no link at all to prefer over it.
        root_xml_base = root.attrib.get(_XML_BASE_ATTR, "").strip()
        entry_base = _xml_base_scope(
            root, base_url if root_xml_base else (link or feed_link or base_url)
        )
        if root_name in {"rss", "rdf"} and channels:
            entry_base = _xml_base_scope(channels[0], entry_base)
        entry_base = _xml_base_scope(entry, entry_base)
        content = _all_text_with_links(
            entry,
            entry_base,
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
    normalized_entries.sort(key=operator.itemgetter(0))
    text = "\n".join(
        line for _, fields in normalized_entries for line in (*fields, "END ENTRY")
    )
    metadata = {
        "feed_kind": feed_kind,
        "entry_count": str(len(normalized_entries)),
    }
    return text, metadata
