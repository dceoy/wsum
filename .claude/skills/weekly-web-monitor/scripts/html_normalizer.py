"""Dependency-free HTML extraction with a deliberately small selector grammar."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser

from errors import MonitorError

VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
NOISE_TAGS = frozenset(
    {
        "script",
        "style",
        "nav",
        "footer",
        "aside",
        "form",
        "noscript",
        "iframe",
        "template",
        "svg",
        "canvas",
    }
)
# ``<header>`` is page chrome only when it isn't nested in a content
# container: ``<article><header><h1>title</h1></header>…</article>`` is a
# section heading, not boilerplate, so it must survive noise stripping.
CONTENT_SECTIONING_TAGS = frozenset({"article", "section", "main"})
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "pre",
        "section",
        "summary",
        "table",
    }
)
NOISE_TOKEN_RE = re.compile(
    r"(?:^|[-_])(?:ad|ads|advert|banner|breadcrumb|cookie|footer|"
    r"menu|modal|nav|newsletter|popup|promo|share|sidebar|social|tracking)(?:$|[-_])",
    re.IGNORECASE,
)
# Checked separately from ``NOISE_TOKEN_RE`` so it can honor the same
# nested-content exception as the ``header`` tag rule below: a class/id
# like ``article-header`` on a heading nested in ``article``/``section``/
# ``main`` is a content sub-heading, not page chrome.
HEADER_NOISE_TOKEN_RE = re.compile(r"(?:^|[-_])header(?:$|[-_])", re.IGNORECASE)
TIMESTAMP_ONLY_RE = re.compile(
    r"^(?:last\s+)?(?:updated|modified|published)\s*[:：]?\s*"
    r"(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?)$",
    re.IGNORECASE,
)
COOKIE_TEXT_RE = re.compile(
    r"\b(?:accept|allow|manage|reject)\b.{0,80}\b(?:cookie|cookies)\b",
    re.IGNORECASE,
)
BOILERPLATE_TEXT_RE = re.compile(
    r"(?:all rights reserved|copyright|skip to (?:main )?content|"
    r"無断転載|著作権|本文へ移動)",
    re.IGNORECASE,
)
SIMPLE_HEAD_RE = re.compile(r"^(?:\*|[A-Za-z][A-Za-z0-9_-]*)?")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(
        self, tag: str, attrs: dict[str, str], parent: Node | None = None
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        normalized_attrs = {name.lower(): value or "" for name, value in attrs if name}
        node = Node(tag, normalized_attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        cursor: Node | None = self.current
        while cursor is not None and cursor is not self.root:
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent

    def handle_data(self, data: str) -> None:
        if data:
            self.current.children.append(data)


class SimpleSelector:
    __slots__ = ("tag", "element_id", "classes", "attributes")

    def __init__(
        self,
        tag: str,
        element_id: str,
        classes: tuple[str, ...],
        attributes: tuple[tuple[str, str | None], ...],
    ) -> None:
        self.tag = tag
        self.element_id = element_id
        self.classes = classes
        self.attributes = attributes

    def matches(self, node: Node) -> bool:
        if self.tag not in {"", "*"} and node.tag != self.tag:
            return False
        if self.element_id and node.attrs.get("id", "") != self.element_id:
            return False
        class_values = set(node.attrs.get("class", "").split())
        if any(value not in class_values for value in self.classes):
            return False
        for name, expected in self.attributes:
            if name not in node.attrs:
                return False
            if expected is not None and node.attrs[name] != expected:
                return False
        return True


def _parse_simple_selector(value: str) -> SimpleSelector:
    if not value:
        raise MonitorError("selector_invalid", "selector contains an empty component")
    match = SIMPLE_HEAD_RE.match(value)
    assert match is not None
    tag = match.group(0).lower()
    position = len(tag)
    element_id = ""
    classes: list[str] = []
    attributes: list[tuple[str, str | None]] = []
    while position < len(value):
        marker = value[position]
        if marker in {"#", "."}:
            name_match = NAME_RE.match(value[position + 1 :])
            if not name_match:
                raise MonitorError("selector_invalid", "selector name is invalid")
            name = name_match.group(0)
            if marker == "#":
                if element_id:
                    raise MonitorError(
                        "selector_invalid", "selector has more than one id"
                    )
                element_id = name
            else:
                classes.append(name)
            position += len(name) + 1
            continue
        if marker == "[":
            end = value.find("]", position + 1)
            if end < 0:
                raise MonitorError("selector_invalid", "selector attribute is unclosed")
            expression = value[position + 1 : end].strip()
            if "=" in expression:
                name, expected = expression.split("=", 1)
                name = name.strip().lower()
                expected = expected.strip().strip("\"'")
            else:
                name, expected = expression.lower(), None
            if (
                not NAME_RE.fullmatch(name)
                or expected is not None
                and len(expected) > 200
            ):
                raise MonitorError("selector_invalid", "selector attribute is invalid")
            attributes.append((name, expected))
            position = end + 1
            continue
        raise MonitorError(
            "selector_invalid",
            "only tag, id, class, attribute, and descendant selectors are supported",
        )
    if not tag and not element_id and not classes and not attributes:
        raise MonitorError("selector_invalid", "selector component is empty")
    return SimpleSelector(tag, element_id, tuple(classes), tuple(attributes))


def parse_selector(value: str) -> tuple[SimpleSelector, ...]:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise MonitorError("selector_invalid", "selector is empty or too long")
    if any(char in value for char in ",>+~:{}"):
        raise MonitorError(
            "selector_invalid",
            "selector uses unsupported combinators or pseudo-selectors",
        )
    return tuple(_parse_simple_selector(part) for part in value.split())


def iter_nodes(root: Node) -> Iterator[Node]:
    for child in root.children:
        if isinstance(child, Node):
            yield child
            yield from iter_nodes(child)


def _matches_selector(node: Node, selector: tuple[SimpleSelector, ...]) -> bool:
    if not selector[-1].matches(node):
        return False
    cursor = node.parent
    for component in reversed(selector[:-1]):
        while cursor is not None and not component.matches(cursor):
            cursor = cursor.parent
        if cursor is None:
            return False
        cursor = cursor.parent
    return True


def select(root: Node, value: str) -> list[Node]:
    selector = parse_selector(value)
    return [node for node in iter_nodes(root) if _matches_selector(node, selector)]


def _has_content_ancestor(node: Node) -> bool:
    cursor = node.parent
    while cursor is not None:
        if cursor.tag in CONTENT_SECTIONING_TAGS:
            return True
        cursor = cursor.parent
    return False


def _is_noise(node: Node) -> bool:
    if node.tag in NOISE_TAGS:
        return True
    if node.tag == "header" and not _has_content_ancestor(node):
        return True
    if "hidden" in node.attrs or node.attrs.get("aria-hidden", "").lower() == "true":
        return True
    if node.attrs.get("role", "").lower() in {
        "banner",
        "complementary",
        "contentinfo",
        "dialog",
        "navigation",
    }:
        return True
    tokens = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}"
    if NOISE_TOKEN_RE.search(tokens):
        return True
    return bool(HEADER_NOISE_TOKEN_RE.search(tokens)) and not _has_content_ancestor(node)


def _text_content(node: Node, excluded: set[Node]) -> str:
    if node in excluded or _is_noise(node):
        return ""
    pieces: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            pieces.append(child)
        elif child.tag == "br":
            pieces.append("\n")
        else:
            pieces.append(_text_content(child, excluded))
    return " ".join(pieces)


def _clean_line(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", value).strip()


def _table_lines(table: Node, excluded: set[Node]) -> list[str]:
    lines: list[str] = []
    for row in (node for node in iter_nodes(table) if node.tag == "tr"):
        cells = [
            _clean_line(_text_content(child, excluded))
            for child in row.children
            if isinstance(child, Node) and child.tag in {"td", "th"}
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _extract_lines(root: Node, excluded: set[Node]) -> list[str]:
    lines: list[str] = []

    def visit(node: Node) -> None:
        if node in excluded or _is_noise(node):
            return
        if node.tag == "table":
            lines.extend(_table_lines(node, excluded))
            return
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _clean_line(_text_content(node, excluded))
            if text:
                lines.append(f"{'#' * int(node.tag[1])} {text}")
            return
        if node.tag == "li":
            text = _clean_line(_text_content(node, excluded))
            if text:
                lines.append(f"- {text}")
            return
        if node.tag in {
            "address",
            "blockquote",
            "dd",
            "dt",
            "figcaption",
            "p",
            "pre",
            "summary",
        }:
            text = _clean_line(_text_content(node, excluded))
            if text:
                lines.append(text)
            return
        child_nodes = [child for child in node.children if isinstance(child, Node)]
        has_block_child = any(child.tag in BLOCK_TAGS for child in child_nodes)
        direct_text = _clean_line(
            " ".join(child for child in node.children if isinstance(child, str))
        )
        if direct_text and not has_block_child:
            full_text = _clean_line(_text_content(node, excluded))
            if full_text:
                lines.append(full_text)
                return
        if direct_text and has_block_child:
            # The element mixes its own text with block-level children
            # (e.g. ``<main>Status text<div>Details</div></main>``).
            # Emitting it here keeps a change to that text from being
            # silently dropped from the normalized hash, even though the
            # block children below are still emitted as separate lines.
            lines.append(direct_text)
        for child in child_nodes:
            visit(child)

    visit(root)
    return lines


def normalize_html(
    html: str,
    *,
    include_selector: str = "",
    exclude_selectors: Iterable[str] = (),
    strict_selectors: bool = True,
) -> str:
    if not isinstance(html, str):
        raise MonitorError("html_invalid", "HTML input must be text")
    parser = _TreeParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise MonitorError(
            "html_malformed", "HTML parser rejected the document"
        ) from exc

    roots = [parser.root]
    if include_selector:
        roots = select(parser.root, include_selector)
        if not roots:
            raise MonitorError(
                "selector_no_match", "include_selector matched no elements"
            )
    excluded: set[Node] = set()
    for selector in exclude_selectors:
        matches = select(parser.root, selector)
        if strict_selectors and not matches:
            raise MonitorError(
                "selector_no_match", "an exclude_selector matched no elements"
            )
        excluded.update(matches)

    raw_lines: list[str] = []
    for root in roots:
        raw_lines.extend(_extract_lines(root, excluded))
    if not raw_lines:
        fallback = " ".join(
            _clean_line(_text_content(root, excluded)) for root in roots
        ).strip()
        if fallback:
            raw_lines.append(fallback)

    lines: list[str] = []
    for value in raw_lines:
        line = _clean_line(value)
        if (
            not line
            or TIMESTAMP_ONLY_RE.fullmatch(line)
            or len(line) <= 300
            and COOKIE_TEXT_RE.search(line)
        ):
            continue
        if BOILERPLATE_TEXT_RE.search(line) and line in lines:
            continue
        lines.append(line)
    normalized = "\n".join(lines).strip()
    if not normalized:
        raise MonitorError(
            "empty_extraction", "HTML extraction produced no meaningful content"
        )
    return normalized
