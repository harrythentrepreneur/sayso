import re
import unittest
from datetime import datetime, timezone

from sayso.brand import app


class AppPartsTests(unittest.TestCase):
    def test_text_is_escaped(self):
        for out in (app.badge("<b>x</b>"), app.header("<i>t</i>", "<s>"), app.empty("<a>", "<b>"),
                    app.tile("<x>", app.e("<y>"), source="<z>"), app.trust_line("<fp>", version="<v>")):
            self.assertNotRegex(out, r"<(b|i|s|a|x|y|z|fp|v)>")

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
