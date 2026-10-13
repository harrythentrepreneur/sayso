"""The shared design system (sayso.brand): bundled assets ship and read back, nothing loads off-origin."""
from __future__ import annotations

import re
import unittest

from sayso import brand


class Brand(unittest.TestCase):
    def test_fonts_are_real_woff2_and_licensed(self):
        from importlib import resources
        for fam in brand.FONTS:
            data = brand.font_bytes(fam)
            self.assertEqual(data[:4], b"wOF2", fam)
            self.assertGreater(len(data), 10_000, fam)
        root = resources.files("sayso.brand").joinpath("fonts")
        for lic in ("OFL-Fredoka.txt", "OFL-Nunito.txt"):
            self.assertIn("SIL OPEN FONT LICENSE", root.joinpath(lic).read_text(encoding="utf-8").upper())
        with self.assertRaises(KeyError):
            brand.font_bytes("../../etc/passwd")

    def test_mark_is_inline_svg_with_class(self):
        svg = brand.mark_svg("x-mark")
        self.assertTrue(svg.startswith('<svg class="x-mark" aria-hidden="true" '))
        self.assertIn("#ff7a4d", svg)

    def test_css_has_every_token_and_no_external_refs(self):
        css = brand.css() + brand.font_faces("/brand/fonts")
        for k in brand.TOKENS:
            self.assertIn(f"--{k}:", css)
        self.assertIsNone(re.search(r"(https?:)?//|@import", css))
        self.assertIn("url(/brand/fonts/fredoka-latin.woff2)", css)

    def test_font_faces_refuse_other_origins(self):
        for bad in ("https://cdn.example/f", "//cdn.example/f"):
            with self.assertRaises(ValueError):
                brand.font_faces(bad)

    def test_core_colours_are_the_project_site_colours(self):
        """The site (gh-pages index.html) and this module must not drift apart."""
        self.assertEqual((brand.TOKENS["bg"], brand.TOKENS["coral"], brand.TOKENS["ink"]),
                         ("#0f0d12", "#ff7a4d", "#fff6f1"))


if __name__ == "__main__":
    unittest.main()
