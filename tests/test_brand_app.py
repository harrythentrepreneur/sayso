import re
import unittest
from datetime import datetime, timezone

from sayso.brand import app


class AppPartsTests(unittest.TestCase):
    def test_text_is_escaped(self):
        bad = "<img src=x onerror=alert(1)>"
        for out in (app.badge(bad), app.header(bad, bad, eyebrow_html=""), app.empty(bad, bad),
                    app.tile(bad, app.e(bad), source=bad, note=bad), app.trust_line(bad, version=bad),
                    app.tabs([(bad, bad, 1, True)]), app.search("/s", bad, placeholder=bad), app.source(bad),
                    app.panel(bad, "", sub=bad), app.hbars([(bad, 1)]), app.axis_chart([(bad, 1)])):
            self.assertNotIn("<img", out)

    def test_money_formats(self):
        self.assertEqual(app.money_plain(1200, "usd"), "$12.00 USD")
        self.assertEqual(app.money_plain(500, "jpy"), "\u00a5500 JPY")
        self.assertEqual(app.money_plain("x", "usd"), "\u2014")

    def test_reltime_carries_exact_title(self):
        ref = datetime(2030, 1, 10, 12, tzinfo=timezone.utc)
        out = app.reltime("2030-01-10T09:00:00+00:00", ref)
        self.assertIn(">3h<", out)
        self.assertIn("title='10 Jan 2030, 09:00 UTC'", out)
        self.assertIn("\u2014", app.reltime("nope", ref))

    def test_unknown_tone_is_neutral(self):
        self.assertIn("sa-badge neutral", app.badge("x", "accent"))

    def test_css_no_external_refs_or_scripts(self):
        self.assertIsNone(re.search(r"(https?:)?//|@import|<script", app.css()))


if __name__ == "__main__":
    unittest.main()
