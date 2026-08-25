"""Dependency-free HTML extraction with a deliberately small selector grammar."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from errors import MonitorError
from network_policy import canonicalize_fragment_identity, canonicalize_url

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
    r"menu|modal|nav|newsletter|popup|sidebar|social|tracking)(?:$|[-_])",
    re.IGNORECASE,
)
# Checked separately from ``NOISE_TOKEN_RE`` so it can honor the same
# nested-content exception as the ``header`` tag rule below: a class/id
# like ``article-header`` on a heading nested in ``article``/``section``/
# ``main`` is a content sub-heading, not page chrome.
HEADER_NOISE_TOKEN_RE = re.compile(r"(?:^|[-_])header(?:$|[-_])", re.IGNORECASE)
# ``share`` alone is not a safe generic noise token: business content such
# as a ``share-price`` widget uses the same word as a social-share button.
# Only treat it as boilerplate when paired with an explicit social-sharing
# qualifier (e.g. ``social-share``, ``share-widget``) or a known share
# plugin name, so a bare business compound like ``share-price`` survives.
_SHARE_QUALIFIER = (
    r"(?:social|this|button|buttons|btn|icon|icons|widget|widgets|bar|"
    r"tool|tools|link|links|box)"
)
# ``tokens`` below joins the id and class attributes with a plain space, so
# a class value at the very start of that joined string is bounded by a
# space rather than by ``^`` or ``[-_]`` -- the boundary classes here must
# include ``\s`` or a leading token like ``share-widget`` would silently
# fail to match.
SHARE_NOISE_TOKEN_RE = re.compile(
    rf"(?:^|[-_\s])(?:{_SHARE_QUALIFIER}[-_]share|share[-_]{_SHARE_QUALIFIER}|"
    rf"sharethis|addthis|addtoany)(?:$|[-_\s])",
    re.IGNORECASE,
)
# ``promo`` alone is not a safe generic noise token: business content such
# as a ``promo-code`` field or a ``product-promo-price`` widget uses the
# same word as a marketing chrome banner/popup. Only treat it as boilerplate
# when paired with an explicit chrome/widget qualifier, so a bare business
# compound like ``promo-code`` survives.
_PROMO_QUALIFIER = (
    r"(?:banner|bar|modal|overlay|popup|strip|widget|widgets|top|site|"
    r"header|footer)"
)
PROMO_NOISE_TOKEN_RE = re.compile(
    rf"(?:^|[-_\s])(?:{_PROMO_QUALIFIER}[-_]promo|promo[-_]{_PROMO_QUALIFIER})"
    rf"(?:$|[-_\s])",
    re.IGNORECASE,
)
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


# Bounds on the parsed tree, independent of the raw byte-size cap applied
# before parsing (normalize.normalize_content). A few megabytes of tiny or
# deeply nested tags can still produce hundreds of thousands of Node objects
# or nesting deep enough to blow the interpreter's recursion limit in the
# recursive iter_nodes()/_text_content()/visit() tree walks below, so both
# node count and nesting depth are tracked and rejected during parsing
# rather than left unbounded. MAX_DEPTH is kept well under
# sys.getrecursionlimit() (default 1000): _extract_lines.visit() recurses to
# tree depth and calls _text_content(), which recurses again, so worst-case
# stack usage is roughly 2x MAX_DEPTH.
MAX_NODES = 50_000
MAX_DEPTH = 200

# Bounds on link-destination annotations (see _link_destination): a
# link-heavy page must not be able to inflate the normalized text/hash
# input without limit just because every anchor gets a canonical-URL
# suffix appended.
MAX_LINK_ANNOTATIONS = 500
MAX_LINK_URL_CHARS = 300


class _LinkContext:
    __slots__ = ("base_url", "remaining")

    def __init__(self, base_url: str, remaining: int) -> None:
        self.base_url = base_url
        self.remaining = remaining


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
        self.node_count = 0
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        normalized_attrs = {name.lower(): value or "" for name, value in attrs if name}
        self.node_count += 1
        if self.node_count > MAX_NODES:
            raise MonitorError("html_too_large", "HTML document has too many elements")
        node = Node(tag, normalized_attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            if self.depth >= MAX_DEPTH:
                raise MonitorError(
                    "html_too_large", "HTML document nesting is too deep"
                )
            self.current = node
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.current = self.current.parent or self.root
            self.depth -= 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        cursor: Node | None = self.current
        levels = 0
        while cursor is not None and cursor is not self.root:
            levels += 1
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                self.depth -= levels
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


def _has_content_descendant(node: Node) -> bool:
    return any(child.tag in CONTENT_SECTIONING_TAGS for child in iter_nodes(node))


def _is_noise(node: Node) -> bool:
    if node.tag in NOISE_TAGS:
        return True
    if node.tag == "header" and not _has_content_ancestor(node):
        return True
    if (
        node.tag == "form"
        and not _has_content_ancestor(node)
        and not _has_content_descendant(node)
    ):
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
    if (
        NOISE_TOKEN_RE.search(tokens)
        or SHARE_NOISE_TOKEN_RE.search(tokens)
        or PROMO_NOISE_TOKEN_RE.search(tokens)
    ):
        return True
    return bool(HEADER_NOISE_TOKEN_RE.search(tokens)) and not _has_content_ancestor(
        node
    )


def _link_destination(node: Node, attr: str, ctx: _LinkContext) -> str:
    # Canonicalize (and, via canonicalize_url, reject credential-bearing)
    # destination URLs so a same-text link/form-target change (e.g.
    # /apply-v1 -> /apply-v2) is not silently absorbed into an identical
    # normalized text/hash. Resolution requires a base URL and a bounded
    # remaining budget (see MAX_LINK_ANNOTATIONS). Policy-denied HTTP(S)
    # destinations fail closed so credentials cannot enter stored artifacts,
    # even as offline-testable unsalted digests. Non-web schemes are omitted.
    #
    # canonicalize_url always strips the fragment (it is never sent to the
    # server), so a fragment-only href (e.g. "#open") or a fragment-only
    # destination change (e.g. "/apply#step1" -> "/apply#step2") would
    # otherwise normalize identically to the unchanged page. The fragment
    # is folded back into the identity/hash separately via
    # canonicalize_fragment_identity, which also fails closed on
    # credential-like fragments (e.g. OAuth implicit-flow "#access_token=").
    if not ctx.base_url:
        return ""
    value = node.attrs.get(attr, "").strip()
    if not value:
        return ""
    resolved = urljoin(ctx.base_url, value)
    try:
        canonical, _ = canonicalize_url(resolved)
    except MonitorError:
        try:
            scheme = urlsplit(resolved).scheme.lower()
        except ValueError:
            scheme = resolved.partition(":")[0].lower()
        if scheme not in {"http", "https"}:
            return ""
        raise
    if ctx.remaining <= 0:
        raise MonitorError(
            "html_too_large", "HTML document has too many link destinations"
        )
    ctx.remaining -= 1
    fragment = canonicalize_fragment_identity(urlsplit(resolved).fragment)
    identity = f"{canonical}#{fragment}" if fragment else canonical
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{identity[:MAX_LINK_URL_CHARS]} [sha256:{digest}]"


_SUBMIT_INPUT_TYPES = frozenset({"submit", "image"})


def _submit_control_annotation(node: Node, ctx: _LinkContext) -> str:
    # A submit control's own ``formaction`` overrides its form's ``action``
    # for that control only, so a same-text/same-label control whose
    # formaction destination changes (e.g. /apply-v1 -> /apply-v2) must not
    # normalize identically to the unchanged control.
    destination = _link_destination(node, "formaction", ctx)
    return f"[formaction: {destination}]" if destination else ""


def _document_base_url(root: Node, fetched_url: str) -> str:
    """Resolve the first declared document base against the fetched URL."""
    if not fetched_url:
        return ""
    for node in iter_nodes(root):
        if node.tag != "base" or "href" not in node.attrs:
            continue
        value = node.attrs.get("href", "").strip()
        try:
            canonical, _ = canonicalize_url(urljoin(fetched_url, value))
        except ValueError as exc:
            raise MonitorError(
                "network_policy_denied", "document base URL is malformed"
            ) from exc
        return canonical
    return fetched_url


def _text_content(node: Node, excluded: set[Node], ctx: _LinkContext) -> str:
    if node in excluded or _is_noise(node):
        return ""
    pieces: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            pieces.append(child)
        elif child.tag == "br":
            pieces.append("\n")
        else:
            text = _text_content(child, excluded, ctx)
            is_live = child not in excluded and not _is_noise(child)
            if child.tag == "a" and is_live:
                destination = _link_destination(child, "href", ctx)
                if destination:
                    text = f"{text} [{destination}]".strip()
            elif child.tag == "button" and is_live:
                annotation = _submit_control_annotation(child, ctx)
                if annotation:
                    text = f"{text} {annotation}".strip()
            elif (
                is_live
                and child.tag == "input"
                and child.attrs.get("type", "").strip().lower() in _SUBMIT_INPUT_TYPES
            ):
                # The visible value/alt label is real user-facing content
                # even when the control has no formaction of its own (it
                # still submits via the form's normal action), so it must
                # not be gated on an annotation being present.
                annotation = _submit_control_annotation(child, ctx)
                label = _clean_line(
                    child.attrs.get("value", "") or child.attrs.get("alt", "")
                )
                text = f"{label} {annotation}".strip()
            pieces.append(text)
    return " ".join(pieces)


def _clean_line(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", value).strip()


def _table_lines(table: Node, excluded: set[Node], ctx: _LinkContext) -> list[str]:
    lines: list[str] = []
    for row in (node for node in iter_nodes(table) if node.tag == "tr"):
        cells = [
            _clean_line(_text_content(child, excluded, ctx))
            for child in row.children
            if isinstance(child, Node) and child.tag in {"td", "th"}
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _extract_lines(root: Node, excluded: set[Node], ctx: _LinkContext) -> list[str]:
    lines: list[str] = []

    def visit(node: Node) -> None:
        if node in excluded or _is_noise(node):
            return
        if node.tag == "table":
            lines.extend(_table_lines(node, excluded, ctx))
            return
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _clean_line(_text_content(node, excluded, ctx))
            if text:
                lines.append(f"{'#' * int(node.tag[1])} {text}")
            return
        if node.tag == "li":
            text = _clean_line(_text_content(node, excluded, ctx))
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
            text = _clean_line(_text_content(node, excluded, ctx))
            if text:
                lines.append(text)
            return
        if node.tag == "a":
            # Parent text extraction annotates child anchors, but a directly
            # visited anchor (for example, an immediate child of <main> or an
            # include_selector root) has no parent extraction step. Annotate
            # its own destination here so a href-only change cannot disappear
            # from the normalized representation.
            text = _clean_line(_text_content(node, excluded, ctx))
            destination = _link_destination(node, "href", ctx)
            if destination:
                text = f"{text} [{destination}]".strip()
            if text:
                lines.append(text)
            return
        if node.tag == "button":
            annotation = _submit_control_annotation(node, ctx)
            if annotation:
                # Mirrors the <a> case above: a directly visited button (no
                # parent text-extraction step annotated it) still needs its
                # own formaction destination represented, or a
                # destination-only change on it would silently disappear.
                # Falling through when there is no formaction preserves the
                # existing per-child-node line splitting below.
                text = _clean_line(_text_content(node, excluded, ctx))
                text = f"{text} {annotation}".strip()
                if text:
                    lines.append(text)
                return
        if (
            node.tag == "input"
            and node.attrs.get("type", "").strip().lower() in _SUBMIT_INPUT_TYPES
        ):
            # Submit/image inputs are void elements with nothing further to
            # visit, so returning unconditionally is safe. The visible
            # value/alt label is real user-facing content even when the
            # control has no formaction of its own (it still submits via
            # the form's normal action), so it is emitted regardless of
            # whether formaction produced its own annotation.
            annotation = _submit_control_annotation(node, ctx)
            label = _clean_line(
                node.attrs.get("value", "") or node.attrs.get("alt", "")
            )
            text = f"{label} {annotation}".strip()
            if text:
                lines.append(text)
            return
        if node.tag == "form":
            # A form's own text content (buttons/labels, handled by the
            # generic recursion below) can stay identical while its submit
            # destination changes; represent that destination as its own
            # line rather than silently dropping the change.
            destination = _link_destination(node, "action", ctx)
            if destination:
                lines.append(f"[form action: {destination}]")
        child_nodes = [child for child in node.children if isinstance(child, Node)]
        has_block_child = any(child.tag in BLOCK_TAGS for child in child_nodes)
        direct_text = _clean_line(
            " ".join(child for child in node.children if isinstance(child, str))
        )
        if direct_text and not has_block_child:
            full_text = _clean_line(_text_content(node, excluded, ctx))
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
    base_url: str = "",
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

    document_base_url = _document_base_url(parser.root, base_url)
    ctx = _LinkContext(document_base_url, MAX_LINK_ANNOTATIONS)
    raw_lines: list[str] = []
    for root in roots:
        raw_lines.extend(_extract_lines(root, excluded, ctx))
    if not raw_lines:
        fallback = " ".join(
            _clean_line(_text_content(root, excluded, ctx)) for root in roots
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
