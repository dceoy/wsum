"""Dependency-free HTML extraction with a deliberately small selector grammar."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from errors import MonitorError
from network_policy import canonicalize_fragment_identity, canonicalize_url

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

VOID_TAGS = frozenset({
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
})
NOISE_TAGS = frozenset({
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
})
# ``<header>`` is page chrome only when it isn't nested in a content
# container: ``<article><header><h1>title</h1></header>…</article>`` is a
# section heading, not boilerplate, so it must survive noise stripping.
CONTENT_SECTIONING_TAGS = frozenset({"article", "section", "main"})
BLOCK_TAGS = frozenset({
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
})
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
    r"^(?:last\s+)?(?:updated|modified|published)\s*[:：]?\s*"  # ruff: ignore[ambiguous-unicode-character-string] -- genuine Japanese full-width colon
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


@dataclass(slots=True)
class _LinkContext:
    """Shared state for resolving/annotating link destinations across a walk."""

    base_url: str
    remaining: int


class Node:
    """One parsed HTML element: its tag, attributes, children, and parent."""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(
        self, tag: str, attrs: dict[str, str], parent: Node | None = None
    ) -> None:
        """Create a node, starting with no children."""
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent


class _TreeParser(HTMLParser):
    """Parse HTML into a bounded :class:`Node` tree, rejecting oversized documents."""

    def __init__(self) -> None:
        """Initialize an empty document root."""
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.current = self.root
        self.node_count = 0
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Append a new element node and descend into it, unless it's void.

        Raises:
            MonitorError: If the document exceeds the node-count or
                nesting-depth bound.
        """
        tag = tag.lower()
        normalized_attrs = {name.lower(): value or "" for name, value in attrs if name}
        self.node_count += 1
        if self.node_count > MAX_NODES:
            msg = "html_too_large"
            raise MonitorError(msg, "HTML document has too many elements")
        node = Node(tag, normalized_attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            if self.depth >= MAX_DEPTH:
                msg = "html_too_large"
                raise MonitorError(msg, "HTML document nesting is too deep")
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
    """One tag/id/classes/attributes component of a (possibly compound) selector."""

    __slots__ = ("attributes", "classes", "element_id", "tag")

    def __init__(
        self,
        tag: str,
        element_id: str,
        classes: tuple[str, ...],
        attributes: tuple[tuple[str, str | None], ...],
    ) -> None:
        """Create a selector component from its already-parsed parts."""
        self.tag = tag
        self.element_id = element_id
        self.classes = classes
        self.attributes = attributes

    def matches(self, node: Node) -> bool:
        """Return whether ``node`` matches this selector component.

        Returns:
            True if ``node``'s tag, id, classes, and attributes all satisfy
            this component's constraints.
        """
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


_MAX_SELECTOR_ATTR_VALUE_LENGTH = 200
_MAX_SELECTOR_LENGTH = 500


def _consume_id_or_class(
    value: str,
    position: int,
    marker: str,
    element_id: str,
    classes: list[str],
) -> tuple[str, int]:
    """Parse one ``#id`` or ``.class`` component starting at ``position``.

    Returns:
        The (possibly updated) element id, and the position just past this
        component.

    Raises:
        MonitorError: If the name is invalid, or a second id is given.
    """
    name_match = NAME_RE.match(value[position + 1 :])
    if not name_match:
        msg = "selector_invalid"
        raise MonitorError(msg, "selector name is invalid")
    name = name_match.group(0)
    if marker == "#":
        if element_id:
            msg = "selector_invalid"
            raise MonitorError(msg, "selector has more than one id")
        element_id = name
    else:
        classes.append(name)
    return element_id, position + len(name) + 1


def _consume_attribute(
    value: str, position: int, attributes: list[tuple[str, str | None]]
) -> int:
    """Parse one ``[attr]`` or ``[attr=value]`` component starting at ``position``.

    Returns:
        The position just past this component.

    Raises:
        MonitorError: If the attribute bracket is unclosed or its content
            is invalid.
    """
    end = value.find("]", position + 1)
    if end < 0:
        msg = "selector_invalid"
        raise MonitorError(msg, "selector attribute is unclosed")
    expression = value[position + 1 : end].strip()
    if "=" in expression:
        name, expected = expression.split("=", 1)
        name = name.strip().lower()
        expected = expected.strip().strip("\"'")
    else:
        name, expected = expression.lower(), None
    if not NAME_RE.fullmatch(name) or (
        expected is not None and len(expected) > _MAX_SELECTOR_ATTR_VALUE_LENGTH
    ):
        msg = "selector_invalid"
        raise MonitorError(msg, "selector attribute is invalid")
    attributes.append((name, expected))
    return end + 1


def _parse_simple_selector(value: str) -> SimpleSelector:
    """Parse one whitespace-delimited component of a selector string.

    Returns:
        The parsed selector component.

    Raises:
        MonitorError: If the component is empty or otherwise invalid (via
            :func:`_consume_id_or_class`/:func:`_consume_attribute`), or
            uses an unsupported marker.
    """
    if not value:
        msg = "selector_invalid"
        raise MonitorError(msg, "selector contains an empty component")
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
            element_id, position = _consume_id_or_class(
                value, position, marker, element_id, classes
            )
            continue
        if marker == "[":
            position = _consume_attribute(value, position, attributes)
            continue
        msg = "selector_invalid"
        raise MonitorError(
            msg,
            "only tag, id, class, attribute, and descendant selectors are supported",
        )
    if not tag and not element_id and not classes and not attributes:
        msg = "selector_invalid"
        raise MonitorError(msg, "selector component is empty")
    return SimpleSelector(tag, element_id, tuple(classes), tuple(attributes))


def parse_selector(value: str) -> tuple[SimpleSelector, ...]:
    """Parse a whitespace-delimited descendant-combinator selector.

    Returns:
        The selector's components, outermost first.

    Raises:
        MonitorError: If ``value`` is not a string, is empty, too long, or
            uses an unsupported combinator/pseudo-selector.
    """
    if (
        not isinstance(value, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        # value ultimately originates from CLI/config input whose own
        # contract is not statically enforced across that boundary; this
        # stays a load-bearing runtime check.
        or not value.strip()
        or len(value) > _MAX_SELECTOR_LENGTH
    ):
        msg = "selector_invalid"
        raise MonitorError(msg, "selector is empty or too long")
    if any(char in value for char in ",>+~:{}"):
        msg = "selector_invalid"
        raise MonitorError(
            msg,
            "selector uses unsupported combinators or pseudo-selectors",
        )
    return tuple(_parse_simple_selector(part) for part in value.split())


def iter_nodes(root: Node) -> Iterator[Node]:
    """Yield every descendant element of ``root``, depth-first.

    Yields:
        Each descendant :class:`Node`, in document order.
    """
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
    """Return every descendant of ``root`` matching the selector ``value``.

    Returns:
        The matching nodes, in document order.
    """
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


_NOISE_ROLES = frozenset({
    "banner",
    "complementary",
    "contentinfo",
    "dialog",
    "navigation",
})


def _is_noise_by_tokens(node: Node, tokens: str) -> bool:
    """Return whether ``node``'s id/class tokens mark it as boilerplate.

    Returns:
        True if the tokens match a generic, share, or promo noise pattern,
        or a header-noise pattern outside a content section.
    """
    if (
        NOISE_TOKEN_RE.search(tokens)
        or SHARE_NOISE_TOKEN_RE.search(tokens)
        or PROMO_NOISE_TOKEN_RE.search(tokens)
    ):
        return True
    return bool(HEADER_NOISE_TOKEN_RE.search(tokens)) and not _has_content_ancestor(
        node
    )


def _is_noise(node: Node) -> bool:
    """Return whether ``node`` is page chrome/boilerplate rather than content.

    Returns:
        True if the node's tag, attributes, or id/class tokens (via
        :func:`_is_noise_by_tokens`) mark it as noise.
    """
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
    if node.attrs.get("role", "").lower() in _NOISE_ROLES:
        return True
    tokens = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}"
    return _is_noise_by_tokens(node, tokens)


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
        msg = "html_too_large"
        raise MonitorError(msg, "HTML document has too many link destinations")
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
    """Resolve the first declared document base against the fetched URL.

    Returns:
        The resolved, canonical base URL, or ``fetched_url`` if no
        ``<base>`` element is declared (or ``fetched_url`` itself is
        empty).

    Raises:
        MonitorError: If a declared base URL is malformed.
    """
    if not fetched_url:
        return ""
    for node in iter_nodes(root):
        if node.tag != "base" or "href" not in node.attrs:
            continue
        value = node.attrs.get("href", "").strip()
        try:
            canonical, _ = canonicalize_url(urljoin(fetched_url, value))
        except ValueError as exc:
            msg = "network_policy_denied"
            raise MonitorError(msg, "document base URL is malformed") from exc
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


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_TEXT_BLOCK_TAGS = frozenset({
    "address",
    "blockquote",
    "dd",
    "dt",
    "figcaption",
    "p",
    "pre",
    "summary",
})


def _leaf_line_for_prefixed_tag(
    node: Node, excluded: set[Node], ctx: _LinkContext
) -> list[str] | None:
    """Return the line for a heading/list-item/submit-input tag, or None.

    Returns:
        A (possibly empty) single-line list, or ``None`` if ``node``'s tag
        is none of these.
    """
    if node.tag in _HEADING_TAGS:
        text = _clean_line(_text_content(node, excluded, ctx))
        return [f"{'#' * int(node.tag[1])} {text}"] if text else []
    if node.tag == "li":
        text = _clean_line(_text_content(node, excluded, ctx))
        return [f"- {text}"] if text else []
    if (
        node.tag == "input"
        and node.attrs.get("type", "").strip().lower() in _SUBMIT_INPUT_TYPES
    ):
        # Submit/image inputs are void elements with nothing further to
        # visit. The visible value/alt label is real user-facing content
        # even when the control has no formaction of its own (it still
        # submits via the form's normal action), so it is emitted
        # regardless of whether formaction produced its own annotation.
        annotation = _submit_control_annotation(node, ctx)
        label = _clean_line(node.attrs.get("value", "") or node.attrs.get("alt", ""))
        text = f"{label} {annotation}".strip()
        return [text] if text else []
    return None


def _leaf_line_for_tag(
    node: Node, excluded: set[Node], ctx: _LinkContext
) -> list[str] | None:
    """Return the line(s) for a tag that fully replaces recursion into it.

    Returns:
        A (possibly empty) list of lines to emit in place of recursing into
        ``node``'s children, or ``None`` if ``node``'s tag is not one of
        these specially handled leaf-like tags (in which case the caller
        should fall through to further handling).
    """
    if node.tag == "table":
        return _table_lines(node, excluded, ctx)
    if node.tag in _TEXT_BLOCK_TAGS:
        text = _clean_line(_text_content(node, excluded, ctx))
        return [text] if text else []
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
        return [text] if text else []
    return _leaf_line_for_prefixed_tag(node, excluded, ctx)


def _button_line(
    node: Node, excluded: set[Node], ctx: _LinkContext
) -> list[str] | None:
    """Return the line for a directly-visited button with a formaction, or None.

    Mirrors the ``<a>`` case in :func:`_leaf_line_for_tag`: a directly
    visited button (no parent text-extraction step annotated it) still
    needs its own formaction destination represented, or a
    destination-only change on it would silently disappear.

    Returns:
        A single-line list if the button has a formaction annotation, or
        ``None`` to fall through to generic recursion when it has none
        (preserving the existing per-child-node line splitting).
    """
    annotation = _submit_control_annotation(node, ctx)
    if not annotation:
        return None
    text = _clean_line(_text_content(node, excluded, ctx))
    text = f"{text} {annotation}".strip()
    return [text] if text else []


def _form_action_line(node: Node, ctx: _LinkContext) -> str:
    """Return a form's action-destination line, if it declares one.

    A form's own text content (buttons/labels, handled by the generic
    recursion in :func:`_extract_lines`) can stay identical while its
    submit destination changes; representing that destination as its own
    line keeps such a change from silently disappearing.

    Returns:
        The ``[form action: ...]`` line, or ``""`` if ``node`` is not a
        ``<form>`` or declares no resolvable action.
    """
    if node.tag != "form":
        return ""
    destination = _link_destination(node, "action", ctx)
    return f"[form action: {destination}]" if destination else ""


def _direct_text_and_block_child(
    node: Node, child_nodes: list[Node]
) -> tuple[str, bool]:
    """Compute a node's own direct text and whether any child is block-level.

    Returns:
        The node's directly-contained text (excluding descendants), and
        whether any immediate child is a block-level tag.
    """
    has_block_child = any(child.tag in BLOCK_TAGS for child in child_nodes)
    direct_text = _clean_line(
        " ".join(child for child in node.children if isinstance(child, str))
    )
    return direct_text, has_block_child


def _generic_node_lines(
    node: Node, excluded: set[Node], ctx: _LinkContext, child_nodes: list[Node]
) -> tuple[list[str], bool]:
    """Compute the line(s) for a node handled by the generic (non-leaf) case.

    Returns:
        The line(s) to append, and whether the caller should still recurse
        into ``child_nodes`` afterward.
    """
    direct_text, has_block_child = _direct_text_and_block_child(node, child_nodes)
    if direct_text and not has_block_child:
        full_text = _clean_line(_text_content(node, excluded, ctx))
        if full_text:
            return [full_text], False
    if direct_text and has_block_child:
        # The element mixes its own text with block-level children
        # (e.g. ``<main>Status text<div>Details</div></main>``).
        # Emitting it here keeps a change to that text from being
        # silently dropped from the normalized hash, even though the
        # block children below are still emitted as separate lines.
        return [direct_text], True
    return [], True


def _extract_lines(root: Node, excluded: set[Node], ctx: _LinkContext) -> list[str]:
    """Render a node tree into normalized text lines.

    Returns:
        One line per block-level element/table-row/heading/etc, in
        document order.
    """
    lines: list[str] = []

    def visit(node: Node) -> None:
        if node in excluded or _is_noise(node):
            return
        leaf_lines = _leaf_line_for_tag(node, excluded, ctx)
        if leaf_lines is not None:
            lines.extend(leaf_lines)
            return
        if node.tag == "button":
            button_lines = _button_line(node, excluded, ctx)
            if button_lines is not None:
                lines.extend(button_lines)
                return
        form_line = _form_action_line(node, ctx)
        if form_line:
            lines.append(form_line)
        child_nodes = [child for child in node.children if isinstance(child, Node)]
        extra_lines, should_recurse = _generic_node_lines(
            node, excluded, ctx, child_nodes
        )
        lines.extend(extra_lines)
        if not should_recurse:
            return
        for child in child_nodes:
            visit(child)

    visit(root)
    return lines


_MAX_COOKIE_NOTICE_LENGTH = 300


def _parse_html_tree(html: str) -> Node:
    """Parse ``html`` into a bounded :class:`Node` tree.

    Returns:
        The parsed document's root node.

    Raises:
        MonitorError: If ``html`` is not text, or the parser rejects the
            document (via :class:`_TreeParser`, or a malformed low-level
            parse).
    """
    if (
        not isinstance(html, str)  # pyright: ignore[reportUnnecessaryIsInstance]
        # html ultimately originates from an untrusted HTTP fetch/decode
        # pipeline whose own contract is not statically enforced across
        # that boundary; this stays a load-bearing runtime check.
    ):
        msg = "html_invalid"
        raise MonitorError(msg, "HTML input must be text")
    parser = _TreeParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError) as exc:
        msg = "html_malformed"
        raise MonitorError(msg, "HTML parser rejected the document") from exc
    return parser.root


def _select_roots(document_root: Node, include_selector: str) -> list[Node]:
    """Select the extraction roots: the whole document, or an include match.

    Returns:
        The document root alone, or every element matching
        ``include_selector``.

    Raises:
        MonitorError: If ``include_selector`` is set but matches nothing.
    """
    if not include_selector:
        return [document_root]
    roots = select(document_root, include_selector)
    if not roots:
        msg = "selector_no_match"
        raise MonitorError(msg, "include_selector matched no elements")
    return roots


def _select_excluded(
    document_root: Node, exclude_selectors: Iterable[str], *, strict_selectors: bool
) -> set[Node]:
    """Select every element matched by any of ``exclude_selectors``.

    Returns:
        The union of all matched elements.

    Raises:
        MonitorError: If ``strict_selectors`` is set and a selector matches
            nothing.
    """
    excluded: set[Node] = set()
    for selector in exclude_selectors:
        matches = select(document_root, selector)
        if strict_selectors and not matches:
            msg = "selector_no_match"
            raise MonitorError(msg, "an exclude_selector matched no elements")
        excluded.update(matches)
    return excluded


def _collect_raw_lines(
    roots: list[Node], excluded: set[Node], ctx: _LinkContext
) -> list[str]:
    """Extract lines from every root, falling back to plain text if none.

    Returns:
        The extracted lines (via :func:`_extract_lines`), or a single
        fallback line of concatenated text content if extraction produced
        no lines at all.
    """
    raw_lines: list[str] = []
    for root in roots:
        raw_lines.extend(_extract_lines(root, excluded, ctx))
    if not raw_lines:
        fallback = " ".join(
            _clean_line(_text_content(root, excluded, ctx)) for root in roots
        ).strip()
        if fallback:
            raw_lines.append(fallback)
    return raw_lines


def _filter_lines(raw_lines: list[str]) -> list[str]:
    """Clean, drop noise (timestamps/cookie notices), and dedupe boilerplate lines.

    Returns:
        The filtered lines, in order.
    """
    lines: list[str] = []
    for value in raw_lines:
        line = _clean_line(value)
        if (
            not line
            or TIMESTAMP_ONLY_RE.fullmatch(line)
            or (len(line) <= _MAX_COOKIE_NOTICE_LENGTH and COOKIE_TEXT_RE.search(line))
        ):
            continue
        if BOILERPLATE_TEXT_RE.search(line) and line in lines:
            continue
        lines.append(line)
    return lines


def normalize_html(
    html: str,
    *,
    base_url: str = "",
    include_selector: str = "",
    exclude_selectors: Iterable[str] = (),
    strict_selectors: bool = True,
) -> str:
    """Extract and normalize an HTML document's meaningful text content.

    Returns:
        The normalized, newline-joined text.

    Raises:
        MonitorError: If the document is invalid or malformed (via
            :func:`_parse_html_tree`), a selector matches nothing (via
            :func:`_select_roots`/:func:`_select_excluded`), or extraction
            produces no meaningful content.
    """
    document_root = _parse_html_tree(html)
    roots = _select_roots(document_root, include_selector)
    excluded = _select_excluded(
        document_root, exclude_selectors, strict_selectors=strict_selectors
    )
    document_base_url = _document_base_url(document_root, base_url)
    ctx = _LinkContext(document_base_url, MAX_LINK_ANNOTATIONS)
    raw_lines = _collect_raw_lines(roots, excluded, ctx)
    lines = _filter_lines(raw_lines)
    normalized = "\n".join(lines).strip()
    if not normalized:
        msg = "empty_extraction"
        raise MonitorError(msg, "HTML extraction produced no meaningful content")
    return normalized
