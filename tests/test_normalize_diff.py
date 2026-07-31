from __future__ import annotations

import codecs
import time
import unittest
import zlib

import support  # noqa: F401
from diff import DiffConfig, compare_content
from errors import MonitorError
from feed_normalizer import normalize_feed
from normalize import normalize_content


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
            b"<html><body><article><header class=\"article-header\">"
            b"<h1>Old status</h1></header><p>Body text.</p></article>"
            b"</body></html>",
            content_type="text/html",
        )
        after = normalize_content(
            b"<html><body><article><header class=\"article-header\">"
            b"<h1>New status</h1></header><p>Body text.</p></article>"
            b"</body></html>",
            content_type="text/html",
        )
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)
        self.assertIn("New status", after.text)
        # A page-level "site-header" class remains boilerplate and is
        # still stripped.
        page_header = normalize_content(
            b"<html><body><header class=\"site-header\">Site Nav</header>"
            b"<main><p>Body text.</p></main></body></html>",
            content_type="text/html",
        )
        self.assertNotIn("Site Nav", page_header.text)

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
        body = codecs.BOM_UTF8 + "Notice".encode()
        result = normalize_content(
            body, content_type="text/plain", charset="utf-16"
        )
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
        self.assertNotEqual(
            open_before.normalized_hash, closed_before.normalized_hash
        )
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
        self.assertNotEqual(
            open_after.normalized_hash, closed_after.normalized_hash
        )
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

    def test_text_pdf_and_pdf_failures(self) -> None:
        pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Length 38 >>\nstream\n"
            b"BT (Hello PDF) Tj ET\nendstream\nendobj\n%%EOF"
        )
        result = normalize_content(pdf, content_type="application/pdf")
        self.assertEqual("pdf", result.kind)
        self.assertIn("Hello PDF", result.text)
        encrypted = b"%PDF-1.4\n1 0 obj << /Encrypt 2 0 R >> endobj\n%%EOF"
        with self.assertRaisesRegex(MonitorError, "encrypted"):
            normalize_content(encrypted, content_type="application/pdf")
        image_only = (
            b"%PDF-1.4\n1 0 obj << /Subtype /Image >>\n"
            b"stream\nabc\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "image-only"):
            normalize_content(image_only, content_type="application/pdf")

    def test_pdf_tj_array_hex_strings_are_not_silently_dropped(self) -> None:
        # TJ arrays commonly mix literal (...) runs with hex-encoded <...>
        # runs (e.g. a font/CMap-encoded value between literal label text).
        # Only extracting the literal runs would keep the normalized hash
        # stable even when the hex-encoded content changes.
        mixed = (
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b"BT [(Hello ) <576f726c64>] TJ ET\nendstream\nendobj\n%%EOF"
        )
        result = normalize_content(mixed, content_type="application/pdf")
        self.assertIn("Hello World", result.text)
        before = normalize_content(
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b"BT [(Total: ) <30303030>] TJ ET\nendstream\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        after = normalize_content(
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b"BT [(Total: ) <39393939>] TJ ET\nendstream\nendobj\n%%EOF",
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
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b"BT (Header) Tj (Old status) ' ET\nendstream\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        after = normalize_content(
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b"BT (Header) Tj (New status) ' ET\nendstream\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        self.assertIn("Old status", before.text)
        self.assertIn("New status", after.text)
        self.assertNotEqual(before.normalized_hash, after.normalized_hash)

        double_quote = normalize_content(
            b"%PDF-1.4\n1 0 obj\n<< /Length 60 >>\nstream\n"
            b'BT (Header) Tj 0 0 (Quoted status) " ET\nendstream\nendobj\n%%EOF',
            content_type="application/pdf",
        )
        self.assertIn("Quoted status", double_quote.text)

    def test_pdf_unsupported_filter_streams_are_rejected_not_skipped(self) -> None:
        # A stream without /FlateDecode is currently assumed to already be
        # decoded plain content. But /ASCIIHexDecode, /ASCII85Decode,
        # /LZWDecode, /RunLengthDecode, and filter chains are also valid and
        # leave their bytes filter-encoded, not plain text. If only the
        # unfiltered stream is scanned, a change confined to a filtered
        # stream would leave the normalized hash unchanged, so such filters
        # must be rejected rather than silently skipped.
        before = (
            b"%PDF-1.4\n1 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Stable label) Tj ET\nendstream\nendobj\n"
            b"2 0 obj\n<< /Filter /ASCIIHexDecode /Length 20 >>\nstream\n"
            b"28546f74616c3a20303030302947\nendstream\nendobj\n%%EOF"
        )
        after = (
            b"%PDF-1.4\n1 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Stable label) Tj ET\nendstream\nendobj\n"
            b"2 0 obj\n<< /Filter /ASCIIHexDecode /Length 20 >>\nstream\n"
            b"28546f74616c3a20393939392947\nendstream\nendobj\n%%EOF"
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
        pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Filter /ASCIIHexDecode /Length 20 "
            b"/Extra (" + padding + b") >>\nstream\n"
            b"28546f74616c3a20303030302947\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "filter"):
            normalize_content(pdf, content_type="application/pdf")

    def test_pdf_truncated_flate_stream_fails_closed(self) -> None:
        # zlib.decompressobj() commonly returns partial output for truncated
        # input without raising zlib.error, so a stream cut short mid-flush
        # could otherwise be accepted as valid text instead of rejected.
        compressed = zlib.compress(b"BT (Hello Flate PDF) Tj ET")
        truncated = compressed[:-4]
        pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
            + str(len(truncated)).encode()
            + b" >>\nstream\n"
            + truncated
            + b"\nendstream\nendobj\n%%EOF"
        )
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

    def test_pdf_custom_font_encodings_are_rejected_not_mishashed(self) -> None:
        # A font's /ToUnicode CMap or /Differences array can remap a
        # character code to a different rendered glyph without the raw
        # string bytes in the content stream changing at all. This extractor
        # decodes string bytes directly and never resolves either mapping,
        # so it must reject such PDFs instead of hashing the unresolved
        # (and potentially wrong) text.
        to_unicode = (
            b"%PDF-1.4\n1 0 obj\n<< /ToUnicode 2 0 R >>\nendobj\n"
            b"3 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "encoding"):
            normalize_content(to_unicode, content_type="application/pdf")

        differences = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding << /Differences [1 /A] >> >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "encoding"):
            normalize_content(differences, content_type="application/pdf")

        # A named /Encoding with no /Differences or /ToUnicode (e.g. a font
        # using /WinAnsiEncoding or /MacRomanEncoding directly) still remaps
        # character codes to glyphs; this extractor's fixed Latin-1 decode
        # can produce the same text/hash regardless of which named encoding
        # is actually active, so it must be rejected too.
        named_encoding = (
            b"%PDF-1.4\n1 0 obj\n<< /Encoding /WinAnsiEncoding >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "encoding"):
            normalize_content(named_encoding, content_type="application/pdf")

        # A composite (/Type0) font always routes character codes through a
        # CMap, regardless of /Encoding/ToUnicode presence.
        composite_font = (
            b"%PDF-1.4\n1 0 obj\n<< /Subtype /Type0 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "encoding"):
            normalize_content(composite_font, content_type="application/pdf")

        # A compressed object stream can hide a font/Encoding dictionary
        # from this raw marker scan entirely, so its mere presence must
        # also be rejected rather than assumed safe.
        object_stream = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /ObjStm /N 1 >>\nendobj\n"
            b"2 0 obj\n<< /Length 20 >>\nstream\n"
            b"BT (Total: 0000) Tj ET\nendstream\nendobj\n%%EOF"
        )
        with self.assertRaisesRegex(MonitorError, "encoding"):
            normalize_content(object_stream, content_type="application/pdf")


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
