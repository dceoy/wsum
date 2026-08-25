"""Tests for the normalize_diff module."""

from __future__ import annotations

import base64
import builtins
import codecs
import json
import tempfile
import time
import unittest
import zlib
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from diff import DiffConfig, compare_content
from diff import _main as diff_main
from errors import MonitorError
from feed_normalizer import normalize_feed
from normalize import _main as normalize_main
from normalize import normalize_content
from pdf_normalizer import _stream_filters, extract_pdf_text
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    StreamObject,
)


def _pdf_stream(number: int, body: bytes, *, extra: bytes = b"") -> bytes:
    # /Length must equal the exact stream body byte count (pdf_normalizer
    # now derives the stream boundary from it instead of scanning for the
    # next literal "endstream" bytes), so it is always computed here rather
    # than hand-counted per fixture.
    dict_entries = b"/Length " + str(len(body)).encode()
    if extra:
        dict_entries += b" " + extra
    return (
        f"{number} 0 obj\n".encode()
        + b"<< "
        + dict_entries
        + b" >>\nstream\n"
        + body
        + b"\nendstream\nendobj\n"
    )


def _pdf(*objects: bytes) -> bytes:
    return b"%PDF-1.4\n" + b"".join(objects) + b"%%EOF"


def _escaped_filter_text_pdf(text: bytes) -> bytes:
    """Build a valid one-page PDF whose Flate filter key uses a name escape."""
    compressed = zlib.compress(b"BT /F1 12 Tf (" + text + b") Tj ET")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            b"<< /Length "
            + str(len(compressed)).encode()
            + b" /Fil#74er /FlateDecode >>\nstream\n"
            + compressed
            + b"\nendstream"
        ),
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        ),
    )
    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = sum(len(part) for part in parts)
    parts.append(b"xref\n0 6\n0000000000 65535 f \n")
    parts.extend(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    parts.append(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return b"".join(parts)


class _IndirectLengthStreamObject(StreamObject):
    """A writer stream whose /Length remains an indirect reference."""

    def write_to_stream(self, stream: BytesIO, encryption_key: object = None) -> None:
        del encryption_key
        DictionaryObject.write_to_stream(self, stream)
        stream.write(b"\nstream\n")
        stream.write(self._data)
        stream.write(b"\nendstream")


def _text_pdf(content: bytes, *, indirect_length: bool = False) -> bytes:
    """Build a valid, one-page PDF containing the supplied text operators."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream_data = b"BT /F1 12 Tf " + content + b" ET"
    if indirect_length:
        stream = _IndirectLengthStreamObject()
        stream._data = stream_data
        stream[NameObject("/Length")] = writer._add_object(
            NumberObject(len(stream_data))
        )
    else:
        stream = DecodedStreamObject()
        stream.set_data(stream_data)
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_pdf_with_link(content: bytes, *, uri: str) -> bytes:
    """Build a valid, one-page text PDF with a link annotation on the page."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream_data = b"BT /F1 12 Tf " + content + b" ET"
    stream = DecodedStreamObject()
    stream.set_data(stream_data)
    page[NameObject("/Contents")] = writer._add_object(stream)
    link = Link(rect=RectangleObject((0, 0, 50, 50)), url=uri)
    writer.add_annotation(page_number=0, annotation=link)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _image_only_pdf() -> bytes:
    """Build a valid page that contains only an image XObject."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    image = StreamObject()
    image._data = b"bounded encoded image fixture"
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/DCTDecode"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/XObject"): DictionaryObject(
                {NameObject("/Im1"): writer._add_object(image)}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(b"q 1 0 0 1 0 0 cm /Im1 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class NormalizationTests(unittest.TestCase):
    """Tests for NormalizationTests."""

    def test_formatting_noise_produces_same_hash(self) -> None:
        """Test that formatting noise produces same hash."""
        first = b"""
        <html><body><nav>Home Shop</nav><main>
          <h1>Product</h1><p>Fast and safe.</p>
          <p>Updated: 2026-07-30 10:00</p>
          <script>alert(1)</script>
        </main></body></html>
        """
        second = b"""
        <!doctype html><html><body><header>Menu</header><main>
        <h1> Product </h1>
        <p>Fast   and safe.</p><p>Updated: 2026-07-31 11:30</p>
        </main><footer>Copyright</footer></body></html>
        """
        normalized_first = normalize_content(first, content_type="text/html")
        normalized_second = normalize_content(second, content_type="text/html")
        assert normalized_first.text == normalized_second.text
        assert normalized_first.normalized_hash == normalized_second.normalized_hash
        assert normalized_first.normalization_version == "2026-01"

    def test_form_content_inside_main_is_preserved_but_outside_is_dropped(
        self,
    ) -> None:
        """Test that form content inside main is preserved but outside is dropped."""
        before = b"""
        <html><body>
        <form id="site-search"><input type="search"></form>
        <main><form><p>Application deadline: 2026-08-31</p></form></main>
        </body></html>
        """
        after = b"""
        <html><body>
        <form id="site-search"><input type="search"></form>
        <main><form><p>Application deadline: 2026-09-30</p></form></main>
        </body></html>
        """
        normalized_before = normalize_content(before, content_type="text/html")
        normalized_after = normalize_content(after, content_type="text/html")
        assert "2026-08-31" in normalized_before.text
        assert "2026-09-30" in normalized_after.text
        assert normalized_before.normalized_hash != normalized_after.normalized_hash

    def test_form_wrapping_main_preserves_the_main_content(self) -> None:
        # The noise check used to look only at ancestors, so a page-level
        # <form> wrapping <main> (not the other way around) marked the form
        # -- and everything inside it, including <main> -- as noise and
        # dropped the whole subtree. A change inside such a wrapped <main>
        # must still change the normalized hash.
        """Test that form wrapping main preserves the main content."""
        before = b"""
        <html><body>
        <form><main><p>Applications are open until 2026-08-31</p></main></form>
        </body></html>
        """
        after = b"""
        <html><body>
        <form><main><p>Applications are open until 2026-09-30</p></main></form>
        </body></html>
        """
        normalized_before = normalize_content(before, content_type="text/html")
        normalized_after = normalize_content(after, content_type="text/html")
        assert "2026-08-31" in normalized_before.text
        assert "2026-09-30" in normalized_after.text
        assert normalized_before.normalized_hash != normalized_after.normalized_hash
        bom_feed = (
            b"\xef\xbb\xbf<!--synthetic--><rss><channel>"
            b"<item><guid>1</guid><title>One</title></item>"
            b"</channel></rss>"
        )
        assert normalize_content(bom_feed, content_type="application/rss+xml").kind == "feed"
        plain = normalize_content("ＡＢＣ".encode(), content_type="text/plain")
        assert plain.text == "ABC"

    def test_article_header_title_change_is_not_silently_missed(self) -> None:
        # A <header> nested inside <article>/<section> is a content
        # sub-heading, not page chrome, so a change confined to it must
        # still change the normalized hash.
        """Test that article header title change is not silently missed."""
        before = normalize_content(
            b"<html><body><article><header><h1>Old Title</h1></header>"
            b"<p>Body text.</p></article></body></html>",
            content_type="text/html",
        )
        after = normalize_content(
            b"<html><body><article><header><h1>New Title</h1></header>"
            b"<p>Body text.</p></article></body></html>",
            content_type="text/html",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "New Title" in after.text
        # A page-level header outside any content container remains
        # boilerplate and is still stripped.
        page_header = normalize_content(
            b"<html><body><header>Site Nav</header>"
            b"<main><p>Body text.</p></main></body></html>",
            content_type="text/html",
        )
        assert "Site Nav" not in page_header.text

    def test_classed_article_header_title_change_is_not_silently_missed(
        self,
    ) -> None:
        # A "header" class/id token is generic boilerplate noise (e.g.
        # "site-header"), but a <header class="article-header"> nested in
        # an <article> is the same content sub-heading as a bare <header>
        # nested there -- the class token must not re-drop it.
        """Test that classed article header title change is not silently missed."""
        before = normalize_content(
            b'<html><body><article><header class="article-header">'
            b"<h1>Old status</h1></header><p>Body text.</p></article>"
            b"</body></html>",
            content_type="text/html",
        )
        after = normalize_content(
            b'<html><body><article><header class="article-header">'
            b"<h1>New status</h1></header><p>Body text.</p></article>"
            b"</body></html>",
            content_type="text/html",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "New status" in after.text
        # A page-level "site-header" class remains boilerplate and is
        # still stripped.
        page_header = normalize_content(
            b'<html><body><header class="site-header">Site Nav</header>'
            b"<main><p>Body text.</p></main></body></html>",
            content_type="text/html",
        )
        assert "Site Nav" not in page_header.text

    def test_share_price_business_content_is_not_dropped_as_noise(self) -> None:
        # NOISE_TOKEN_RE used to treat any class/id token containing "share"
        # as boilerplate, so a business widget like class="share-price"
        # (and BEM-style compounds such as "product-share-price") was
        # dropped wholesale -- a value-only change then left the normalized
        # text and hash unchanged.
        """Test that share price business content is not dropped as noise."""
        before = normalize_content(
            b'<html><body><main><div class="share-price">100</div>'
            b'<div class="product-share-price">100</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        after = normalize_content(
            b'<html><body><main><div class="share-price">105</div>'
            b'<div class="product-share-price">105</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "105" in after.text
        # A genuine social-share widget remains boilerplate and is still
        # stripped.
        share_widget = normalize_content(
            b"<html><body><main><p>Body text.</p>"
            b'<div class="social-share">Share this</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        assert "Share this" not in share_widget.text

    def test_promo_business_content_is_not_dropped_as_noise(self) -> None:
        # NOISE_TOKEN_RE used to treat any class/id token containing "promo"
        # as boilerplate, so business content such as id="promo-code" or
        # class="product-promo-price" was dropped wholesale -- a change
        # confined to that promotion then left the normalized text and hash
        # unchanged.
        """Test that promo business content is not dropped as noise."""
        before = normalize_content(
            b'<html><body><main><div id="promo-code">SAVE20</div>'
            b'<div class="product-promo-price">100</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        after = normalize_content(
            b'<html><body><main><div id="promo-code">SAVE30</div>'
            b'<div class="product-promo-price">105</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "SAVE30" in after.text
        assert "105" in after.text
        # A genuine promo widget remains boilerplate and is still stripped.
        # "promo-widget" (unlike "promo-banner") is not independently caught
        # by NOISE_TOKEN_RE's generic tokens, so this exercises
        # PROMO_NOISE_TOKEN_RE specifically rather than a pre-existing rule.
        promo_widget = normalize_content(
            b"<html><body><main><p>Body text.</p>"
            b'<div class="promo-widget">Save big today</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        assert "Save big today" not in promo_widget.text

    def test_http_charset_is_used_before_bom_or_body_sniffing(self) -> None:
        """Test that http charset is used before bom or body sniffing."""
        body = "価格改定のお知らせ".encode("shift_jis")
        # Without the declared charset, the shift_jis bytes are not valid
        # UTF-8 and must fail closed rather than silently decode as garbage
        # replacement text (which could mask a real change).
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(body, content_type="text/plain")
        with_charset = normalize_content(
            body, content_type="text/plain", charset="shift_jis"
        )
        assert with_charset.text == "価格改定のお知らせ"

    def test_unusable_declared_charset_falls_back_to_bom_instead_of_failing(
        self,
    ) -> None:
        # A server-declared charset we do not allow-list (or mislabel) must
        # not turn a previously-decodable BOM'd body into a hard failure.
        """Test that unusable declared charset falls back to bom instead of failing."""
        body = codecs.BOM_UTF8 + b"Notice"
        result = normalize_content(body, content_type="text/plain", charset="utf-16")
        assert result.text == "Notice"

    def test_unsupported_declared_charset_fails_closed_without_rescue(
        self,
    ) -> None:
        # A declared charset that resolves to a real (but non-allow-listed)
        # codec, such as iso-2022-jp, must not silently decode as UTF-8
        # replacement garbage when there is no BOM or in-body declaration
        # to rescue it -- that would make distinct legacy-encoded responses
        # normalize incorrectly or identically, masking real changes.
        """Test that unsupported declared charset fails closed without rescue."""
        body = "価格改定のお知らせ".encode("iso-2022-jp")
        with pytest.raises(MonitorError, match="unsupported"):
            normalize_content(body, content_type="text/plain", charset="iso-2022-jp")

    def test_unsupported_declared_charset_still_rescued_by_in_body_declaration(
        self,
    ) -> None:
        # Even when the declared charset can't be used, an in-body charset
        # declaration must still get a chance to rescue the decode, same as
        # the BOM path above.
        """Test that unsupported declared charset still rescued by in body declaration."""
        body = "charset=shift_jis 価格改定のお知らせ".encode("shift_jis")
        result = normalize_content(
            body, content_type="text/plain", charset="iso-2022-jp"
        )
        assert "価格改定のお知らせ" in result.text

    def test_malformed_bytes_fail_closed_instead_of_collapsing_to_replacement(
        self,
    ) -> None:
        # Two distinct invalid UTF-8 byte sequences both decode under
        # errors="replace" to the same U+FFFD-filled text ("Price: �10"),
        # which would make genuinely different responses hash identically
        # and silently mask a real change. Decoding must fail closed on
        # both instead of quietly treating them as equivalent.
        """Test that malformed bytes fail closed instead of collapsing to replacement."""
        first = b"Price: \xff10"
        second = b"Price: \xfe10"
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(first, content_type="text/plain")
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(second, content_type="text/plain")

    def test_meaningful_price_specification_and_terms_change_hash(self) -> None:
        """Test that meaningful price specification and terms change hash."""
        values = [
            b"<main><p>Price: $10</p></main>",
            b"<main><p>Price: $20</p></main>",
            b"<main><p>Capacity: 20 GB</p></main>",
            b"<main><p>Contract terms changed.</p></main>",
        ]
        hashes = {
            normalize_content(value, content_type="text/html").normalized_hash
            for value in values
        }
        assert len(hashes) == 4

    def test_link_destination_change_is_not_silently_missed(self) -> None:
        # Same visible link text, changed href: without a destination
        # annotation this produces identical normalized text/hash and the
        # routine never diffs or notifies on an application/pricing/
        # checkout link change.
        """Test that link destination change is not silently missed."""
        before = normalize_content(
            b'<main><p><a href="/apply-v1">Apply</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><p><a href="/apply-v2">Apply</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "https://example.com/apply-v1" in before.text
        assert "https://example.com/apply-v2" in after.text

    def test_standalone_link_destination_change_is_not_silently_missed(self) -> None:
        """Test that standalone link destination change is not silently missed."""
        before = normalize_content(
            b'<main><a href="/apply-v1">Apply</a></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><a href="/apply-v2">Apply</a></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "https://example.com/apply-v1" in before.text
        assert "https://example.com/apply-v2" in after.text

    def test_document_base_change_is_not_silently_missed(self) -> None:
        """Test that document base change is not silently missed."""
        before = normalize_content(
            b'<base href="/v1/"><main><a href="apply">Apply</a></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<base href="/v2/"><main><a href="apply">Apply</a></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "https://example.com/v1/apply" in before.text
        assert "https://example.com/v2/apply" in after.text

    def test_unsafe_document_base_fails_closed(self) -> None:
        """Test that unsafe document base fails closed."""
        with pytest.raises(MonitorError, match="HTTP and HTTPS"):
            normalize_content(
                b'<base href="javascript:alert(1)"><main>Content</main>',
                content_type="text/html",
                base_url="https://example.com/page",
            )

    def test_first_declared_document_base_wins_even_when_empty(self) -> None:
        """Test that first declared document base wins even when empty."""
        result = normalize_content(
            (
                b'<base href=""><base href="/ignored/">'
                b'<main><a href="apply">Apply</a></main>'
            ),
            content_type="text/html",
            base_url="https://example.com/current/page",
        )
        assert "https://example.com/current/apply" in result.text
        assert "https://example.com/ignored/apply" not in result.text

    def test_long_link_destination_keeps_full_identity_in_digest(self) -> None:
        """Test that long link destination keeps full identity in digest."""
        common_prefix = "/" + "a" * 350
        before = normalize_content(
            f'<main><p><a href="{common_prefix}-old">Apply</a></p></main>'.encode(),
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            f'<main><p><a href="{common_prefix}-new">Apply</a></p></main>'.encode(),
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash
        assert "sha256:" in before.text
        assert "sha256:" in after.text

    def test_link_destination_budget_fails_closed(self) -> None:
        """Test that link destination budget fails closed."""
        links = "".join(
            f'<p><a href="/item/{index}">Item</a></p>' for index in range(501)
        )
        with pytest.raises(MonitorError, match="too many link destinations"):
            normalize_content(
                f"<main>{links}</main>".encode(),
                content_type="text/html",
                base_url="https://example.com/page",
            )

    def test_form_action_change_is_not_silently_missed(self) -> None:
        """Test that form action change is not silently missed."""
        before = normalize_content(
            b'<main><form action="/checkout-v1"><button>Buy</button></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><form action="/checkout-v2"><button>Buy</button></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_button_formaction_change_is_not_silently_missed(self) -> None:
        # A submit button's formaction overrides the form's action for that
        # control only, so the visible text and form action can stay
        # identical while the actual submit destination changes.
        """Test that button formaction change is not silently missed."""
        before = normalize_content(
            b'<main><form action="/checkout"><button '
            b'formaction="/apply-v1">Apply</button></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><form action="/checkout"><button '
            b'formaction="/apply-v2">Apply</button></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_standalone_button_formaction_change_is_not_silently_missed(
        self,
    ) -> None:
        """Test that standalone button formaction change is not silently missed."""
        before = normalize_content(
            b'<main><button formaction="/apply-v1">Apply</button></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><button formaction="/apply-v2">Apply</button></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_submit_input_formaction_change_is_not_silently_missed(self) -> None:
        """Test that submit input formaction change is not silently missed."""
        before = normalize_content(
            b'<main><form action="/checkout"><input type="submit" '
            b'value="Buy" formaction="/apply-v1"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><form action="/checkout"><input type="submit" '
            b'value="Buy" formaction="/apply-v2"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_image_input_formaction_change_is_not_silently_missed(self) -> None:
        """Test that image input formaction change is not silently missed."""
        before = normalize_content(
            b'<main><form action="/checkout"><input type="image" '
            b'alt="Buy" formaction="/apply-v1"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><form action="/checkout"><input type="image" '
            b'alt="Buy" formaction="/apply-v2"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_non_submit_input_formaction_is_ignored(self) -> None:
        # Only submit/image inputs are real submit controls; a formaction
        # on a plain text input has no browser effect and must not be
        # annotated (which would otherwise mask a real content change under
        # noise, or worse, misrepresent the digest as destination-relevant).
        """Test that non submit input formaction is ignored."""
        before = normalize_content(
            b'<main>Status text<input type="text" formaction="/apply-v1"></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main>Status text<input type="text" formaction="/apply-v2"></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash == after.normalized_hash

    def test_hidden_and_password_input_values_are_never_emitted(self) -> None:
        # The submit/image label fix must stay scoped to _SUBMIT_INPUT_TYPES:
        # a hidden or password input's value is not user-facing content, and
        # must never be treated as a label regardless of where in the tree
        # it appears.
        """Test that hidden and password input values are never emitted."""
        result = normalize_content(
            b'<main><p>Status: <input type="hidden" value="secret-token">'
            b'<input type="password" value="hunter2"></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert "secret-token" not in result.text
        assert "hunter2" not in result.text

    def test_submit_input_label_change_without_formaction_is_not_silently_missed(
        self,
    ) -> None:
        # A submit control's visible value/alt label is real user-facing
        # content even when it has no formaction of its own (it still
        # submits via the form's normal action), so losing it here would
        # let a status change like "Applications open" -> "Applications
        # closed" disappear from the normalized hash.
        """Test that submit input label change without formaction is not silently missed."""
        before = normalize_content(
            b'<main><form action="/checkout">'
            b'<input type="submit" value="Applications open"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><form action="/checkout">'
            b'<input type="submit" value="Applications closed"></form></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_image_input_label_change_nested_in_text_without_formaction_is_not_silently_missed(
        self,
    ) -> None:
        # The same label-loss bug also affects a submit/image input reached
        # via the parent-text-extraction path (a directly visited input is
        # covered above), so it needs its own regression case.
        """Test that image input label change nested in text without formaction is not silently missed."""
        before = normalize_content(
            b'<main><p>Status: <input type="image" alt="Open"></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><p>Status: <input type="image" alt="Closed"></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_button_without_formaction_keeps_existing_line_splitting(self) -> None:
        # A directly visited button with no formaction must fall through to
        # the same generic per-child-node handling used before this change,
        # not collapse its block-level children into a single joined line.
        """Test that button without formaction keeps existing line splitting."""
        wrapped = normalize_content(
            b"<main><button><div>a</div><div>b</div></button></main>",
            content_type="text/html",
            base_url="https://example.com/page",
        )
        without_button = normalize_content(
            b"<main><div>a</div><div>b</div></main>",
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert wrapped.text == without_button.text

    def test_credential_bearing_link_destination_fails_closed(self) -> None:
        # Rejected HTTP(S) destinations must not be omitted or represented by
        # an offline-testable unsalted digest: both choices can expose or miss
        # credential-only changes. The policy error itself is content-safe.
        """Test that credential bearing link destination fails closed."""
        cases = (
            ("https://user:pass@example.com/x", "user:pass"),
            ("https://example.com/x?token=secret", "secret"),
        )
        for destination, secret in cases:
            with self.subTest(destination=destination):
                source = f'<main><a href="{destination}">Link</a></main>'.encode()
                with pytest.raises(MonitorError) as captured:
                    normalize_content(
                        source,
                        content_type="text/html",
                        base_url="https://example.com/page",
                    )
                assert secret not in str(captured.value)

    def test_credential_bearing_link_nested_in_query_value_fails_closed(
        self,
    ) -> None:
        # A benign-looking outer query parameter (e.g. "redirect") can carry
        # a nested, URL-encoded HTTP(S) URL whose own query or userinfo
        # carries the credential; canonicalize_url must catch this for
        # page-controlled link destinations, not just configured target
        # URLs, since the canonical destination is written into normalized
        # snapshots/diffs and can reach summary-model context.
        """Test that credential bearing link nested in query value fails closed."""
        destination = (
            "https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
            "cb%3Faccess_token%3Dsecret123"
        )
        source = f'<main><a href="{destination}">Link</a></main>'.encode()
        with pytest.raises(MonitorError) as captured:
            normalize_content(
                source,
                content_type="text/html",
                base_url="https://example.com/page",
            )
        assert "secret123" not in str(captured.value)

    def test_credential_bearing_link_scheme_relative_or_double_encoded_fails_closed(
        self,
    ) -> None:
        # canonicalize_url only recognized a nested URL with an explicit
        # http(s) scheme and a single layer of percent-encoding, so a
        # scheme-relative ("//host/...") or double-encoded nested webhook
        # URL reached the normalized link destination unexamined even though
        # it is fully recoverable and would cross the documented
        # snapshot/model/Slack secret boundary.
        """Test that credential bearing link scheme relative or double encoded fails closed."""
        destinations = (
            ("https://example.com/?redirect=%2F%2Fhooks.slack.com%2Fservices"
            "%2FT00000000%2FB00000000%2FXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("https://example.com/?redirect=https%253A%252F%252Fhooks.slack"
            ".com%252Fservices%252FT00000000%252FB00000000"
            "%252FXXXXXXXXXXXXXXXXXXXXXXXX"),
        )
        for destination in destinations:
            with self.subTest(destination=destination):
                source = f'<main><a href="{destination}">Link</a></main>'.encode()
                with pytest.raises(MonitorError) as captured:
                    normalize_content(
                        source,
                        content_type="text/html",
                        base_url="https://example.com/page",
                    )
                assert "XXXXXXXXXXXXXXXXXXXXXXXX" not in str(captured.value)

    def test_credential_bearing_link_nested_relative_reference_fails_closed(
        self,
    ) -> None:
        # canonicalize_url only recognized a nested URL that carried its own
        # scheme or host, so a scheme-less relative reference like
        # "/callback?access_token=secret" -- the common shape of an OAuth
        # redirect target -- reached the normalized link destination
        # unexamined even though its query carries the credential.
        """Test that credential bearing link nested relative reference fails closed."""
        destination = (
            "https://example.com/?redirect=%2Fcallback%3Faccess_token%3Dsecret123"
        )
        source = f'<main><a href="{destination}">Link</a></main>'.encode()
        with pytest.raises(MonitorError) as captured:
            normalize_content(
                source,
                content_type="text/html",
                base_url="https://example.com/page",
            )
        assert "secret123" not in str(captured.value)

    def test_relative_links_without_base_url_are_not_annotated(self) -> None:
        """Test that relative links without base url are not annotated."""
        no_base = normalize_content(
            b'<main><p><a href="/apply-v1">Apply</a></p></main>',
            content_type="text/html",
        )
        assert "/apply-v1" not in no_base.text

    def test_fragment_only_and_fragment_destination_changes_are_tracked(
        self,
    ) -> None:
        # canonicalize_url always strips the fragment (it is never sent to
        # the server), so without separate fragment-identity tracking a
        # fragment-only href would be silently dropped and a fragment-only
        # destination change would normalize identically to the unchanged
        # page.
        """Test that fragment only and fragment destination changes are tracked."""
        same_fragment_only = normalize_content(
            b'<main><p><a href="#section">Jump</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert "example.com/page#section" in same_fragment_only.text

        before = normalize_content(
            b'<main><p><a href="/apply#step1">Apply</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            b'<main><p><a href="/apply#step2">Apply</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_long_fragment_identity_distinguishes_divergent_tails(self) -> None:
        # canonicalize_fragment_identity truncates the *display* value to
        # MAX_FRAGMENT_IDENTITY_CHARS (200), but must still hash the
        # complete fragment -- otherwise two fragments sharing a 200-char
        # common prefix would collide even though their tails differ.
        """Test that long fragment identity distinguishes divergent tails."""
        prefix = "step-" * 50  # 250 chars, longer than the 200-char bound
        before = normalize_content(
            f'<main><a href="/apply#{prefix}AAA">Apply</a></main>'.encode(),
            content_type="text/html",
            base_url="https://example.com/page",
        )
        after = normalize_content(
            f'<main><a href="/apply#{prefix}BBB">Apply</a></main>'.encode(),
            content_type="text/html",
            base_url="https://example.com/page",
        )
        assert before.normalized_hash != after.normalized_hash

    def test_credential_bearing_link_fragment_fails_closed(self) -> None:
        # OAuth implicit-flow tokens (and similar credentials) can appear in
        # a URL fragment, so a fragment must fail closed exactly like a
        # credential-bearing query parameter rather than being retained or
        # hashed into a stored artifact.
        """Test that credential bearing link fragment fails closed."""
        source = b'<main><a href="/callback#access_token=secret123">Link</a></main>'
        with pytest.raises(MonitorError) as captured:
            normalize_content(
                source,
                content_type="text/html",
                base_url="https://example.com/page",
            )
        assert "secret123" not in str(captured.value)

    def test_credential_bearing_link_nested_in_fragment_value_fails_closed(
        self,
    ) -> None:
        # canonicalize_url always strips the fragment, so a fragment-only
        # destination is tracked separately via canonicalize_fragment_identity
        # and written verbatim into normalized text/hash once accepted; a
        # benign-looking fragment parameter name (e.g. "redirect") can carry
        # a nested, URL-encoded HTTP(S) URL whose own query carries the
        # credential, so this must fail closed exactly like the query and
        # nested-query cases above rather than write the credential in.
        """Test that credential bearing link nested in fragment value fails closed."""
        destination = (
            "https://example.com/page#redirect=https%3A%2F%2Fidp.example%2F"
            "cb%3Faccess_token%3Dsecret123"
        )
        source = f'<main><a href="{destination}">Link</a></main>'.encode()
        with pytest.raises(MonitorError) as captured:
            normalize_content(
                source,
                content_type="text/html",
                base_url="https://example.com/page",
            )
        assert "secret123" not in str(captured.value)

    def test_standalone_deadline_date_is_preserved_not_erased(self) -> None:
        # A date/time-only line is stripped as routine-timestamp noise, but
        # that must not erase a standalone date that is itself the monitored
        # business data (e.g. an application deadline with no "updated"/
        # "modified"/"published" label). Only explicitly labelled timestamp
        # lines should be treated as noise.
        """Test that standalone deadline date is preserved not erased."""
        before = normalize_content(
            b"<main><h2>Application deadline</h2><p>2026-08-31</p></main>",
            content_type="text/html",
        )
        after = normalize_content(
            b"<main><h2>Application deadline</h2><p>2026-09-30</p></main>",
            content_type="text/html",
        )
        assert "2026-08-31" in before.text
        assert "2026-09-30" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_direct_text_before_and_after_a_block_child_is_not_dropped(self) -> None:
        # An element that mixes its own direct text with a block-level
        # child (e.g. a status line followed by a details <div>) must not
        # silently lose that text from the normalized hash.
        """Test that direct text before and after a block child is not dropped."""
        open_before = normalize_content(
            b"<main>Applications are now open<div>Details</div></main>",
            content_type="text/html",
        )
        closed_before = normalize_content(
            b"<main>Applications are now closed<div>Details</div></main>",
            content_type="text/html",
        )
        assert open_before.normalized_hash != closed_before.normalized_hash
        assert "Applications are now closed" in closed_before.text
        assert "Details" in closed_before.text

        open_after = normalize_content(
            b"<main><div>Details</div>Applications are now open</main>",
            content_type="text/html",
        )
        closed_after = normalize_content(
            b"<main><div>Details</div>Applications are now closed</main>",
            content_type="text/html",
        )
        assert open_after.normalized_hash != closed_after.normalized_hash
        assert "Applications are now closed" in closed_after.text
        assert "Details" in closed_after.text

    def test_selectors_tables_ads_and_drift(self) -> None:
        """Test that selectors tables ads and drift."""
        body = b"""
        <html><body>
          <div class="ad">Buy now</div>
          <main id="content">
            <table><tr><th>Plan</th><th>Price</th></tr>
              <tr><td>Pro</td><td>$20</td></tr></table>
          </main>
        </body></html>
        """
        result = normalize_content(
            body,
            content_type="text/html",
            include_selector="main#content",
            exclude_selectors=(".ad",),
        )
        assert "Plan | Price" in result.text
        assert "Pro | $20" in result.text
        assert "Buy now" not in result.text
        with pytest.raises(MonitorError, match="matched no"):
            normalize_content(
                body,
                content_type="text/html",
                include_selector=".missing",
            )
        with pytest.raises(MonitorError, match="matched no"):
            normalize_content(
                body,
                content_type="text/html",
                exclude_selectors=(".missing",),
            )
        with pytest.raises(MonitorError, match="unsupported"):
            normalize_content(
                body,
                content_type="text/html",
                include_selector="main > table",
            )

    def test_rss_and_atom_order_are_stable(self) -> None:
        """Test that rss and atom order are stable."""
        rss_one = b"""<?xml version="1.0"?>
        <rss><channel>
          <item><guid>2</guid><title>Second</title><link>https://example.com/2</link></item>
          <item><guid>1</guid><title>First</title><link>https://example.com/1</link></item>
        </channel></rss>"""
        rss_two = b"""<?xml version="1.0"?>
        <rss><channel>
          <item><guid>1</guid><title>First</title><link>https://example.com/1</link></item>
          <item><guid>2</guid><title>Second</title><link>https://example.com/2</link></item>
        </channel></rss>"""
        first = normalize_content(rss_one, content_type="application/rss+xml")
        second = normalize_content(rss_two, content_type="application/rss+xml")
        assert first.normalized_hash == second.normalized_hash
        updated = normalize_content(
            rss_two.replace(b"Second", b"Second updated"),
            content_type="application/rss+xml",
        )
        assert first.normalized_hash != updated.normalized_hash
        assert "ENTRY 1" in first.text
        atom = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example,1</id><title>Atom item</title>
            <link href="https://example.com/atom/1"/>
            <updated>2026-01-01T00:00:00Z</updated>
          </entry>
        </feed>"""
        atom_result = normalize_content(atom, content_type="application/atom+xml")
        assert atom_result.metadata["feed_kind"] == "atom"
        assert "ENTRY tag:example,1" in atom_result.text
        removed = normalize_content(
            b"""<rss><channel>
              <item><guid>1</guid><title>First</title></item>
            </channel></rss>""",
            content_type="application/rss+xml",
        )
        feed_diff = compare_content(first.text, removed.text)
        assert any(section.kind == "removed" for section in feed_diff.sections)
        assert any(section.anchor.startswith("ENTRY 2") for section in feed_diff.sections)

    def test_long_feed_entry_ids_keep_full_identity_with_bounded_output(self) -> None:
        # A bare ``stable_id[:1_000]`` makes IDs that differ only after that
        # boundary indistinguishable. Both RSS GUIDs and Atom IDs are entry
        # anchors, so preserve the full identity in a digest while retaining
        # the existing output-size bound.
        """Test that long feed entry ids keep full identity with bounded output."""
        common_prefix = "a" * 1_000

        def render(entry_id: str, feed_kind: str) -> bytes:
            if feed_kind == "rss":
                return (
                    "<rss><channel><item><guid>"
                    f"{entry_id}</guid><title>Post</title>"
                    "</item></channel></rss>"
                ).encode()
            return (
                '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>'
                f"{entry_id}</id><title>Post</title>"
                "</entry></feed>"
            ).encode()

        for feed_kind, content_type in (
            ("rss", "application/rss+xml"),
            ("atom", "application/atom+xml"),
        ):
            with self.subTest(feed_kind=feed_kind):
                before = normalize_content(
                    render(f"{common_prefix}-old", feed_kind),
                    content_type=content_type,
                )
                after = normalize_content(
                    render(f"{common_prefix}-new", feed_kind),
                    content_type=content_type,
                )
                before_entry_id = before.text.splitlines()[0].removeprefix("ENTRY ")
                after_entry_id = after.text.splitlines()[0].removeprefix("ENTRY ")

                assert before.normalized_hash != after.normalized_hash
                assert before_entry_id != after_entry_id
                assert len(before_entry_id) <= 1000
                assert len(after_entry_id) <= 1000
                assert "[sha256:" in before_entry_id
                assert "[sha256:" in after_entry_id

    def test_rss_content_encoded_body_is_captured(self) -> None:
        """Test that rss content encoded body is captured."""
        xml = b"""<?xml version="1.0"?>
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
        <channel>
          <item>
            <guid>1</guid>
            <title>Post</title>
            <content:encoded>Full article body</content:encoded>
          </item>
        </channel></rss>"""
        text, _ = normalize_feed(xml)
        assert "CONTENT Full article body" in text

    def test_rss_content_encoded_survives_alongside_description(self) -> None:
        """Test that rss content encoded survives alongside description."""
        def render(encoded: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <item>
                <guid>1</guid>
                <title>Post</title>
                <description>Stable teaser</description>
                <content:encoded>{encoded}</content:encoded>
              </item>
            </channel></rss>""".encode()
            text, _ = normalize_feed(xml)
            return text

        first = render("Original full article body")
        second = render("Edited full article body")
        assert "Stable teaser" in first
        assert "Original full article body" in first
        assert first != second

    def test_feed_content_destination_only_change_is_not_silently_missed(
        self,
    ) -> None:
        # The feed HTML cleaner used to record only text nodes, so a common
        # RSS body like <a href="...">Apply</a> normalized to "Apply"
        # regardless of the href -- a stable guid/title/link plus a
        # destination-only href change left the entry hash unchanged.
        """Test that feed content destination only change is not silently missed."""
        def render(href: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <link>https://example.com/job-1</link>
                <description>&lt;a href="{href}"&gt;Apply&lt;/a&gt;</description>
                <content:encoded>
                  &lt;a href="{href}"&gt;Apply&lt;/a&gt;
                </content:encoded>
              </item>
            </channel></rss>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/apply-v1")
        after = render("https://example.com/apply-v2")
        assert before != after
        assert "Apply [https://example.com/apply-v1" in before

    def test_feed_content_relative_link_without_entry_link_fails_closed(
        self,
    ) -> None:
        # An item with a stable guid/title but no entry <link> used to reach
        # _content_link_destination with an empty base URL: a relative href
        # like /apply-v1 was silently discarded, so bumping it to /apply-v2
        # left the entry hash unchanged and the destination update invisible.
        """Test that feed content relative link without entry link fails closed."""
        def render(href: str) -> bytes:
            return f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <description>&lt;a href="{href}"&gt;Apply&lt;/a&gt;</description>
              </item>
            </channel></rss>""".encode()

        with pytest.raises(MonitorError, match="feed_content_relative_link"):
            normalize_feed(render("/apply-v1"))

    def test_feed_content_relative_link_resolves_against_channel_link(
        self,
    ) -> None:
        # When an item omits its own <link> but the channel declares one,
        # relative content-link hrefs should resolve against that inherited
        # feed-level base instead of failing closed.
        """Test that feed content relative link resolves against channel link."""
        def render(href: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <link>https://example.com/jobs/</link>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <description>&lt;a href="{href}"&gt;Apply&lt;/a&gt;</description>
              </item>
            </channel></rss>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("apply-v1")
        after = render("apply-v2")
        assert before != after
        assert "Apply [https://example.com/jobs/apply-v1" in before

    def test_feed_channel_link_change_is_tracked_with_fetched_base_url(
        self,
    ) -> None:
        # normalize_content always passes the fetched final URL as base_url
        # (routine.py wires base_url=fetched.final_url). With no xml:base
        # anywhere, `base_url or link or feed_link` used to always select
        # base_url, so an item with no <link> of its own kept resolving its
        # relative content href against the unchanging fetch URL even when
        # the channel <link> moved from /jobs-v1/ to /jobs-v2/, silently
        # missing the destination change. Routed through normalize_content,
        # like the real fetch -> normalize pipeline, rather than calling
        # normalize_feed directly.
        """Test that feed channel link change is tracked with fetched base url."""
        def render(channel_link: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss version="2.0">
            <channel>
              <link>{channel_link}</link>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <description>&lt;a href="apply"&gt;Apply&lt;/a&gt;</description>
              </item>
            </channel></rss>""".encode()
            return normalize_content(
                xml,
                content_type="application/rss+xml",
                base_url="https://example.com/feed.xml",
            ).text

        before = render("https://example.com/jobs-v1/")
        after = render("https://example.com/jobs-v2/")
        assert before != after
        assert "Apply [https://example.com/jobs-v1/apply" in before
        assert "Apply [https://example.com/jobs-v2/apply" in after

    def test_feed_xml_base_only_change_is_not_silently_missed(self) -> None:
        # xml:base overrides the base URI used to resolve relative content
        # links independent of <link> (XML Base). Here the entry's own
        # <link> and the relative href both stay identical across renders;
        # only the feed-level xml:base changes. Ignoring it would resolve
        # "apply" against the unchanged entry link both times and miss that
        # the real destination moved from /v1/ to /v2/.
        """Test that feed xml base only change is not silently missed."""
        def render(base: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom" xml:base="{base}">
              <entry>
                <id>job-1</id>
                <title>Job posting</title>
                <link href="https://example.com/job-1"/>
                <summary>&lt;a href="apply"&gt;Apply&lt;/a&gt;</summary>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/v1/")
        after = render("https://example.com/v2/")
        assert before != after
        assert "Apply [https://example.com/v1/apply" in before
        assert "Apply [https://example.com/v2/apply" in after

    def test_feed_xml_base_at_entry_and_content_scope_overrides_feed_level(
        self,
    ) -> None:
        # xml:base cascades feed -> entry -> content-element scope, each
        # overriding its parent. The feed-level test above only exercises
        # the outermost scope; this exercises the entry-level and
        # per-content-element overrides specifically, since a bug in either
        # would otherwise leave a real destination change unrecorded.
        """Test that feed xml base at entry and content scope overrides feed level."""
        def render(entry_base: str, content_base: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom"
                  xml:base="https://example.com/feed-level/">
              <entry xml:base="{entry_base}">
                <id>job-1</id>
                <title>Job posting</title>
                <link href="https://example.com/job-1"/>
                <summary xml:base="{content_base}">
                  &lt;a href="apply"&gt;Apply&lt;/a&gt;
                </summary>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        entry_before = render("https://example.com/entry-v1/", "")
        entry_after = render("https://example.com/entry-v2/", "")
        assert entry_before != entry_after
        assert "Apply [https://example.com/entry-v1/apply" in entry_before

        content_before = render(
            "https://example.com/entry/", "https://example.com/content-v1/"
        )
        content_after = render(
            "https://example.com/entry/", "https://example.com/content-v2/"
        )
        assert content_before != content_after
        assert "Apply [https://example.com/content-v1/apply" in content_before

    def test_feed_root_xml_base_resolves_against_fetched_document_url(
        self,
    ) -> None:
        # A relative root-level xml:base must resolve against the URL the
        # feed was actually fetched from, not against an entry's own <link>.
        # Here the feed bytes are byte-for-byte identical across renders;
        # only the caller-supplied base_url (the fetched final URL) moves
        # from /v1/ to /v2/. Falling back to the entry link instead of the
        # fetched URL would resolve "apply" against the same address both
        # times and silently miss that the real destination moved.
        """Test that feed root xml base resolves against fetched document url."""
        xml = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xml:base="./">
          <entry>
            <id>job-1</id>
            <title>Job posting</title>
            <link href="https://example.com/job-1"/>
            <summary>&lt;a href="apply"&gt;Apply&lt;/a&gt;</summary>
          </entry>
        </feed>"""

        before, _ = normalize_feed(xml, base_url="https://example.com/v1/feed.xml")
        after, _ = normalize_feed(xml, base_url="https://example.com/v2/feed.xml")
        assert before != after
        assert "Apply [https://example.com/v1/apply" in before
        assert "Apply [https://example.com/v2/apply" in after

    def test_feed_content_link_fragment_destination_change_is_tracked(
        self,
    ) -> None:
        # canonicalize_url always strips the fragment, so a fragment-only
        # destination change inside feed content (e.g. an in-page anchor
        # retargeted from one step to another) must not be silently absorbed
        # into an identical normalized text.
        """Test that feed content link fragment destination change is tracked."""
        def render(href: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <link>https://example.com/jobs/</link>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <description>&lt;a href="{href}"&gt;Apply&lt;/a&gt;</description>
              </item>
            </channel></rss>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("apply#step1")
        after = render("apply#step2")
        assert before != after

    def test_feed_content_link_credential_fragment_fails_closed(self) -> None:
        """Test that feed content link credential fragment fails closed."""
        xml = b"""<?xml version="1.0"?>
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
        <channel>
          <link>https://example.com/jobs/</link>
          <item>
            <guid>job-1</guid>
            <title>Job posting</title>
            <description>&lt;a href="https://example.com/callback#access_token=secret123"&gt;Apply&lt;/a&gt;</description>
          </item>
        </channel></rss>"""
        with pytest.raises(MonitorError) as captured:
            normalize_feed(xml)
        assert "secret123" not in str(captured.value)

    def test_feed_content_link_nested_credential_destination_fails_closed(
        self,
    ) -> None:
        # Same nested-credential-URL case as the HTML link destination
        # check, reached through the feed content-link path instead.
        """Test that feed content link nested credential destination fails closed."""
        destination = (
            "https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
            "cb%3Faccess_token%3Dsecret123"
        )
        xml = f"""<?xml version="1.0"?>
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
        <channel>
          <link>https://example.com/jobs/</link>
          <item>
            <guid>job-1</guid>
            <title>Job posting</title>
            <description>&lt;a href="{destination}"&gt;Apply&lt;/a&gt;</description>
          </item>
        </channel></rss>""".encode()
        with pytest.raises(MonitorError) as captured:
            normalize_feed(xml)
        assert "secret123" not in str(captured.value)

    def test_feed_content_unterminated_anchor_href_is_not_dropped(self) -> None:
        # HTMLParser.close() does not synthesize missing </a> events, so a
        # malformed embedded anchor with no closing tag (common in feed
        # content) used to leave its href stuck unflushed: a destination-only
        # change to it left the entry's normalized text/hash unchanged.
        """Test that feed content unterminated anchor href is not dropped."""
        def render(href: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel>
              <link>https://example.com/jobs/</link>
              <item>
                <guid>job-1</guid>
                <title>Job posting</title>
                <description>&lt;a href="{href}"&gt;Apply</description>
              </item>
            </channel></rss>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/apply-v1")
        after = render("https://example.com/apply-v2")
        assert "apply-v1" in before
        assert before != after

    def test_atom_xhtml_content_destination_only_change_is_not_missed(
        self,
    ) -> None:
        # Atom permits inline markup as real XML children (<content
        # type="xhtml"><div><a href="...">...</a></div></content>), not
        # just HTML escaped into text -- ElementTree's itertext() drops
        # attributes, so a real <a> element's href needs its own structural
        # walk rather than relying on the escaped-text HTML parser.
        """Test that atom xhtml content destination only change is not missed."""
        def render(href: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>1</id><title>Post</title>
                <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
                <a href="{href}">Apply</a></div></content>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/apply-v1")
        after = render("https://example.com/apply-v2")
        assert before != after
        assert "Apply [https://example.com/apply-v1" in before

    def test_atom_xhtml_descendant_xml_base_change_is_not_missed(self) -> None:
        # XML Base can be overridden on any descendant of the content
        # element, not just re-declared at the content element itself,
        # so every real anchor below it was previously resolved against
        # the same content-level base regardless of a wrapper's own
        # xml:base. Changing only the wrapping <div>'s xml:base must
        # change the resolved destination.
        """Test that atom xhtml descendant xml base change is not missed."""
        def render(div_base: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>1</id><title>Post</title>
                <link href="https://example.com/post-1"/>
                <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"
                xml:base="{div_base}">
                <a href="apply">Apply</a></div></content>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/v1/")
        after = render("https://example.com/v2/")
        assert before != after
        assert "Apply [https://example.com/v1/apply" in before

    def test_atom_xhtml_anchor_own_xml_base_change_is_not_missed(self) -> None:
        # xml:base can be set on the anchor element itself rather than a
        # wrapping ancestor, with no xml:base anywhere else in the content
        # subtree. Only the anchor's own base changes here.
        """Test that atom xhtml anchor own xml base change is not missed."""
        def render(anchor_base: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>1</id><title>Post</title>
                <link href="https://example.com/post-1"/>
                <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
                <a xml:base="{anchor_base}" href="apply">Apply</a></div></content>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/v1/")
        after = render("https://example.com/v2/")
        assert before != after
        assert "Apply [https://example.com/v1/apply" in before

    def test_atom_xhtml_deep_nesting_does_not_raise_recursion_error(self) -> None:
        # _element_link_destinations walks the real XHTML element tree to
        # apply per-descendant xml:base; a naive recursive walk would hit
        # Python's call-stack recursion limit on a deeply nested (but
        # otherwise small, well within max_elements) content tree and raise
        # an unhandled RecursionError instead of failing closed with a
        # MonitorError or succeeding outright.
        """Test that atom xhtml deep nesting does not raise recursion error."""
        depth = 1_500
        xml = (
            b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            b"<entry><id>1</id><title>Post</title>"
            b'<link href="https://example.com/post-1"/>'
            b'<content type="xhtml">'
            + b'<div xmlns="http://www.w3.org/1999/xhtml">' * depth
            + b'<a href="apply">Apply</a>'
            + b"</div>" * depth
            + b"</content></entry></feed>"
        )
        text, _ = normalize_feed(xml)
        assert "Apply [https://example.com/apply" in text

    def test_content_link_budget_covers_an_ordinary_large_feed(self) -> None:
        # The link-destination annotation budget is shared across the whole
        # feed (the feed is normalized into one stored/hashed/diffed blob,
        # so the aggregate output is what must stay bounded), but it must
        # still be sized generously enough that an ordinary large feed with
        # one link per entry, up to the default max_entries, never trips it.
        """Test that content link budget covers an ordinary large feed."""
        entries = "".join(
            f"<item><guid>{i}</guid><title>Post {i}</title>"
            f'<description>&lt;a href="https://example.com/{i}"&gt;'
            "Link&lt;/a&gt;</description></item>"
            for i in range(1_000)
        )
        xml = f"<rss><channel>{entries}</channel></rss>".encode()
        text, metadata = normalize_feed(xml)
        assert metadata["entry_count"] == "1000"
        assert "https://example.com/0" in text
        assert "https://example.com/999" in text

    def test_content_link_budget_fails_closed_when_exhausted(self) -> None:
        # A feed with far more embedded link destinations than any
        # legitimate feed would carry must fail closed instead of producing
        # unbounded normalized output.
        """Test that content link budget fails closed when exhausted."""
        links = "".join(
            f'&lt;a href="https://example.com/{i}"&gt;Link{i}&lt;/a&gt; '
            for i in range(6_000)
        )
        xml = (
            "<rss><channel><item><guid>1</guid><title>Post</title>"
            f"<description>{links}</description></item></channel></rss>"
        ).encode()
        with pytest.raises(MonitorError, match="feed_too_large"):
            normalize_feed(xml)

    def test_atom_prefers_alternate_link_over_self(self) -> None:
        """Test that atom prefers alternate link over self."""
        def render(destination: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>tag:example,1</id><title>Post</title>
                <link rel="self" href="https://example.com/feed/entry/1"/>
                <link rel="alternate" href="{destination}"/>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/articles/old")
        after = render("https://example.com/articles/new")
        assert "LINK https://example.com/articles/old" in before
        assert "https://example.com/feed/entry/1" not in before
        assert before != after

    def test_atom_external_content_source_is_canonicalized_and_captured(self) -> None:
        """Test that atom external content source is canonicalized and captured."""
        def render(source: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>tag:example,1</id><title>Post</title>
                <content src="{source}"/>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://EXAMPLE.com:443/external/v1")
        after = render("https://example.com/external/v2")
        assert "CONTENT_SRC https://example.com/external/v1" in before
        assert before != after

    def test_atom_content_source_fragment_change_is_tracked(self) -> None:
        # canonicalize_url always strips the fragment, so a content src
        # differing only by fragment (e.g. "#v1" -> "#v2") must not collapse
        # into the same stored CONTENT_SRC identity/hash.
        """Test that atom content source fragment change is tracked."""
        def render(source: str) -> str:
            xml = f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <id>tag:example,1</id><title>Post</title>
                <content src="{source}"/>
              </entry>
            </feed>""".encode()
            text, _ = normalize_feed(xml)
            return text

        before = render("https://example.com/doc#v1")
        after = render("https://example.com/doc#v2")
        assert "CONTENT_SRC https://example.com/doc#v1" in before
        assert before != after

    def test_atom_relative_external_content_source_is_rejected(self) -> None:
        """Test that atom relative external content source is rejected."""
        relative = b"""<feed xmlns="http://www.w3.org/2005/Atom"
            xml:base="https://example.com/">
          <entry><id>tag:example,1</id><content src="external/v1"/></entry>
        </feed>"""
        with pytest.raises(MonitorError, match="absolute HTTP"):
            normalize_feed(relative)

        credential_source = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example,1</id>
            <content src="https://example.com/external?token=secret"/>
          </entry>
        </feed>"""
        with pytest.raises(MonitorError, match="unsafe external content"):
            normalize_feed(credential_source)

    def test_feed_relative_entry_link_is_explicitly_rejected(self) -> None:
        """Test that feed relative entry link is explicitly rejected."""
        relative = b"""<feed xmlns="http://www.w3.org/2005/Atom"
            xml:base="https://example.com/">
          <entry><id>tag:example,1</id><link rel="alternate" href="post/1"/></entry>
        </feed>"""
        with pytest.raises(MonitorError, match="absolute HTTP"):
            normalize_feed(relative)

    def test_feed_rejects_entities_malformed_and_type_mismatch(self) -> None:
        """Test that feed rejects entities malformed and type mismatch."""
        with pytest.raises(MonitorError, match="DOCTYPE"):
            normalize_content(
                b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "x">]><rss/>',
                content_type="application/rss+xml",
            )
        with pytest.raises(MonitorError):
            normalize_content(
                b"<?xml version='1.0'?><rss><item>",
                content_type="application/rss+xml",
            )
        with pytest.raises(MonitorError, match="does not match"):
            normalize_content(b"%PDF-1.4\n%%EOF", content_type="text/html")
        utf16 = '<?xml version="1.0"?><rss><channel/></rss>'.encode("utf-16")
        with pytest.raises(MonitorError, match="UTF-16/32"):
            normalize_feed(utf16)
        unsafe_link = (
            b"<rss><channel><item><guid>1</guid>"
            b"<link>https://example.com/?to"
            b"ken=fixture</link></item></channel></rss>"
        )
        with pytest.raises(MonitorError, match="unsafe link"):
            normalize_content(unsafe_link, content_type="application/rss+xml")

    def test_html_wide_element_count_is_bounded(self) -> None:
        # The byte-size cap on the raw response does not bound the number of
        # parsed Node objects: a few megabytes of tiny tags can still exceed
        # html_normalizer.MAX_NODES and drive excessive memory use in the
        # tree walks. This must fail closed rather than parse unboundedly.
        """Test that html wide element count is bounded."""
        wide = b"<html><body>" + b"<p>x</p>" * 60_000 + b"</body></html>"
        with pytest.raises(MonitorError, match="too many elements"):
            normalize_content(wide, content_type="text/html")

    def test_html_deep_nesting_is_bounded(self) -> None:
        # iter_nodes()/_text_content()/visit() all recurse to tree depth.
        # Nesting beyond html_normalizer.MAX_DEPTH must be rejected during
        # parsing instead of risking a RecursionError deep inside those
        # walks on untrusted HTML.
        """Test that html deep nesting is bounded."""
        deep = (
            b"<html><body>"
            + b"<div>" * 300
            + b"x"
            + b"</div>" * 300
            + b"</body></html>"
        )
        with pytest.raises(MonitorError, match="nesting is too deep"):
            normalize_content(deep, content_type="text/html")

    def test_text_pdf_and_pdf_failures(self) -> None:
        """Test that text pdf and pdf failures."""
        pdf = _text_pdf(b"(Hello PDF) Tj")
        result = normalize_content(pdf, content_type="application/pdf")
        assert result.kind == "pdf"
        assert "Hello PDF" in result.text
        encrypted = b"%PDF-1.4\n1 0 obj << /Encrypt 2 0 R >> endobj\n%%EOF"
        with pytest.raises(MonitorError, match="encrypted"):
            normalize_content(encrypted, content_type="application/pdf")
        image_only = _image_only_pdf()
        with pytest.raises(MonitorError, match="image-only"):
            normalize_content(image_only, content_type="application/pdf")

    def test_pdf_parser_is_lazy_and_reports_a_missing_capability(self) -> None:
        # HTML-only callers must not need pypdf just because normalize.py
        # imports the PDF normalizer. A PDF request should instead return a
        # stable, actionable error when the optional runtime capability is
        # absent.
        """Test that pdf parser is lazy and reports a missing capability."""
        pdf = _text_pdf(b"(Parser capability) Tj")
        original_import = builtins.__import__

        def reject_pypdf(
            name: str,
            globals: object = None,
            locals: object = None,
            fromlist: object = (),
            level: int = 0,
        ) -> object:
            if name == "pypdf" or name.startswith("pypdf."):
                msg = f"No module named {name!r}"
                raise ModuleNotFoundError(msg, name=name)
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", new=reject_pypdf):
            html = normalize_content(
                b"<html><body><main>HTML remains available</main></body></html>",
                content_type="text/html",
            )
            assert "HTML remains available" in html.text
            with pytest.raises(MonitorError, match="pdf_parser_unavailable"):
                normalize_content(pdf, content_type="application/pdf")

    def test_generated_text_pdf_with_filtered_images_is_extracted(self) -> None:
        """Test that generated text pdf with filtered images is extracted."""
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        resources = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        xobjects = DictionaryObject()
        for index, filter_name in enumerate(("/DCTDecode", "/JPXDecode"), start=1):
            image = StreamObject()
            image._data = b"bounded encoded image fixture"
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/Filter"): NameObject(filter_name),
                }
            )
            xobjects[NameObject(f"/Im{index}")] = writer._add_object(image)
        resources[NameObject("/XObject")] = xobjects
        page[NameObject("/Resources")] = resources
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 36 120 Td (Text with images) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        output = BytesIO()
        writer.write(output)

        result = normalize_content(output.getvalue(), content_type="application/pdf")

        assert "Text with images" in result.text

    def test_pdf_image_classification_ignores_nested_and_string_markers(
        self,
    ) -> None:
        """Test that pdf image classification ignores nested and string markers."""
        fake_image_markers = (
            b"/Note (/Type /XObject /Subtype /Image)",
            b"/Metadata << /Type /XObject /Subtype /Image >>",
        )
        for marker in fake_image_markers:
            with self.subTest(marker=marker):
                pdf = _pdf(
                    _pdf_stream(
                        1,
                        b"28537461626c65207465787429",
                        extra=marker + b" /Filter /ASCIIHexDecode",
                    )
                )
                with pytest.raises(MonitorError, match="filter"):
                    normalize_content(pdf, content_type="application/pdf")

    def test_pdf_tj_array_hex_strings_are_not_silently_dropped(self) -> None:
        # TJ arrays commonly mix literal (...) runs with hex-encoded <...>
        # runs (e.g. a font/CMap-encoded value between literal label text).
        # Only extracting the literal runs would keep the normalized hash
        # stable even when the hex-encoded content changes.
        """Test that pdf tj array hex strings are not silently dropped."""
        mixed = _text_pdf(b"[(Hello ) <576f726c64>] TJ")
        result = normalize_content(mixed, content_type="application/pdf")
        assert "Hello World" in result.text
        before = normalize_content(
            _text_pdf(b"[(Total: ) <30303030>] TJ"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"[(Total: ) <39393939>] TJ"),
            content_type="application/pdf",
        )
        assert "Total: 0000" in before.text
        assert "Total: 9999" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_quote_operators_are_not_silently_dropped(self) -> None:
        # ' (move to next line, show text) and " (set spacing, move to next
        # line, show text) are valid text-showing operators alongside Tj/TJ.
        # A parser that only understands Tj/TJ extracts the header but
        # silently drops content shown only via ' or ", so an edit confined
        # to that content would leave the normalized hash unchanged.
        """Test that pdf quote operators are not silently dropped."""
        before = normalize_content(
            _text_pdf(b"(Header) Tj (Old status) '"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(Header) Tj (New status) '"),
            content_type="application/pdf",
        )
        assert "Old status" in before.text
        assert "New status" in after.text
        assert before.normalized_hash != after.normalized_hash

        double_quote = normalize_content(
            _text_pdf(b'(Header) Tj 0 0 (Quoted status) "'),
            content_type="application/pdf",
        )
        assert "Quoted status" in double_quote.text

    def test_pdf_et_inside_a_string_operand_does_not_truncate_the_text_block(
        self,
    ) -> None:
        # A literal string operand that happens to contain "ET" (e.g. inside
        # "status ET old") must remain text rather than being mistaken for
        # the end-text operator and silently dropped from the hash.
        """Test that pdf et inside a string operand does not truncate the text block."""
        before = normalize_content(
            _text_pdf(b"(status ET old) Tj"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(status ET new) Tj"),
            content_type="application/pdf",
        )
        assert "status ET old" in before.text
        assert "status ET new" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_et_inside_a_name_token_does_not_truncate_the_text_block(
        self,
    ) -> None:
        # A marked-content tag like "/ETMarker" contains the literal bytes
        # "ET" outside of any string. It must not cause the real Tj call
        # that follows to be silently dropped.
        """Test that pdf et inside a name token does not truncate the text block."""
        before = normalize_content(
            _text_pdf(b"/ETMarker BMC (Old status) Tj EMC"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"/ETMarker BMC (New status) Tj EMC"),
            content_type="application/pdf",
        )
        assert "Old status" in before.text
        assert "New status" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_nested_parens_in_a_string_are_not_silently_dropped(self) -> None:
        # A literal string can contain balanced, unescaped nested parens
        # (e.g. "(Old (status))"). A flat, non-recursive string regex only
        # matches up to the first unescaped ")", misaligning with the "Tj"
        # that follows and silently dropping the whole operand instead of
        # extracting "Old (status)".
        """Test that pdf nested parens in a string are not silently dropped."""
        before = normalize_content(
            _text_pdf(b"(Stable) Tj (Old (status)) Tj"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(Stable) Tj (New (status)) Tj"),
            content_type="application/pdf",
        )
        assert "Old (status)" in before.text
        assert "New (status)" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_endstream_inside_a_string_operand_does_not_truncate_the_stream(
        self,
    ) -> None:
        # A parser that locates the stream body by scanning for the first
        # newline-delimited "endstream" bytes (rather than honoring the
        # dictionary's /Length) truncates here: this literal string operand
        # legally contains "\nendstream" as raw content, and everything
        # after it -- including a later, otherwise-stable Tj whose own text
        # changes -- would be silently dropped from the normalized hash.
        """Test that pdf endstream inside a string operand does not truncate the stream."""
        def stream(edit: bytes) -> bytes:
            return (
                b"(marker\nendstream inside a string) Tj (Stable) Tj (" + edit + b") Tj"
            )

        before = normalize_content(
            _text_pdf(stream(b"Old edit")),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(stream(b"New edit")),
            content_type="application/pdf",
        )
        assert "Stable" in before.text
        assert "Old edit" in before.text
        assert "New edit" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_stream_keyword_inside_a_stream_body_is_not_rescanned(self) -> None:
        """Test that pdf stream keyword inside a stream body is not rescanned."""
        body = b"(marker\nstream\ninside a string) Tj"

        normalized = normalize_content(
            _text_pdf(body),
            content_type="application/pdf",
        )

        assert "stream" in normalized.text

    def test_pdf_unterminated_string_fails_closed(self) -> None:
        """Test that pdf unterminated string fails closed."""
        pdf = _pdf(_pdf_stream(1, b"BT (unterminated Tj ET"))
        with pytest.raises(MonitorError, match="pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_standard_generated_font_pdf_is_extracted(self) -> None:
        # Generated by ReportLab 4 with Helvetica and page compression off.
        # This is an ordinary, xref-bearing PDF with /BaseFont and
        # /WinAnsiEncoding rather than a synthetic font-less content stream.
        """Test that standard generated font pdf is extracted."""
        encoded = (
            "JVBERi0xLjMKJZOMi54gUmVwb3J0TGFiIEdlbmVyYXRlZCBQREYgZG9jdW1lbnQg"
            "KG9wZW5zb3VyY2UpCjEgMCBvYmoKPDwKL0YxIDIgMCBSCj4+CmVuZG9iagoyIDAg"
            "b2JqCjw8Ci9CYXNlRm9udCAvSGVsdmV0aWNhIC9FbmNvZGluZyAvV2luQW5zaUVu"
            "Y29kaW5nIC9OYW1lIC9GMSAvU3VidHlwZSAvVHlwZTEgL1R5cGUgL0ZvbnQKPj4K"
            "ZW5kb2JqCjMgMCBvYmoKPDwKL0NvbnRlbnRzIDcgMCBSIC9NZWRpYUJveCBbIDAg"
            "MCAzMDAgMjAwIF0gL1BhcmVudCA2IDAgUiAvUmVzb3VyY2VzIDw8Ci9Gb250IDEg"
            "MCBSIC9Qcm9jU2V0IFsgL1BERiAvVGV4dCAvSW1hZ2VCIC9JbWFnZUMgL0ltYWdl"
            "SSBdCj4+IC9Sb3RhdGUgMCAvVHJhbnMgPDwKCj4+IAogIC9UeXBlIC9QYWdlCj4+"
            "CmVuZG9iago0IDAgb2JqCjw8Ci9QYWdlTW9kZSAvVXNlTm9uZSAvUGFnZXMgNiAw"
            "IFIgL1R5cGUgL0NhdGFsb2cKPj4KZW5kb2JqCjUgMCBvYmoKPDwKL0F1dGhvciAo"
            "YW5vbnltb3VzKSAvQ3JlYXRpb25EYXRlIChEOjIwMDAwMTAxMDAwMDAwKzAwJzAw"
            "JykgL0NyZWF0b3IgKGFub255bW91cykgL0tleXdvcmRzICgpIC9Nb2REYXRlIChE"
            "OjIwMDAwMTAxMDAwMDAwKzAwJzAwJykgL1Byb2R1Y2VyIChSZXBvcnRMYWIgUERG"
            "IExpYnJhcnkgLSBcKG9wZW5zb3VyY2VcKSkgCiAgL1N1YmplY3QgKHVuc3BlY2lm"
            "aWVkKSAvVGl0bGUgKHVudGl0bGVkKSAvVHJhcHBlZCAvRmFsc2UKPj4KZW5kb2Jq"
            "CjYgMCBvYmoKPDwKL0NvdW50IDEgL0tpZHMgWyAzIDAgUiBdIC9UeXBlIC9QYWdl"
            "cwo+PgplbmRvYmoKNyAwIG9iago8PAovTGVuZ3RoIDE2Ngo+PgpzdHJlYW0KMSAw"
            "IDAgMSAwIDAgY20gIEJUIC9GMSAxMiBUZiAxNC40IFRMIEVUCkJUIC9GMSAxMiBU"
            "ZiAxNC40IFRMIEVUCkJUIDEgMCAwIDEgMzYgMTIwIFRtIChTdGFuZGFyZCBnZW5l"
            "cmF0ZWQgUERGKSBUaiBUKiBFVApCVCAxIDAgMCAxIDM2IDk2IFRtIChQcmljZTog"
            "NDIgVVNEKSBUaiBUKiBFVAogCmVuZHN0cmVhbQplbmRvYmoKeHJlZgowIDgKMDAw"
            "MDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDYxIDAwMDAwIG4gCjAwMDAwMDAwOTIg"
            "MDAwMDAgbiAKMDAwMDAwMDE5OSAwMDAwMCBuIAowMDAwMDAwMzkyIDAwMDAwIG4g"
            "CjAwMDAwMDA0NjAgMDAwMDAgbiAKMDAwMDAwMDcyMSAwMDAwMCBuIAowMDAwMDAw"
            "NzgwIDAwMDAwIG4gCnRyYWlsZXIKPDwKL0lEIApbPDFjMTc4MTk4ZmJkZmE1MWIy"
            "NTk5NWQ4OWQ0MTAyMDQzPjwxYzE3ODE5OGZiZGZhNTFiMjU5OTVkODlkNDEwMjA0"
            "Mz5dCiUgUmVwb3J0TGFiIGdlbmVyYXRlZCBQREYgZG9jdW1lbnQgLS0gZGlnZXN0"
            "IChvcGVuc291cmNlKQoKL0luZm8gNSAwIFIKL1Jvb3QgNCAwIFIKL1NpemUgOAo+"
            "PgpzdGFydHhyZWYKOTk2CiUlRU9GCg=="
        )
        result = normalize_content(
            base64.b64decode(encoded), content_type="application/pdf"
        )
        assert "Standard generated PDF" in result.text
        assert "Price: 42 USD" in result.text

    def test_pdf_link_annotation_destination_only_change_is_not_missed(
        self,
    ) -> None:
        # page.extract_text() reads only the visible content stream and
        # omits /Annots URI actions, so two PDFs with the same visible
        # label ("Apply") but a changed clickable destination must not
        # normalize to the same text/hash.
        """Test that pdf link annotation destination only change is not missed."""
        before = normalize_content(
            _text_pdf_with_link(b"(Apply) Tj", uri="https://example.com/apply-v1"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf_with_link(b"(Apply) Tj", uri="https://example.com/apply-v2"),
            content_type="application/pdf",
        )
        assert "apply-v1" in before.text
        assert "apply-v2" in after.text
        assert before.normalized_hash != after.normalized_hash

    def test_pdf_link_annotation_credential_destination_fails_closed(
        self,
    ) -> None:
        """Test that pdf link annotation credential destination fails closed."""
        pdf = _text_pdf_with_link(
            b"(Apply) Tj", uri="https://example.com/apply?token=secret123"
        )
        with pytest.raises(MonitorError) as captured:
            normalize_content(pdf, content_type="application/pdf")
        assert "secret123" not in str(captured.value)

    def test_pdf_link_annotation_nested_credential_destination_fails_closed(
        self,
    ) -> None:
        # Same nested-credential-URL case as the HTML link destination
        # check, reached through the PDF /URI link-annotation path instead.
        """Test that pdf link annotation nested credential destination fails closed."""
        destination = (
            "https://example.com/?redirect=https%3A%2F%2Fidp.example%2F"
            "cb%3Faccess_token%3Dsecret123"
        )
        pdf = _text_pdf_with_link(b"(Apply) Tj", uri=destination)
        with pytest.raises(MonitorError) as captured:
            normalize_content(pdf, content_type="application/pdf")
        assert "secret123" not in str(captured.value)

    def test_pdf_link_annotation_non_web_scheme_is_omitted(self) -> None:
        """Test that pdf link annotation non web scheme is omitted."""
        pdf = _text_pdf_with_link(b"(Apply) Tj", uri="mailto:jobs@example.com")
        result = normalize_content(pdf, content_type="application/pdf")
        assert "Apply" in result.text
        assert "jobs@example.com" not in result.text

    def test_pdf_relative_link_action_fails_closed(self) -> None:
        # A /URI action with no scheme (e.g. "/apply-v1") is a relative
        # reference, not a non-web scheme like mailto:/tel: -- this
        # normalizer has no document base to resolve it against, so it must
        # fail closed instead of being silently omitted like a destination
        # change would otherwise go undetected.
        """Test that pdf relative link action fails closed."""
        pdf = _text_pdf_with_link(b"(Apply) Tj", uri="/apply-v1")
        with pytest.raises(MonitorError, match="pdf_relative_link_action"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_indirect_length_reference_is_resolved(self) -> None:
        # /Length can be an indirect reference ("N G R") to a separate
        # object holding the bare integer, not just a direct integer.
        """Test that pdf indirect length reference is resolved."""
        pdf = _text_pdf(b"(Indirect length) Tj", indirect_length=True)
        result = normalize_content(pdf, content_type="application/pdf")
        assert "Indirect length" in result.text

    def test_pdf_unresolvable_indirect_length_fails_closed(self) -> None:
        """Test that pdf unresolvable indirect length fails closed."""
        body = b"BT (Indirect length) Tj ET"
        pdf = _pdf(
            b"1 0 obj\n<< /Length 3 0 R >>\nstream\n" + body + b"\nendstream\nendobj\n"
        )
        with pytest.raises(MonitorError, match="pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_unsupported_filter_streams_are_rejected_not_skipped(self) -> None:
        # A stream without /FlateDecode is currently assumed to already be
        # decoded plain content. But /ASCIIHexDecode, /ASCII85Decode,
        # /LZWDecode, /RunLengthDecode, and filter chains are also valid and
        # leave their bytes filter-encoded, not plain text. If only the
        # unfiltered stream is scanned, a change confined to a filtered
        # stream would leave the normalized hash unchanged, so such filters
        # must be rejected rather than silently skipped.
        """Test that pdf unsupported filter streams are rejected not skipped."""
        before = _pdf(
            _pdf_stream(1, b"BT (Stable label) Tj ET"),
            _pdf_stream(
                2, b"28546f74616c3a20303030302947", extra=b"/Filter /ASCIIHexDecode"
            ),
        )
        after = _pdf(
            _pdf_stream(1, b"BT (Stable label) Tj ET"),
            _pdf_stream(
                2, b"28546f74616c3a20393939392947", extra=b"/Filter /ASCIIHexDecode"
            ),
        )
        with pytest.raises(MonitorError, match="filter"):
            normalize_content(before, content_type="application/pdf")
        with pytest.raises(MonitorError, match="filter"):
            normalize_content(after, content_type="application/pdf")

    def test_pdf_filter_beyond_a_fixed_lookbehind_window_is_still_detected(
        self,
    ) -> None:
        # A stream dictionary can exceed a fixed-size lookbehind window. If
        # /Filter appears more than that window's width before the "stream"
        # keyword, a scan bounded by a fixed window would see no filter and
        # silently treat the still-encoded bytes as plain content instead of
        # rejecting the unsupported filter.
        """Test that pdf filter beyond a fixed lookbehind window is still detected."""
        padding = b"x" * 1_200
        pdf = _pdf(
            _pdf_stream(
                1,
                b"28546f74616c3a20303030302947",
                extra=b"/Filter /ASCIIHexDecode /Extra (" + padding + b")",
            )
        )
        with pytest.raises(MonitorError, match="filter"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_escaped_filter_key_cannot_bypass_decompression_bound(self) -> None:
        # A valid #xx name escape makes /Fil#74er semantically equivalent to
        # /Filter. It must be decoded before pypdf can inflate the stream.
        """Test that pdf escaped filter key cannot bypass decompression bound."""
        pdf = _escaped_filter_text_pdf(b"x" * 2_048)

        with pytest.raises(MonitorError) as raised:
            extract_pdf_text(
                pdf,
                max_input_bytes=len(pdf),
                max_decompressed_bytes=100,
            )

        assert raised.value.code == "pdf_decompressed_too_large"

    def test_pdf_duplicate_filter_keys_fail_closed(self) -> None:
        """Test that pdf duplicate filter keys fail closed."""
        compressed = zlib.compress(b"BT (bounded) Tj ET")
        pdf = _pdf(
            _pdf_stream(
                1,
                compressed,
                extra=b"/Filter /FlateDecode /Fil#74er /FlateDecode",
            )
        )

        with pytest.raises(MonitorError) as raised:
            extract_pdf_text(pdf, max_input_bytes=len(pdf))

        assert raised.value.code == "pdf_malformed"

    def test_pdf_filter_name_value_is_not_misclassified_as_a_key(self) -> None:
        """Test that pdf filter name value is not misclassified as a key."""
        assert _stream_filters(b"<< /Length 1 /Marker /Filter >>") == []

    def test_pdf_stream_dictionary_nesting_is_bounded(self) -> None:
        """Test that pdf stream dictionary nesting is bounded."""
        dictionary = b"<< /Length 1 /Metadata " + b"[" * 101 + b"/Value" + b"]" * 101
        dictionary += b" >>"

        with pytest.raises(MonitorError) as raised:
            _stream_filters(dictionary)

        assert raised.value.code == "pdf_malformed"

    def test_pdf_truncated_flate_stream_fails_closed(self) -> None:
        # zlib.decompressobj() commonly returns partial output for truncated
        # input without raising zlib.error, so a stream cut short mid-flush
        # could otherwise be accepted as valid text instead of rejected.
        """Test that pdf truncated flate stream fails closed."""
        compressed = zlib.compress(b"BT (Hello Flate PDF) Tj ET")
        truncated = compressed[:-4]
        pdf = _pdf(_pdf_stream(1, truncated, extra=b"/Filter /FlateDecode"))
        with pytest.raises(MonitorError, match="truncated|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_stream_without_an_enclosing_object_fails_closed(self) -> None:
        # A stream with no discoverable "N G obj" header before it has no
        # provable dictionary association; the filter cannot be trusted
        # either way, so this must reject rather than treat it as unfiltered
        # plain content.
        """Test that pdf stream without an enclosing object fails closed."""
        pdf = b"%PDF-1.4\nstream\nBT (Hello) Tj ET\nendstream\n%%EOF"
        with pytest.raises(MonitorError, match="pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_malformed_font_pdfs_are_rejected_not_mishashed(self) -> None:
        # Font-bearing PDFs are routed through pypdf so character codes are
        # resolved through the active encoding/CMap. These deliberately
        # incomplete fixtures have no page tree/xref and must fail closed.
        """Test that malformed font pdfs are rejected not mishashed."""
        to_unicode = (
            b"%PDF-1.4\n1 0 obj\n<< /ToUnicode 2 0 R >>\nendobj\n"
            b"3 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(to_unicode, content_type="application/pdf")

        differences = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding << /Differences [1 /A] >> >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(differences, content_type="application/pdf")

        named_encoding = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding /WinAnsiEncoding >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(named_encoding, content_type="application/pdf")

        # A PDF name may encode letters with #xx escapes. The prior raw-byte
        # gate did not recognize these names and could route this font-backed
        # stream to a Latin-1 operand decoder; every accepted PDF now uses
        # the font-aware parser instead.
        escaped_font_names = (
            b"%PDF-1.4\n1 0 obj\n"
            b"<< /Type /F#6fnt /Subtype /Type1 /Base#46ont /Symbol >>\n"
            b"endobj\n" + _pdf_stream(2, b"BT /F1 12 Tf <41> Tj ET") + b"%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(escaped_font_names, content_type="application/pdf")

        # A simple font can omit /Encoding and use its built-in encoding.
        for base_font in (b"Helvetica", b"Symbol"):
            with self.subTest(base_font=base_font):
                built_in_encoding = (
                    b"%PDF-1.4\n1 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /"
                    + base_font
                    + b" >>\nendobj\n"
                    + _pdf_stream(2, b"BT /F1 12 Tf <41> Tj ET")
                    + b"%%EOF"
                )
                with pytest.raises(MonitorError, match="malformed"):
                    normalize_content(built_in_encoding, content_type="application/pdf")

        # A composite (/Type0) font always routes character codes through a
        # CMap, regardless of /Encoding/ToUnicode presence.
        composite_font = (
            b"%PDF-1.4\n1 0 obj\n<< /Subtype /Type0 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(composite_font, content_type="application/pdf")

        # A compressed object stream can hide a font/Encoding dictionary,
        # so a raw fallback would not be safe even if no obvious font name
        # were present in the top-level bytes.
        object_stream = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /ObjStm /N 1 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with pytest.raises(MonitorError, match="malformed"):
            normalize_content(object_stream, content_type="application/pdf")

    def test_xhtml_with_xml_declaration_is_detected_as_html(self) -> None:
        """Test that xhtml with xml declaration is detected as html."""
        result = normalize_content(
            (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                b"<main><p>Application open</p></main></body></html>"
            ),
            content_type="application/xhtml+xml",
        )
        assert result.kind == "html"
        assert "Application open" in result.text


class DiffTests(unittest.TestCase):
    """Tests for DiffTests."""

    def test_unchanged_and_first_fetch_short_circuit(self) -> None:
        """Test that unchanged and first fetch short circuit."""
        baseline = compare_content(None, "hello")
        assert baseline.result == "baseline_created"
        assert not baseline.should_summarize
        unchanged = compare_content("old", "new", previous_hash="a", current_hash="a")
        assert unchanged.result == "unchanged"
        assert unchanged.sections == ()

    def test_noise_is_minor_but_important_patterns_are_candidates(self) -> None:
        """Test that noise is minor but important patterns are candidates."""
        noise = compare_content("Updated: 2026-07-30", "Updated: 2026-07-31")
        assert noise.result == "minor"
        assert noise.change_score < 35
        important_cases = (
            ("Price: $10", "Price: $20"),
            ("Capacity: 10 GB", "Capacity: 20 GB"),
            ("Contract terms: old", "Contract terms: new"),
            ("Available now", "Out of stock"),
            ("Eligibility: all", "Eligibility: members"),
        )
        for before, after in important_cases:
            with self.subTest(before=before):
                result = compare_content(before, after)
                assert result.result == "candidate_material"
                assert result.should_summarize

    def test_watch_focus_overrides_a_minor_verdict_outside_fixed_patterns(
        self,
    ) -> None:
        # A small change that matches none of the five fixed signal patterns
        # (price/spec/terms/availability/eligibility) is classified "minor"
        # by default. A target explicitly focused on this kind of change
        # (e.g. tracking executive changes) must still get it summarized
        # rather than silently advancing the baseline.
        """Test that watch focus overrides a minor verdict outside fixed patterns."""
        before, after = "# CEO\nAlice", "# CEO\nBob"
        unfocused = compare_content(before, after)
        assert unfocused.result == "minor"
        assert not unfocused.should_summarize

        focused = compare_content(before, after, watch_focus="executive changes")
        assert focused.result == "candidate_material"
        assert focused.should_summarize
        assert "watch_focus_configured" in focused.scoring_reasons

        # A change clamped as pure noise (e.g. a bare "last updated" date)
        # must not be forced to candidate_material just because a focus is
        # configured -- that would defeat the noise clamp entirely.
        noise = compare_content(
            "Updated: 2026-07-30",
            "Updated: 2026-07-31",
            watch_focus="executive changes",
        )
        assert noise.result == "minor"

    def test_watch_focus_rescues_labeled_bare_numeric_noise_clamp(self) -> None:
        # A standalone numeric/date value with no label match against the
        # five fixed patterns is clamped as noise_only. If the target's
        # watch_focus names the very label the value is under, that must
        # still reach the summary model rather than being silently
        # discarded by the generic noise clamp.
        """Test that watch focus rescues labeled bare numeric noise clamp."""
        before, after = "# Valuation\n10", "# Valuation\n20"
        unfocused = compare_content(before, after)
        assert unfocused.result == "minor"
        assert "noise_only" in unfocused.scoring_reasons

        focused = compare_content(before, after, watch_focus="valuation")
        assert focused.result == "candidate_material"
        assert focused.should_summarize
        assert "watch_focus_configured" in focused.scoring_reasons

        # A focus that has nothing to do with this label must not rescue
        # it -- the noise clamp still applies when the focus doesn't match.
        unrelated_focus = compare_content(
            before, after, watch_focus="executive changes"
        )
        assert unrelated_focus.result == "minor"

    def test_watch_focus_matches_short_cjk_terms(self) -> None:
        # A len(term) > 2 filter drops common two-character Japanese
        # focuses, and \b is not a reliable tokenizer for CJK text (there is
        # no whitespace between words). A bare numeric label/value change
        # under a matching CJK focus must still reach the summary model
        # instead of being silently clamped as noise.
        """Test that watch focus matches short cjk terms."""
        before, after = "# 株価\n100", "# 株価\n101"
        unfocused = compare_content(before, after)
        assert unfocused.result == "minor"
        assert "noise_only" in unfocused.scoring_reasons

        focused = compare_content(before, after, watch_focus="株価")
        assert focused.result == "candidate_material"
        assert focused.should_summarize
        assert "watch_focus_configured" in focused.scoring_reasons

        unrelated_focus = compare_content(before, after, watch_focus="為替")
        assert unrelated_focus.result == "minor"

    def test_watch_focus_matches_short_uppercase_acronym(self) -> None:
        # A len(term) > 2 filter also drops common two-letter Latin
        # acronyms such as "AI"; those are deliberate uppercase tokens, not
        # accidental word fragments, so they must still reach the summary
        # model under a matching watch_focus instead of being clamped as
        # noise.
        """Test that watch focus matches short uppercase acronym."""
        before, after = "# AI\n100", "# AI\n101"
        unfocused = compare_content(before, after)
        assert unfocused.result == "minor"
        assert "noise_only" in unfocused.scoring_reasons

        focused = compare_content(before, after, watch_focus="AI")
        assert focused.result == "candidate_material"
        assert focused.should_summarize
        assert "watch_focus_configured" in focused.scoring_reasons

        lowercase_focus = compare_content(before, after, watch_focus="ai")
        assert lowercase_focus.result == "minor"

        unrelated_focus = compare_content(before, after, watch_focus="HR")
        assert unrelated_focus.result == "minor"

    def test_label_value_split_across_lines_is_still_material(self) -> None:
        # A label and its value are often on separate lines (a heading
        # anchor plus a bare value line below it), so only the value line
        # itself is among the changed lines. The label word never appears
        # there, and a bare numeric value alone would otherwise be clamped
        # as noise -- both must be covered via the section anchor.
        """Test that label value split across lines is still material."""
        price = compare_content("# Price\n10", "# Price\n20")
        assert price.result == "candidate_material"
        assert "price" in price.scoring_reasons
        assert "noise_only" not in price.scoring_reasons
        deadline = compare_content("# Deadline\n2026-01-01", "# Deadline\n2026-02-01")
        assert deadline.result == "candidate_material"
        assert "eligibility" in deadline.scoring_reasons
        japanese_price = compare_content("# 価格\n1000", "# 価格\n2000")
        assert japanese_price.result == "candidate_material"
        assert "price" in japanese_price.scoring_reasons

    def test_large_rewrite_and_bounded_output(self) -> None:
        """Test that large rewrite and bounded output."""
        before = "\n".join(f"old line {index}" for index in range(500))
        after = "\n".join(f"new line {index}" for index in range(500))
        result = compare_content(
            before,
            after,
            config=DiffConfig(max_diff_chars=1_000, max_sections=2),
        )
        assert result.result == "candidate_material"
        assert result.truncated
        rendered = str(result.as_dict())
        assert len(rendered) < 3000

    def test_oversized_input_short_circuits_instead_of_quadratic_diffing(self) -> None:
        """Test that oversized input short circuits instead of quadratic diffing."""
        before = "\n".join(["shared line"] * 40_000)
        after = "\n".join(["shared line"] * 39_999 + ["changed line"])
        started = time.monotonic()
        result = compare_content(
            before, after, config=DiffConfig(max_diff_lines=20_000)
        )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert result.result == "candidate_material"
        assert result.should_summarize
        assert result.truncated
        assert "diff_budget_exceeded" in result.scoring_reasons
        assert result.budget_exceeded
        assert result.as_dict()["budget_exceeded"]
        assert len(result.sections) == 1

    def test_budget_exceeded_is_false_for_ordinary_bounded_truncation(self) -> None:
        """Test that budget exceeded is false for ordinary bounded truncation."""
        before = "\n".join(f"old line {index}" for index in range(500))
        after = "\n".join(f"new line {index}" for index in range(500))
        result = compare_content(
            before,
            after,
            config=DiffConfig(max_diff_chars=1_000, max_sections=2),
        )
        assert result.truncated
        assert not result.budget_exceeded

    def test_repeated_lines_below_the_line_cap_still_short_circuit(self) -> None:
        # Below max_diff_lines, so the existing line-count guard cannot fire;
        # only the complexity budget can stop the O(n^2) SequenceMatcher pass.
        """Test that repeated lines below the line cap still short circuit."""
        before = "\n".join(["shared line"] * 19_999)
        after = "\n".join(["shared line"] * 19_998 + ["changed line"])
        started = time.monotonic()
        result = compare_content(
            before, after, config=DiffConfig(max_diff_lines=20_000)
        )
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert result.budget_exceeded
        assert result.truncated

    def test_multi_value_repetition_also_bounded_by_complexity_budget(self) -> None:
        # A different adversarial shape than the single-value case above: ten
        # distinct values repeated evenly, at the line-count cap boundary.
        """Test that multi value repetition also bounded by complexity budget."""
        before_lines = [f"value {index % 10}" for index in range(20_000)]
        after_lines = list(before_lines)
        after_lines[10_000] = "changed line"
        started = time.monotonic()
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_diff_lines=20_000),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert result.budget_exceeded

    def test_unique_line_permutation_is_bounded_before_sequence_matcher(self) -> None:
        # Unique lines defeat frequency-based complexity estimates even though
        # SequenceMatcher can still take quadratic time on this permutation.
        """Test that unique line permutation is bounded before sequence matcher."""
        before_lines = [f"unique line {index}" for index in range(20_000)]
        after_lines = before_lines[::2] + before_lines[1::2]
        started = time.monotonic()
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_diff_lines=20_000),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert result.budget_exceeded
        assert result.truncated

    def test_ordinary_repetition_stays_under_the_complexity_budget(self) -> None:
        """Test that ordinary repetition stays under the complexity budget."""
        before_lines = (
            ["separator"] * 500 + ["Price: $10"] + [f"row {i}" for i in range(500)]
        )
        after_lines = (
            ["separator"] * 500 + ["Price: $20"] + [f"row {i}" for i in range(500)]
        )
        result = compare_content("\n".join(before_lines), "\n".join(after_lines))
        assert not result.budget_exceeded
        assert "price" in result.scoring_reasons

    def test_signal_bearing_section_survives_section_count_truncation(self) -> None:
        """Test that signal bearing section survives section count truncation."""
        before_lines: list[str] = []
        after_lines: list[str] = []
        for index in range(40):
            before_lines.append(f"anchor {index}")
            after_lines.append(f"anchor {index}")
            if index == 35:
                before_lines.append("Price: $10")
                after_lines.append("Price: $20")
            else:
                before_lines.append(f"note {index} original")
                after_lines.append(f"note {index} changed")
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_sections=30),
        )
        assert result.truncated
        assert not result.signal_section_truncated
        assert any("Price: $20" in section.after for section in result.sections)

    def test_signal_section_truncated_when_not_all_signals_fit(self) -> None:
        """Test that signal section truncated when not all signals fit."""
        before_lines = []
        after_lines = []
        for index in range(5):
            before_lines.append(f"anchor {index}")
            after_lines.append(f"anchor {index}")
            before_lines.append(f"Price: ${100 + index}")
            after_lines.append(f"Price: ${200 + index}")
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_sections=2),
        )
        assert result.truncated
        assert result.signal_section_truncated

    def test_signal_section_truncated_when_tail_of_retained_section_is_cut(
        self,
    ) -> None:
        # One oversized replace hunk (every line differs, so difflib emits a
        # single contiguous section) whose only price signal sits in the
        # last line. A tight char budget retains the section's ID (its
        # leading lines fit) but must cut off before that last line, so the
        # section is present yet incomplete -- the section-ID-only check
        # this guards against would miss that the retained content is
        # missing the very evidence that made the section signal-bearing.
        """Test that signal section truncated when tail of retained section is cut."""
        before_lines = [f"row {index} original text" for index in range(150)]
        after_lines = [f"row {index} changed text" for index in range(150)]
        after_lines[-1] = "row 149 changed text Price: $999"
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_diff_chars=1_500, max_sections=30),
        )
        assert result.truncated
        assert result.signal_section_truncated
        for section in result.sections:
            assert "Price: $999" not in "\n".join(section.after)

    def test_sections_contain_only_changed_lines_plus_separate_context(self) -> None:
        """Test that sections contain only changed lines plus separate context."""
        result = compare_content(
            "# Product\nPrice $10\nAvailable",
            "# Product\nPrice $20\nAvailable",
        )
        section = result.sections[0]
        assert section.before == ("Price $10",)
        assert section.after == ("Price $20",)
        assert "# Product" in section.context
        assert len(section.section_id) == 16


class DiffCliTest(unittest.TestCase):
    """Tests for diff.py's `_main` CLI entry point's stdout/stderr contract."""

    def test_usage_error_writes_to_stderr(self) -> None:
        """Test that incorrect argc writes a usage message to stderr and returns 2."""
        with patch("sys.stderr", new_callable=StringIO) as stderr:
            code = diff_main(["diff.py", "one-arg"])
        assert code == 2
        assert "usage" in stderr.getvalue()

    def test_success_writes_json_diff_to_stdout(self) -> None:
        """Test that a successful diff writes the JSON DiffResult to stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path(tmp) / "previous.txt"
            current = Path(tmp) / "current.txt"
            previous.write_text("Price $10", encoding="utf-8")
            current.write_text("Price $20", encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = diff_main(["diff.py", str(previous), str(current)])
        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["result"] in {"minor", "material", "candidate_material"}

    def test_read_failure_writes_error_json_to_stdout(self) -> None:
        """Test that a missing input file writes {"error": ...} JSON to stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.txt"
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = diff_main(["diff.py", str(missing), str(missing)])
        assert code == 1
        payload = json.loads(stdout.getvalue())
        assert payload["error"]["code"] == "input_read_failed"


class NormalizeCliTest(unittest.TestCase):
    """Tests for normalize.py's `_main` CLI entry point's stdout/stderr contract."""

    def test_usage_error_writes_to_stderr(self) -> None:
        """Test that incorrect argc writes a usage message to stderr and returns 2."""
        with patch("sys.stderr", new_callable=StringIO) as stderr:
            code = normalize_main(["normalize.py"])
        assert code == 2
        assert "usage" in stderr.getvalue()

    def test_success_writes_json_result_to_stdout(self) -> None:
        """Test that a successful normalize writes the JSON result to stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.html"
            source.write_text(
                "<html><body><p>Hello</p></body></html>", encoding="utf-8"
            )
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = normalize_main(["normalize.py", str(source), "text/html"])
        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["kind"] == "html"
        assert "Hello" in payload["text"]

    def test_read_failure_writes_error_json_to_stdout(self) -> None:
        """Test that a missing input file writes {"error": ...} JSON to stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.html"
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                code = normalize_main(["normalize.py", str(missing)])
        assert code == 1
        payload = json.loads(stdout.getvalue())
        assert payload["error"]["code"] == "input_read_failed"


if __name__ == "__main__":
    unittest.main()
