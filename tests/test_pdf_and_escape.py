"""Tests for the PDF resource-fetch block and the Markdown cell escaper.

Run: python3 -m unittest tests.test_pdf_and_escape
(_md tests self-skip when the `mcp` dependency isn't installed.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdf_report


class BlockingUrlFetcher(unittest.TestCase):
    def test_refuses_file_url(self):
        with self.assertRaises(ValueError):
            pdf_report._blocking_url_fetcher("file:///etc/passwd")

    def test_refuses_internal_http_url(self):
        with self.assertRaises(ValueError):
            pdf_report._blocking_url_fetcher("http://127.0.0.1:6379/")

    def test_refuses_public_http_url(self):
        # The report needs no external resources at all — even public ones fail.
        with self.assertRaises(ValueError):
            pdf_report._blocking_url_fetcher("https://example.com/x.png")


class MdEscape(unittest.TestCase):
    def setUp(self):
        try:
            import server
        except Exception as e:                      # mcp not installed in CI dev box
            self.skipTest(f"server import unavailable: {e}")
        self.server = server

    def test_neutralises_injected_html_tag(self):
        out = self.server._md('<img src="file:///etc/passwd">')
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)

    def test_escapes_table_control_chars(self):
        out = self.server._md("col_a | col_b\nsecond line")
        self.assertNotIn("\n", out)
        self.assertIn("\\|", out)

    def test_handles_non_str(self):
        self.assertEqual(self.server._md(123), "123")


if __name__ == "__main__":
    unittest.main()
