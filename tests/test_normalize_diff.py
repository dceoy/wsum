from __future__ import annotations

import base64
import builtins
import codecs
import time
import unittest
import zlib
from io import BytesIO
from unittest.mock import patch

import support  # noqa: F401
from diff import DiffConfig, compare_content
from errors import MonitorError
from feed_normalizer import normalize_feed
from normalize import normalize_content
from pdf_normalizer import _stream_filters, extract_pdf_text
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
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
    def test_formatting_noise_produces_same_hash(self) -> None:
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
        self.assertEqual(normalized_first.text, normalized_second.text)
        self.assertEqual(
            normalized_first.normalized_hash, normalized_second.normalized_hash
        )
        self.assertEqual("2026-01", normalized_first.normalization_version)

    def test_form_content_inside_main_is_preserved_but_outside_is_dropped(
        self,
    ) -> None:
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
        self.assertIn("2026-08-31", normalized_before.text)
        self.assertIn("2026-09-30", normalized_after.text)
        self.assertNotEqual(
            normalized_before.normalized_hash, normalized_after.normalized_hash
        )
        bom_feed = (
            b"\xef\xbb\xbf<!--synthetic--><rss><channel>"
            b"<item><guid>1</guid><title>One</title></item>"
            b"</channel></rss>"
        )
        self.assertEqual(
            "feed",
            normalize_content(bom_feed, content_type="application/rss+xml").kind,
        )
        plain = normalize_content("ＡＢＣ".encode(), content_type="text/plain")
        self.assertEqual("ABC", plain.text)

    def test_article_header_title_change_is_not_silently_missed(self) -> None:
        # A <header> nested inside <article>/<section> is a content
        # sub-heading, not page chrome, so a change confined to it must
        # still change the normalized hash.
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("New Title", after.text)
        # A page-level header outside any content container remains
        # boilerplate and is still stripped.
        page_header = normalize_content(
            b"<html><body><header>Site Nav</header>"
            b"<main><p>Body text.</p></main></body></html>",
            content_type="text/html",
        )
        self.assertNotIn("Site Nav", page_header.text)

    def test_classed_article_header_title_change_is_not_silently_missed(
        self,
    ) -> None:
        # A "header" class/id token is generic boilerplate noise (e.g.
        # "site-header"), but a <header class="article-header"> nested in
        # an <article> is the same content sub-heading as a bare <header>
        # nested there -- the class token must not re-drop it.
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("New status", after.text)
        # A page-level "site-header" class remains boilerplate and is
        # still stripped.
        page_header = normalize_content(
            b'<html><body><header class="site-header">Site Nav</header>'
            b"<main><p>Body text.</p></main></body></html>",
            content_type="text/html",
        )
        self.assertNotIn("Site Nav", page_header.text)

    def test_share_price_business_content_is_not_dropped_as_noise(self) -> None:
        # NOISE_TOKEN_RE used to treat any class/id token containing "share"
        # as boilerplate, so a business widget like class="share-price"
        # (and BEM-style compounds such as "product-share-price") was
        # dropped wholesale -- a value-only change then left the normalized
        # text and hash unchanged.
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("105", after.text)
        # A genuine social-share widget remains boilerplate and is still
        # stripped.
        share_widget = normalize_content(
            b'<html><body><main><p>Body text.</p>'
            b'<div class="social-share">Share this</div>'
            b"</main></body></html>",
            content_type="text/html",
        )
        self.assertNotIn("Share this", share_widget.text)

    def test_http_charset_is_used_before_bom_or_body_sniffing(self) -> None:
        body = "価格改定のお知らせ".encode("shift_jis")
        # Without the declared charset, the shift_jis bytes are not valid
        # UTF-8 and must fail closed rather than silently decode as garbage
        # replacement text (which could mask a real change).
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(body, content_type="text/plain")
        with_charset = normalize_content(
            body, content_type="text/plain", charset="shift_jis"
        )
        self.assertEqual("価格改定のお知らせ", with_charset.text)

    def test_unusable_declared_charset_falls_back_to_bom_instead_of_failing(
        self,
    ) -> None:
        # A server-declared charset we do not allow-list (or mislabel) must
        # not turn a previously-decodable BOM'd body into a hard failure.
        body = codecs.BOM_UTF8 + b"Notice"
        result = normalize_content(body, content_type="text/plain", charset="utf-16")
        self.assertEqual("Notice", result.text)

    def test_unsupported_declared_charset_fails_closed_without_rescue(
        self,
    ) -> None:
        # A declared charset that resolves to a real (but non-allow-listed)
        # codec, such as iso-2022-jp, must not silently decode as UTF-8
        # replacement garbage when there is no BOM or in-body declaration
        # to rescue it -- that would make distinct legacy-encoded responses
        # normalize incorrectly or identically, masking real changes.
        body = "価格改定のお知らせ".encode("iso-2022-jp")
        with self.assertRaisesRegex(MonitorError, "unsupported"):
            normalize_content(body, content_type="text/plain", charset="iso-2022-jp")

    def test_unsupported_declared_charset_still_rescued_by_in_body_declaration(
        self,
    ) -> None:
        # Even when the declared charset can't be used, an in-body charset
        # declaration must still get a chance to rescue the decode, same as
        # the BOM path above.
        body = "charset=shift_jis 価格改定のお知らせ".encode("shift_jis")
        result = normalize_content(
            body, content_type="text/plain", charset="iso-2022-jp"
        )
        self.assertIn("価格改定のお知らせ", result.text)

    def test_malformed_bytes_fail_closed_instead_of_collapsing_to_replacement(
        self,
    ) -> None:
        # Two distinct invalid UTF-8 byte sequences both decode under
        # errors="replace" to the same U+FFFD-filled text ("Price: �10"),
        # which would make genuinely different responses hash identically
        # and silently mask a real change. Decoding must fail closed on
        # both instead of quietly treating them as equivalent.
        first = b"Price: \xff10"
        second = b"Price: \xfe10"
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(first, content_type="text/plain")
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(second, content_type="text/plain")

    def test_meaningful_price_specification_and_terms_change_hash(self) -> None:
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
        self.assertEqual(4, len(hashes))

    def test_link_destination_change_is_not_silently_missed(self) -> None:
        # Same visible link text, changed href: without a destination
        # annotation this produces identical normalized text/hash and the
        # routine never diffs or notifies on an application/pricing/
        # checkout link change.
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("https://example.com/apply-v1", before.text)
        self.assertIn("https://example.com/apply-v2", after.text)

    def test_standalone_link_destination_change_is_not_silently_missed(self) -> None:
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("https://example.com/apply-v1", before.text)
        self.assertIn("https://example.com/apply-v2", after.text)

    def test_document_base_change_is_not_silently_missed(self) -> None:
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("https://example.com/v1/apply", before.text)
        self.assertIn("https://example.com/v2/apply", after.text)

    def test_unsafe_document_base_fails_closed(self) -> None:
        with self.assertRaisesRegex(MonitorError, "HTTP and HTTPS"):
            normalize_content(
                b'<base href="javascript:alert(1)"><main>Content</main>',
                content_type="text/html",
                base_url="https://example.com/page",
            )

    def test_first_declared_document_base_wins_even_when_empty(self) -> None:
        result = normalize_content(
            (
                b'<base href=""><base href="/ignored/">'
                b'<main><a href="apply">Apply</a></main>'
            ),
            content_type="text/html",
            base_url="https://example.com/current/page",
        )
        self.assertIn("https://example.com/current/apply", result.text)
        self.assertNotIn("https://example.com/ignored/apply", result.text)

    def test_long_link_destination_keeps_full_identity_in_digest(self) -> None:
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("sha256:", before.text)
        self.assertIn("sha256:", after.text)

    def test_link_destination_budget_fails_closed(self) -> None:
        links = "".join(
            f'<p><a href="/item/{index}">Item</a></p>' for index in range(501)
        )
        with self.assertRaisesRegex(MonitorError, "too many link destinations"):
            normalize_content(
                f"<main>{links}</main>".encode(),
                content_type="text/html",
                base_url="https://example.com/page",
            )

    def test_form_action_change_is_not_silently_missed(self) -> None:
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
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_credential_bearing_link_destination_fails_closed(self) -> None:
        # Rejected HTTP(S) destinations must not be omitted or represented by
        # an offline-testable unsalted digest: both choices can expose or miss
        # credential-only changes. The policy error itself is content-safe.
        cases = (
            ("https://user:pass@example.com/x", "user:pass"),
            ("https://example.com/x?token=secret", "secret"),
        )
        for destination, secret in cases:
            with self.subTest(destination=destination):
                source = f'<main><a href="{destination}">Link</a></main>'.encode()
                with self.assertRaises(MonitorError) as captured:
                    normalize_content(
                        source,
                        content_type="text/html",
                        base_url="https://example.com/page",
                    )
                self.assertNotIn(secret, str(captured.exception))

    def test_in_page_and_relative_links_without_base_url_are_not_annotated(
        self,
    ) -> None:
        same_fragment_only = normalize_content(
            b'<main><p><a href="#section">Jump</a></p></main>',
            content_type="text/html",
            base_url="https://example.com/page",
        )
        self.assertNotIn("example.com", same_fragment_only.text)
        no_base = normalize_content(
            b'<main><p><a href="/apply-v1">Apply</a></p></main>',
            content_type="text/html",
        )
        self.assertNotIn("/apply-v1", no_base.text)

    def test_standalone_deadline_date_is_preserved_not_erased(self) -> None:
        # A date/time-only line is stripped as routine-timestamp noise, but
        # that must not erase a standalone date that is itself the monitored
        # business data (e.g. an application deadline with no "updated"/
        # "modified"/"published" label). Only explicitly labelled timestamp
        # lines should be treated as noise.
        before = normalize_content(
            b"<main><h2>Application deadline</h2><p>2026-08-31</p></main>",
            content_type="text/html",
        )
        after = normalize_content(
            b"<main><h2>Application deadline</h2><p>2026-09-30</p></main>",
            content_type="text/html",
        )
        self.assertIn("2026-08-31", before.text)
        self.assertIn("2026-09-30", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_direct_text_before_and_after_a_block_child_is_not_dropped(self) -> None:
        # An element that mixes its own direct text with a block-level
        # child (e.g. a status line followed by a details <div>) must not
        # silently lose that text from the normalized hash.
        open_before = normalize_content(
            b"<main>Applications are now open<div>Details</div></main>",
            content_type="text/html",
        )
        closed_before = normalize_content(
            b"<main>Applications are now closed<div>Details</div></main>",
            content_type="text/html",
        )
        self.assertNotEqual(open_before.normalized_hash, closed_before.normalized_hash)
        self.assertIn("Applications are now closed", closed_before.text)
        self.assertIn("Details", closed_before.text)

        open_after = normalize_content(
            b"<main><div>Details</div>Applications are now open</main>",
            content_type="text/html",
        )
        closed_after = normalize_content(
            b"<main><div>Details</div>Applications are now closed</main>",
            content_type="text/html",
        )
        self.assertNotEqual(open_after.normalized_hash, closed_after.normalized_hash)
        self.assertIn("Applications are now closed", closed_after.text)
        self.assertIn("Details", closed_after.text)

    def test_selectors_tables_ads_and_drift(self) -> None:
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
        self.assertIn("Plan | Price", result.text)
        self.assertIn("Pro | $20", result.text)
        self.assertNotIn("Buy now", result.text)
        with self.assertRaisesRegex(MonitorError, "matched no"):
            normalize_content(
                body,
                content_type="text/html",
                include_selector=".missing",
            )
        with self.assertRaisesRegex(MonitorError, "matched no"):
            normalize_content(
                body,
                content_type="text/html",
                exclude_selectors=(".missing",),
            )
        with self.assertRaisesRegex(MonitorError, "unsupported"):
            normalize_content(
                body,
                content_type="text/html",
                include_selector="main > table",
            )

    def test_rss_and_atom_order_are_stable(self) -> None:
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
        self.assertEqual(first.normalized_hash, second.normalized_hash)
        updated = normalize_content(
            rss_two.replace(b"Second", b"Second updated"),
            content_type="application/rss+xml",
        )
        self.assertNotEqual(first.normalized_hash, updated.normalized_hash)
        self.assertIn("ENTRY 1", first.text)
        atom = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example,1</id><title>Atom item</title>
            <link href="https://example.com/atom/1"/>
            <updated>2026-01-01T00:00:00Z</updated>
          </entry>
        </feed>"""
        atom_result = normalize_content(atom, content_type="application/atom+xml")
        self.assertEqual("atom", atom_result.metadata["feed_kind"])
        self.assertIn("ENTRY tag:example,1", atom_result.text)
        removed = normalize_content(
            b"""<rss><channel>
              <item><guid>1</guid><title>First</title></item>
            </channel></rss>""",
            content_type="application/rss+xml",
        )
        feed_diff = compare_content(first.text, removed.text)
        self.assertTrue(
            any(section.kind == "removed" for section in feed_diff.sections)
        )
        self.assertTrue(
            any(section.anchor.startswith("ENTRY 2") for section in feed_diff.sections)
        )

    def test_long_feed_entry_ids_keep_full_identity_with_bounded_output(self) -> None:
        # A bare ``stable_id[:1_000]`` makes IDs that differ only after that
        # boundary indistinguishable. Both RSS GUIDs and Atom IDs are entry
        # anchors, so preserve the full identity in a digest while retaining
        # the existing output-size bound.
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

                self.assertNotEqual(before.normalized_hash, after.normalized_hash)
                self.assertNotEqual(before_entry_id, after_entry_id)
                self.assertLessEqual(len(before_entry_id), 1_000)
                self.assertLessEqual(len(after_entry_id), 1_000)
                self.assertIn("[sha256:", before_entry_id)
                self.assertIn("[sha256:", after_entry_id)

    def test_rss_content_encoded_body_is_captured(self) -> None:
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
        self.assertIn("CONTENT Full article body", text)

    def test_rss_content_encoded_survives_alongside_description(self) -> None:
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
        self.assertIn("Stable teaser", first)
        self.assertIn("Original full article body", first)
        self.assertNotEqual(first, second)

    def test_feed_content_destination_only_change_is_not_silently_missed(
        self,
    ) -> None:
        # The feed HTML cleaner used to record only text nodes, so a common
        # RSS body like <a href="...">Apply</a> normalized to "Apply"
        # regardless of the href -- a stable guid/title/link plus a
        # destination-only href change left the entry hash unchanged.
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
        self.assertNotEqual(before, after)
        self.assertIn("Apply [https://example.com/apply-v1", before)

    def test_feed_content_relative_link_without_entry_link_fails_closed(
        self,
    ) -> None:
        # An item with a stable guid/title but no entry <link> used to reach
        # _content_link_destination with an empty base URL: a relative href
        # like /apply-v1 was silently discarded, so bumping it to /apply-v2
        # left the entry hash unchanged and the destination update invisible.
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

        with self.assertRaisesRegex(MonitorError, "feed_content_relative_link"):
            normalize_feed(render("/apply-v1"))

    def test_feed_content_relative_link_resolves_against_channel_link(
        self,
    ) -> None:
        # When an item omits its own <link> but the channel declares one,
        # relative content-link hrefs should resolve against that inherited
        # feed-level base instead of failing closed.
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
        self.assertNotEqual(before, after)
        self.assertIn("Apply [https://example.com/jobs/apply-v1", before)

    def test_atom_xhtml_content_destination_only_change_is_not_missed(
        self,
    ) -> None:
        # Atom permits inline markup as real XML children (<content
        # type="xhtml"><div><a href="...">...</a></div></content>), not
        # just HTML escaped into text -- ElementTree's itertext() drops
        # attributes, so a real <a> element's href needs its own structural
        # walk rather than relying on the escaped-text HTML parser.
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
        self.assertNotEqual(before, after)
        self.assertIn("Apply [https://example.com/apply-v1", before)

    def test_content_link_budget_covers_an_ordinary_large_feed(self) -> None:
        # The link-destination annotation budget is shared across the whole
        # feed (the feed is normalized into one stored/hashed/diffed blob,
        # so the aggregate output is what must stay bounded), but it must
        # still be sized generously enough that an ordinary large feed with
        # one link per entry, up to the default max_entries, never trips it.
        entries = "".join(
            f"<item><guid>{i}</guid><title>Post {i}</title>"
            f'<description>&lt;a href="https://example.com/{i}"&gt;'
            "Link&lt;/a&gt;</description></item>"
            for i in range(1_000)
        )
        xml = f"<rss><channel>{entries}</channel></rss>".encode()
        text, metadata = normalize_feed(xml)
        self.assertEqual("1000", metadata["entry_count"])
        self.assertIn("https://example.com/0", text)
        self.assertIn("https://example.com/999", text)

    def test_content_link_budget_fails_closed_when_exhausted(self) -> None:
        # A feed with far more embedded link destinations than any
        # legitimate feed would carry must fail closed instead of producing
        # unbounded normalized output.
        links = "".join(
            f'&lt;a href="https://example.com/{i}"&gt;Link{i}&lt;/a&gt; '
            for i in range(6_000)
        )
        xml = (
            "<rss><channel><item><guid>1</guid><title>Post</title>"
            f"<description>{links}</description></item></channel></rss>"
        ).encode()
        with self.assertRaisesRegex(MonitorError, "feed_too_large"):
            normalize_feed(xml)

    def test_atom_prefers_alternate_link_over_self(self) -> None:
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
        self.assertIn("LINK https://example.com/articles/old", before)
        self.assertNotIn("https://example.com/feed/entry/1", before)
        self.assertNotEqual(before, after)

    def test_atom_external_content_source_is_canonicalized_and_captured(self) -> None:
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
        self.assertIn("CONTENT_SRC https://example.com/external/v1", before)
        self.assertNotEqual(before, after)

    def test_atom_relative_external_content_source_is_rejected(self) -> None:
        relative = b"""<feed xmlns="http://www.w3.org/2005/Atom"
            xml:base="https://example.com/">
          <entry><id>tag:example,1</id><content src="external/v1"/></entry>
        </feed>"""
        with self.assertRaisesRegex(MonitorError, "absolute HTTP"):
            normalize_feed(relative)

        credential_source = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example,1</id>
            <content src="https://example.com/external?token=secret"/>
          </entry>
        </feed>"""
        with self.assertRaisesRegex(MonitorError, "unsafe external content"):
            normalize_feed(credential_source)

    def test_feed_relative_entry_link_is_explicitly_rejected(self) -> None:
        relative = b"""<feed xmlns="http://www.w3.org/2005/Atom"
            xml:base="https://example.com/">
          <entry><id>tag:example,1</id><link rel="alternate" href="post/1"/></entry>
        </feed>"""
        with self.assertRaisesRegex(MonitorError, "absolute HTTP"):
            normalize_feed(relative)

    def test_feed_rejects_entities_malformed_and_type_mismatch(self) -> None:
        with self.assertRaisesRegex(MonitorError, "DOCTYPE"):
            normalize_content(
                b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "x">]><rss/>',
                content_type="application/rss+xml",
            )
        with self.assertRaises(MonitorError):
            normalize_content(
                b"<?xml version='1.0'?><rss><item>",
                content_type="application/rss+xml",
            )
        with self.assertRaisesRegex(MonitorError, "does not match"):
            normalize_content(b"%PDF-1.4\n%%EOF", content_type="text/html")
        utf16 = '<?xml version="1.0"?><rss><channel/></rss>'.encode("utf-16")
        with self.assertRaisesRegex(MonitorError, "UTF-16/32"):
            normalize_feed(utf16)
        unsafe_link = (
            b"<rss><channel><item><guid>1</guid>"
            b"<link>https://example.com/?to"
            b"ken=fixture</link></item></channel></rss>"
        )
        with self.assertRaisesRegex(MonitorError, "unsafe link"):
            normalize_content(unsafe_link, content_type="application/rss+xml")

    def test_html_wide_element_count_is_bounded(self) -> None:
        # The byte-size cap on the raw response does not bound the number of
        # parsed Node objects: a few megabytes of tiny tags can still exceed
        # html_normalizer.MAX_NODES and drive excessive memory use in the
        # tree walks. This must fail closed rather than parse unboundedly.
        wide = b"<html><body>" + b"<p>x</p>" * 60_000 + b"</body></html>"
        with self.assertRaisesRegex(MonitorError, "too many elements"):
            normalize_content(wide, content_type="text/html")

    def test_html_deep_nesting_is_bounded(self) -> None:
        # iter_nodes()/_text_content()/visit() all recurse to tree depth.
        # Nesting beyond html_normalizer.MAX_DEPTH must be rejected during
        # parsing instead of risking a RecursionError deep inside those
        # walks on untrusted HTML.
        deep = (
            b"<html><body>"
            + b"<div>" * 300
            + b"x"
            + b"</div>" * 300
            + b"</body></html>"
        )
        with self.assertRaisesRegex(MonitorError, "nesting is too deep"):
            normalize_content(deep, content_type="text/html")

    def test_text_pdf_and_pdf_failures(self) -> None:
        pdf = _text_pdf(b"(Hello PDF) Tj")
        result = normalize_content(pdf, content_type="application/pdf")
        self.assertEqual("pdf", result.kind)
        self.assertIn("Hello PDF", result.text)
        encrypted = b"%PDF-1.4\n1 0 obj << /Encrypt 2 0 R >> endobj\n%%EOF"
        with self.assertRaisesRegex(MonitorError, "encrypted"):
            normalize_content(encrypted, content_type="application/pdf")
        image_only = _image_only_pdf()
        with self.assertRaisesRegex(MonitorError, "image-only"):
            normalize_content(image_only, content_type="application/pdf")

    def test_pdf_parser_is_lazy_and_reports_a_missing_capability(self) -> None:
        # HTML-only callers must not need pypdf just because normalize.py
        # imports the PDF normalizer. A PDF request should instead return a
        # stable, actionable error when the optional runtime capability is
        # absent.
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
                raise ModuleNotFoundError(
                    f"No module named {name!r}", name=name
                )
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", new=reject_pypdf):
            html = normalize_content(
                b"<html><body><main>HTML remains available</main></body></html>",
                content_type="text/html",
            )
            self.assertIn("HTML remains available", html.text)
            with self.assertRaisesRegex(MonitorError, "pdf_parser_unavailable"):
                normalize_content(pdf, content_type="application/pdf")

    def test_generated_text_pdf_with_filtered_images_is_extracted(self) -> None:
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

        self.assertIn("Text with images", result.text)

    def test_pdf_image_classification_ignores_nested_and_string_markers(
        self,
    ) -> None:
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
                with self.assertRaisesRegex(MonitorError, "filter"):
                    normalize_content(pdf, content_type="application/pdf")

    def test_pdf_tj_array_hex_strings_are_not_silently_dropped(self) -> None:
        # TJ arrays commonly mix literal (...) runs with hex-encoded <...>
        # runs (e.g. a font/CMap-encoded value between literal label text).
        # Only extracting the literal runs would keep the normalized hash
        # stable even when the hex-encoded content changes.
        mixed = _text_pdf(b"[(Hello ) <576f726c64>] TJ")
        result = normalize_content(mixed, content_type="application/pdf")
        self.assertIn("Hello World", result.text)
        before = normalize_content(
            _text_pdf(b"[(Total: ) <30303030>] TJ"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"[(Total: ) <39393939>] TJ"),
            content_type="application/pdf",
        )
        self.assertIn("Total: 0000", before.text)
        self.assertIn("Total: 9999", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_pdf_quote_operators_are_not_silently_dropped(self) -> None:
        # ' (move to next line, show text) and " (set spacing, move to next
        # line, show text) are valid text-showing operators alongside Tj/TJ.
        # A parser that only understands Tj/TJ extracts the header but
        # silently drops content shown only via ' or ", so an edit confined
        # to that content would leave the normalized hash unchanged.
        before = normalize_content(
            _text_pdf(b"(Header) Tj (Old status) '"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(Header) Tj (New status) '"),
            content_type="application/pdf",
        )
        self.assertIn("Old status", before.text)
        self.assertIn("New status", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

        double_quote = normalize_content(
            _text_pdf(b'(Header) Tj 0 0 (Quoted status) "'),
            content_type="application/pdf",
        )
        self.assertIn("Quoted status", double_quote.text)

    def test_pdf_et_inside_a_string_operand_does_not_truncate_the_text_block(
        self,
    ) -> None:
        # A literal string operand that happens to contain "ET" (e.g. inside
        # "status ET old") must remain text rather than being mistaken for
        # the end-text operator and silently dropped from the hash.
        before = normalize_content(
            _text_pdf(b"(status ET old) Tj"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(status ET new) Tj"),
            content_type="application/pdf",
        )
        self.assertIn("status ET old", before.text)
        self.assertIn("status ET new", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_pdf_et_inside_a_name_token_does_not_truncate_the_text_block(
        self,
    ) -> None:
        # A marked-content tag like "/ETMarker" contains the literal bytes
        # "ET" outside of any string. It must not cause the real Tj call
        # that follows to be silently dropped.
        before = normalize_content(
            _text_pdf(b"/ETMarker BMC (Old status) Tj EMC"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"/ETMarker BMC (New status) Tj EMC"),
            content_type="application/pdf",
        )
        self.assertIn("Old status", before.text)
        self.assertIn("New status", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_pdf_nested_parens_in_a_string_are_not_silently_dropped(self) -> None:
        # A literal string can contain balanced, unescaped nested parens
        # (e.g. "(Old (status))"). A flat, non-recursive string regex only
        # matches up to the first unescaped ")", misaligning with the "Tj"
        # that follows and silently dropping the whole operand instead of
        # extracting "Old (status)".
        before = normalize_content(
            _text_pdf(b"(Stable) Tj (Old (status)) Tj"),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(b"(Stable) Tj (New (status)) Tj"),
            content_type="application/pdf",
        )
        self.assertIn("Old (status)", before.text)
        self.assertIn("New (status)", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_pdf_endstream_inside_a_string_operand_does_not_truncate_the_stream(
        self,
    ) -> None:
        # A parser that locates the stream body by scanning for the first
        # newline-delimited "endstream" bytes (rather than honoring the
        # dictionary's /Length) truncates here: this literal string operand
        # legally contains "\nendstream" as raw content, and everything
        # after it -- including a later, otherwise-stable Tj whose own text
        # changes -- would be silently dropped from the normalized hash.
        def stream(edit: bytes) -> bytes:
            return (
                b"(marker\nendstream inside a string) Tj "
                b"(Stable) Tj (" + edit + b") Tj"
            )

        before = normalize_content(
            _text_pdf(stream(b"Old edit")),
            content_type="application/pdf",
        )
        after = normalize_content(
            _text_pdf(stream(b"New edit")),
            content_type="application/pdf",
        )
        self.assertIn("Stable", before.text)
        self.assertIn("Old edit", before.text)
        self.assertIn("New edit", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

    def test_pdf_stream_keyword_inside_a_stream_body_is_not_rescanned(self) -> None:
        body = b"(marker\nstream\ninside a string) Tj"

        normalized = normalize_content(
            _text_pdf(body),
            content_type="application/pdf",
        )

        self.assertIn("stream", normalized.text)

    def test_pdf_unterminated_string_fails_closed(self) -> None:
        pdf = _pdf(_pdf_stream(1, b"BT (unterminated Tj ET"))
        with self.assertRaisesRegex(MonitorError, "pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_standard_generated_font_pdf_is_extracted(self) -> None:
        # Generated by ReportLab 4 with Helvetica and page compression off.
        # This is an ordinary, xref-bearing PDF with /BaseFont and
        # /WinAnsiEncoding rather than a synthetic font-less content stream.
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
        self.assertIn("Standard generated PDF", result.text)
        self.assertIn("Price: 42 USD", result.text)

    def test_pdf_indirect_length_reference_is_resolved(self) -> None:
        # /Length can be an indirect reference ("N G R") to a separate
        # object holding the bare integer, not just a direct integer.
        pdf = _text_pdf(b"(Indirect length) Tj", indirect_length=True)
        result = normalize_content(pdf, content_type="application/pdf")
        self.assertIn("Indirect length", result.text)

    def test_pdf_unresolvable_indirect_length_fails_closed(self) -> None:
        body = b"BT (Indirect length) Tj ET"
        pdf = _pdf(
            b"1 0 obj\n<< /Length 3 0 R >>\nstream\n" + body + b"\nendstream\nendobj\n"
        )
        with self.assertRaisesRegex(MonitorError, "pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_unsupported_filter_streams_are_rejected_not_skipped(self) -> None:
        # A stream without /FlateDecode is currently assumed to already be
        # decoded plain content. But /ASCIIHexDecode, /ASCII85Decode,
        # /LZWDecode, /RunLengthDecode, and filter chains are also valid and
        # leave their bytes filter-encoded, not plain text. If only the
        # unfiltered stream is scanned, a change confined to a filtered
        # stream would leave the normalized hash unchanged, so such filters
        # must be rejected rather than silently skipped.
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
        with self.assertRaisesRegex(MonitorError, "filter"):
            normalize_content(before, content_type="application/pdf")
        with self.assertRaisesRegex(MonitorError, "filter"):
            normalize_content(after, content_type="application/pdf")

    def test_pdf_filter_beyond_a_fixed_lookbehind_window_is_still_detected(
        self,
    ) -> None:
        # A stream dictionary can exceed a fixed-size lookbehind window. If
        # /Filter appears more than that window's width before the "stream"
        # keyword, a scan bounded by a fixed window would see no filter and
        # silently treat the still-encoded bytes as plain content instead of
        # rejecting the unsupported filter.
        padding = b"x" * 1_200
        pdf = _pdf(
            _pdf_stream(
                1,
                b"28546f74616c3a20303030302947",
                extra=b"/Filter /ASCIIHexDecode /Extra (" + padding + b")",
            )
        )
        with self.assertRaisesRegex(MonitorError, "filter"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_escaped_filter_key_cannot_bypass_decompression_bound(self) -> None:
        # A valid #xx name escape makes /Fil#74er semantically equivalent to
        # /Filter. It must be decoded before pypdf can inflate the stream.
        pdf = _escaped_filter_text_pdf(b"x" * 2_048)

        with self.assertRaises(MonitorError) as raised:
            extract_pdf_text(
                pdf,
                max_input_bytes=len(pdf),
                max_decompressed_bytes=100,
            )

        self.assertEqual("pdf_decompressed_too_large", raised.exception.code)

    def test_pdf_duplicate_filter_keys_fail_closed(self) -> None:
        compressed = zlib.compress(b"BT (bounded) Tj ET")
        pdf = _pdf(
            _pdf_stream(
                1,
                compressed,
                extra=b"/Filter /FlateDecode /Fil#74er /FlateDecode",
            )
        )

        with self.assertRaises(MonitorError) as raised:
            extract_pdf_text(pdf, max_input_bytes=len(pdf))

        self.assertEqual("pdf_malformed", raised.exception.code)

    def test_pdf_filter_name_value_is_not_misclassified_as_a_key(self) -> None:
        self.assertEqual(
            [],
            _stream_filters(b"<< /Length 1 /Marker /Filter >>"),
        )

    def test_pdf_stream_dictionary_nesting_is_bounded(self) -> None:
        dictionary = b"<< /Length 1 /Metadata " + b"[" * 101 + b"/Value" + b"]" * 101
        dictionary += b" >>"

        with self.assertRaises(MonitorError) as raised:
            _stream_filters(dictionary)

        self.assertEqual("pdf_malformed", raised.exception.code)

    def test_pdf_truncated_flate_stream_fails_closed(self) -> None:
        # zlib.decompressobj() commonly returns partial output for truncated
        # input without raising zlib.error, so a stream cut short mid-flush
        # could otherwise be accepted as valid text instead of rejected.
        compressed = zlib.compress(b"BT (Hello Flate PDF) Tj ET")
        truncated = compressed[:-4]
        pdf = _pdf(_pdf_stream(1, truncated, extra=b"/Filter /FlateDecode"))
        with self.assertRaisesRegex(MonitorError, "truncated|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_stream_without_an_enclosing_object_fails_closed(self) -> None:
        # A stream with no discoverable "N G obj" header before it has no
        # provable dictionary association; the filter cannot be trusted
        # either way, so this must reject rather than treat it as unfiltered
        # plain content.
        pdf = b"%PDF-1.4\nstream\nBT (Hello) Tj ET\nendstream\n%%EOF"
        with self.assertRaisesRegex(MonitorError, "pdf_malformed|malformed"):
            normalize_content(pdf, content_type="application/pdf")

    def test_malformed_font_pdfs_are_rejected_not_mishashed(self) -> None:
        # Font-bearing PDFs are routed through pypdf so character codes are
        # resolved through the active encoding/CMap. These deliberately
        # incomplete fixtures have no page tree/xref and must fail closed.
        to_unicode = (
            b"%PDF-1.4\n1 0 obj\n<< /ToUnicode 2 0 R >>\nendobj\n"
            b"3 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(to_unicode, content_type="application/pdf")

        differences = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding << /Differences [1 /A] >> >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(differences, content_type="application/pdf")

        named_encoding = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding /WinAnsiEncoding >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(named_encoding, content_type="application/pdf")

        # A PDF name may encode letters with #xx escapes. The prior raw-byte
        # gate did not recognize these names and could route this font-backed
        # stream to a Latin-1 operand decoder; every accepted PDF now uses
        # the font-aware parser instead.
        escaped_font_names = (
            b"%PDF-1.4\n1 0 obj\n"
            b"<< /Type /F#6fnt /Subtype /Type1 /Base#46ont /Symbol >>\n"
            b"endobj\n"
            + _pdf_stream(2, b"BT /F1 12 Tf <41> Tj ET")
            + b"%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
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
                with self.assertRaisesRegex(MonitorError, "malformed"):
                    normalize_content(built_in_encoding, content_type="application/pdf")

        # A composite (/Type0) font always routes character codes through a
        # CMap, regardless of /Encoding/ToUnicode presence.
        composite_font = (
            b"%PDF-1.4\n1 0 obj\n<< /Subtype /Type0 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(composite_font, content_type="application/pdf")

        # A compressed object stream can hide a font/Encoding dictionary,
        # so a raw fallback would not be safe even if no obvious font name
        # were present in the top-level bytes.
        object_stream = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /ObjStm /N 1 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "malformed"):
            normalize_content(object_stream, content_type="application/pdf")

    def test_xhtml_with_xml_declaration_is_detected_as_html(self) -> None:
        result = normalize_content(
            (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                b"<main><p>Application open</p></main></body></html>"
            ),
            content_type="application/xhtml+xml",
        )
        self.assertEqual("html", result.kind)
        self.assertIn("Application open", result.text)


class DiffTests(unittest.TestCase):
    def test_unchanged_and_first_fetch_short_circuit(self) -> None:
        baseline = compare_content(None, "hello")
        self.assertEqual("baseline_created", baseline.result)
        self.assertFalse(baseline.should_summarize)
        unchanged = compare_content("old", "new", previous_hash="a", current_hash="a")
        self.assertEqual("unchanged", unchanged.result)
        self.assertEqual((), unchanged.sections)

    def test_noise_is_minor_but_important_patterns_are_candidates(self) -> None:
        noise = compare_content("Updated: 2026-07-30", "Updated: 2026-07-31")
        self.assertEqual("minor", noise.result)
        self.assertLess(noise.change_score, 35)
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
                self.assertEqual("candidate_material", result.result)
                self.assertTrue(result.should_summarize)

    def test_watch_focus_overrides_a_minor_verdict_outside_fixed_patterns(
        self,
    ) -> None:
        # A small change that matches none of the five fixed signal patterns
        # (price/spec/terms/availability/eligibility) is classified "minor"
        # by default. A target explicitly focused on this kind of change
        # (e.g. tracking executive changes) must still get it summarized
        # rather than silently advancing the baseline.
        before, after = "# CEO\nAlice", "# CEO\nBob"
        unfocused = compare_content(before, after)
        self.assertEqual("minor", unfocused.result)
        self.assertFalse(unfocused.should_summarize)

        focused = compare_content(before, after, watch_focus="executive changes")
        self.assertEqual("candidate_material", focused.result)
        self.assertTrue(focused.should_summarize)
        self.assertIn("watch_focus_configured", focused.scoring_reasons)

        # A change clamped as pure noise (e.g. a bare "last updated" date)
        # must not be forced to candidate_material just because a focus is
        # configured -- that would defeat the noise clamp entirely.
        noise = compare_content(
            "Updated: 2026-07-30",
            "Updated: 2026-07-31",
            watch_focus="executive changes",
        )
        self.assertEqual("minor", noise.result)

    def test_watch_focus_rescues_labeled_bare_numeric_noise_clamp(self) -> None:
        # A standalone numeric/date value with no label match against the
        # five fixed patterns is clamped as noise_only. If the target's
        # watch_focus names the very label the value is under, that must
        # still reach the summary model rather than being silently
        # discarded by the generic noise clamp.
        before, after = "# Valuation\n10", "# Valuation\n20"
        unfocused = compare_content(before, after)
        self.assertEqual("minor", unfocused.result)
        self.assertIn("noise_only", unfocused.scoring_reasons)

        focused = compare_content(before, after, watch_focus="valuation")
        self.assertEqual("candidate_material", focused.result)
        self.assertTrue(focused.should_summarize)
        self.assertIn("watch_focus_configured", focused.scoring_reasons)

        # A focus that has nothing to do with this label must not rescue
        # it -- the noise clamp still applies when the focus doesn't match.
        unrelated_focus = compare_content(
            before, after, watch_focus="executive changes"
        )
        self.assertEqual("minor", unrelated_focus.result)

    def test_watch_focus_matches_short_cjk_terms(self) -> None:
        # A len(term) > 2 filter drops common two-character Japanese
        # focuses, and \b is not a reliable tokenizer for CJK text (there is
        # no whitespace between words). A bare numeric label/value change
        # under a matching CJK focus must still reach the summary model
        # instead of being silently clamped as noise.
        before, after = "# 株価\n100", "# 株価\n101"
        unfocused = compare_content(before, after)
        self.assertEqual("minor", unfocused.result)
        self.assertIn("noise_only", unfocused.scoring_reasons)

        focused = compare_content(before, after, watch_focus="株価")
        self.assertEqual("candidate_material", focused.result)
        self.assertTrue(focused.should_summarize)
        self.assertIn("watch_focus_configured", focused.scoring_reasons)

        unrelated_focus = compare_content(before, after, watch_focus="為替")
        self.assertEqual("minor", unrelated_focus.result)

    def test_watch_focus_matches_short_uppercase_acronym(self) -> None:
        # A len(term) > 2 filter also drops common two-letter Latin
        # acronyms such as "AI"; those are deliberate uppercase tokens, not
        # accidental word fragments, so they must still reach the summary
        # model under a matching watch_focus instead of being clamped as
        # noise.
        before, after = "# AI\n100", "# AI\n101"
        unfocused = compare_content(before, after)
        self.assertEqual("minor", unfocused.result)
        self.assertIn("noise_only", unfocused.scoring_reasons)

        focused = compare_content(before, after, watch_focus="AI")
        self.assertEqual("candidate_material", focused.result)
        self.assertTrue(focused.should_summarize)
        self.assertIn("watch_focus_configured", focused.scoring_reasons)

        lowercase_focus = compare_content(before, after, watch_focus="ai")
        self.assertEqual("minor", lowercase_focus.result)

        unrelated_focus = compare_content(before, after, watch_focus="HR")
        self.assertEqual("minor", unrelated_focus.result)

    def test_label_value_split_across_lines_is_still_material(self) -> None:
        # A label and its value are often on separate lines (a heading
        # anchor plus a bare value line below it), so only the value line
        # itself is among the changed lines. The label word never appears
        # there, and a bare numeric value alone would otherwise be clamped
        # as noise -- both must be covered via the section anchor.
        price = compare_content("# Price\n10", "# Price\n20")
        self.assertEqual("candidate_material", price.result)
        self.assertIn("price", price.scoring_reasons)
        self.assertNotIn("noise_only", price.scoring_reasons)
        deadline = compare_content("# Deadline\n2026-01-01", "# Deadline\n2026-02-01")
        self.assertEqual("candidate_material", deadline.result)
        self.assertIn("eligibility", deadline.scoring_reasons)
        japanese_price = compare_content("# 価格\n1000", "# 価格\n2000")
        self.assertEqual("candidate_material", japanese_price.result)
        self.assertIn("price", japanese_price.scoring_reasons)

    def test_large_rewrite_and_bounded_output(self) -> None:
        before = "\n".join(f"old line {index}" for index in range(500))
        after = "\n".join(f"new line {index}" for index in range(500))
        result = compare_content(
            before,
            after,
            config=DiffConfig(max_diff_chars=1_000, max_sections=2),
        )
        self.assertEqual("candidate_material", result.result)
        self.assertTrue(result.truncated)
        rendered = str(result.as_dict())
        self.assertLess(len(rendered), 3_000)

    def test_oversized_input_short_circuits_instead_of_quadratic_diffing(self) -> None:
        before = "\n".join(["shared line"] * 40_000)
        after = "\n".join(["shared line"] * 39_999 + ["changed line"])
        started = time.monotonic()
        result = compare_content(
            before, after, config=DiffConfig(max_diff_lines=20_000)
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual("candidate_material", result.result)
        self.assertTrue(result.should_summarize)
        self.assertTrue(result.truncated)
        self.assertIn("diff_budget_exceeded", result.scoring_reasons)
        self.assertTrue(result.budget_exceeded)
        self.assertTrue(result.as_dict()["budget_exceeded"])
        self.assertEqual(1, len(result.sections))

    def test_budget_exceeded_is_false_for_ordinary_bounded_truncation(self) -> None:
        before = "\n".join(f"old line {index}" for index in range(500))
        after = "\n".join(f"new line {index}" for index in range(500))
        result = compare_content(
            before,
            after,
            config=DiffConfig(max_diff_chars=1_000, max_sections=2),
        )
        self.assertTrue(result.truncated)
        self.assertFalse(result.budget_exceeded)

    def test_repeated_lines_below_the_line_cap_still_short_circuit(self) -> None:
        # Below max_diff_lines, so the existing line-count guard cannot fire;
        # only the complexity budget can stop the O(n^2) SequenceMatcher pass.
        before = "\n".join(["shared line"] * 19_999)
        after = "\n".join(["shared line"] * 19_998 + ["changed line"])
        started = time.monotonic()
        result = compare_content(
            before, after, config=DiffConfig(max_diff_lines=20_000)
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(result.budget_exceeded)
        self.assertTrue(result.truncated)

    def test_multi_value_repetition_also_bounded_by_complexity_budget(self) -> None:
        # A different adversarial shape than the single-value case above: ten
        # distinct values repeated evenly, at the line-count cap boundary.
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
        self.assertLess(elapsed, 1.0)
        self.assertTrue(result.budget_exceeded)

    def test_unique_line_permutation_is_bounded_before_sequence_matcher(self) -> None:
        # Unique lines defeat frequency-based complexity estimates even though
        # SequenceMatcher can still take quadratic time on this permutation.
        before_lines = [f"unique line {index}" for index in range(20_000)]
        after_lines = before_lines[::2] + before_lines[1::2]
        started = time.monotonic()
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_diff_lines=20_000),
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(result.budget_exceeded)
        self.assertTrue(result.truncated)

    def test_ordinary_repetition_stays_under_the_complexity_budget(self) -> None:
        before_lines = (
            ["separator"] * 500 + ["Price: $10"] + [f"row {i}" for i in range(500)]
        )
        after_lines = (
            ["separator"] * 500 + ["Price: $20"] + [f"row {i}" for i in range(500)]
        )
        result = compare_content("\n".join(before_lines), "\n".join(after_lines))
        self.assertFalse(result.budget_exceeded)
        self.assertIn("price", result.scoring_reasons)

    def test_signal_bearing_section_survives_section_count_truncation(self) -> None:
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
        self.assertTrue(result.truncated)
        self.assertFalse(result.signal_section_truncated)
        self.assertTrue(
            any("Price: $20" in section.after for section in result.sections)
        )

    def test_signal_section_truncated_when_not_all_signals_fit(self) -> None:
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
        self.assertTrue(result.truncated)
        self.assertTrue(result.signal_section_truncated)

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
        before_lines = [f"row {index} original text" for index in range(150)]
        after_lines = [f"row {index} changed text" for index in range(150)]
        after_lines[-1] = "row 149 changed text Price: $999"
        result = compare_content(
            "\n".join(before_lines),
            "\n".join(after_lines),
            config=DiffConfig(max_diff_chars=1_500, max_sections=30),
        )
        self.assertTrue(result.truncated)
        self.assertTrue(result.signal_section_truncated)
        for section in result.sections:
            self.assertNotIn("Price: $999", "\n".join(section.after))

    def test_sections_contain_only_changed_lines_plus_separate_context(self) -> None:
        result = compare_content(
            "# Product\nPrice $10\nAvailable",
            "# Product\nPrice $20\nAvailable",
        )
        section = result.sections[0]
        self.assertEqual(("Price $10",), section.before)
        self.assertEqual(("Price $20",), section.after)
        self.assertIn("# Product", section.context)
        self.assertEqual(16, len(section.section_id))


if __name__ == "__main__":
    unittest.main()
