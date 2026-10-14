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
        for lic in ("OFL-Inter.txt",):
            self.assertIn("SIL OPEN FONT LICENSE", root.joinpath(lic).read_text(encoding="utf-8").upper())
        with self.assertRaises(KeyError):
            brand.font_bytes("../../etc/passwd")

    def test_mark_is_inline_svg_with_class(self):
        svg = brand.mark_svg("x-mark")
        self.assertTrue(svg.startswith('<svg class="x-mark" aria-hidden="true" '))
        self.assertIn("#ff7a4d", svg)
        # calm look: a plain bubble with a check, no face (no eyes / cheeks)
        self.assertNotIn("<ellipse", svg)
        self.assertNotIn("<circle", svg)

    def test_css_has_every_token_and_no_external_refs(self):
        css = brand.css() + brand.font_faces("/brand/fonts")
        for k in brand.TOKENS:
            self.assertIn(f"--{k}:", css)
        self.assertIsNone(re.search(r"(https?:)?//|@import", css))
        self.assertIn("url(/brand/fonts/inter-latin.woff2)", css)

    def test_font_faces_refuse_other_origins(self):
        for bad in ("https://cdn.example/f", "//cdn.example/f"):
            with self.assertRaises(ValueError):
                brand.font_faces(bad)

    def test_core_colours_are_the_project_site_colours(self):
        """The site (gh-pages index.html) and this module must not drift apart."""
        self.assertEqual((brand.TOKENS["bg"], brand.TOKENS["coral"], brand.TOKENS["accent"], brand.TOKENS["ink"]),
                         ("#f7f7f5", "#ff7a4d", "#b8461c", "#18181b"))

    def test_calm_look_no_rounded_display_font_no_pill_buttons(self):
        """Kaviru, 15 Oct 2026: the rounded font and pill buttons read as a toy. One grotesk, small radii."""
        css = brand.css()
        self.assertNotIn("Fredoka", css + brand.font_faces("/f"))
        self.assertNotIn("Nunito", css + brand.font_faces("/f"))
        btn = re.search(r"\.s-btn\{([^}]*)\}", css).group(1)
        self.assertIn("border-radius:var(--radius-sm)", btn)
        self.assertLessEqual(int(brand.TOKENS["radius"].rstrip("px")), 10)
        self.assertLessEqual(int(brand.TOKENS["radius-sm"].rstrip("px")), 6)

    def test_text_colours_pass_wcag_aa(self):
        def lum(h):
            h = h.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)
        t = brand.TOKENS
        for fg in ("ink", "ink2", "soft", "faint", "accent", "send", "warn", "danger", "info"):
            for bg in ("bg", "card", "card2"):
                with self.subTest(fg=fg, bg=bg):
                    self.assertGreaterEqual(ratio(t[fg], t[bg]), 4.5)
        self.assertGreaterEqual(ratio(t["coral-ink"], t["accent"]), 4.5)   # button text on the button


if __name__ == "__main__":
    unittest.main()
