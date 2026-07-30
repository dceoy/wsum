from __future__ import annotations

import unittest

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
