"""Sayso design system: one look for every Sayso surface.

The open-source project site, the self-hosted console and the hosted Sayso Cloud all read their colours,
fonts, logo and basic parts from HERE, so a change lands everywhere at once. Sayso Cloud installs this
package at a pinned commit, so a design change reaches the paid app only when Cloud moves its pin.

Look ("calm professional", Kaviru 15 Oct 2026; replaces the dark/coral/rounded look of 13 Oct): a light
work surface, one neutral grotesk (Inter) for everything, small radii (6-10px, no pill buttons), neutral
greys, and coral for ONE job only - the primary action and the active place. Status colours carry meaning
(green done, amber waiting, red risk), never decoration. The mark is a plain speech bubble with a check:
the product's promise (nothing goes out without your say-so), no face.

    from sayso import brand
    brand.TOKENS["accent"]           # one colour
    brand.css()                      # :root variables + base type + parts (.s-btn, .s-card, .s-pill ...)
    brand.font_faces("/brand/fonts") # @font-face rule pointing at a URL prefix you serve
    brand.font_bytes("inter")        # the woff2 itself, to serve from your own origin
    brand.mark_svg()                 # the logo, as an inline <svg>

The font is bundled (SIL Open Font License 1.1, licence next to the file) so a surface never has to load
anything from another origin. Only the Latin subset is bundled: Sayso's UI text is English. Text colours
here meet WCAG AA (4.5:1) on the surfaces they are used on; tests/test_brand.py measures it.
"""
from __future__ import annotations

from importlib import resources

# Token NAMES are a contract with Sayso Cloud (it maps its own variables onto these), so they stay stable
# across looks; only values change. "coral" is the mark's colour; "accent" is the darker coral that passes
# 4.5:1 with white text, used for primary buttons, links and the active place.
TOKENS = {
    "bg": "#f7f7f5", "bg2": "#efefec", "card": "#ffffff", "card2": "#f4f4f2",
    "line": "#e6e5e1", "line2": "#d4d3ce",
    "ink": "#18181b", "ink2": "#3f3f46", "soft": "#5f5f68", "faint": "#6b6b74",
    "coral": "#ff7a4d", "accent": "#b8461c", "accent-hover": "#a03c18", "accent-soft": "#fbeee8",
    "coral-ink": "#ffffff",
    "send": "#15803d", "learn": "#6d28d9", "warn": "#92400e", "danger": "#b91c1c", "info": "#1d4ed8",
    "radius": "10px", "radius-sm": "6px", "pill": "999px",
}
FONTS = {"inter": "inter-latin.woff2"}
HEADING_FONT = '"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif'
TEXT_FONT = HEADING_FONT
MONO_FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def _file(name: str):
    return resources.files("sayso.brand").joinpath(name)


def font_bytes(family: str) -> bytes:
    """The bundled woff2 for 'inter'. Raises KeyError for anything else."""
    return _file("fonts/" + FONTS[family]).read_bytes()


def mark_svg(css_class: str = "s-mark") -> str:
    svg = _file("sayso-mark.svg").read_text(encoding="utf-8").strip()
    return svg.replace("<svg ", f'<svg class="{css_class}" aria-hidden="true" ', 1)


def font_faces(prefix: str) -> str:
    """@font-face rule for Inter, served from `prefix` (e.g. '/brand/fonts'). Same-origin only."""
    if "//" in prefix:
        raise ValueError("serve the fonts from your own origin")
    p = prefix.rstrip("/")
    return (f'@font-face{{font-family:"Inter";font-style:normal;font-weight:100 900;font-display:swap;'
            f'src:url({p}/{FONTS["inter"]}) format("woff2")}}')


def css() -> str:
    """Tokens as CSS variables, base type and the shared parts. Scoped to .s-* classes plus :root/body."""
    root = ";".join(f"--{k}:{v}" for k, v in TOKENS.items())
    return (
        f":root{{{root};--font-head:{HEADING_FONT};--font-text:{TEXT_FONT};--font-mono:{MONO_FONT};"
        "color-scheme:light}"
        "body{font-family:var(--font-text);background:var(--bg);color:var(--ink2);-webkit-font-smoothing:antialiased;"
        "font-feature-settings:'cv11','ss01'}"
        "h1,h2,h3,h4{font-family:var(--font-head);color:var(--ink);font-weight:600;letter-spacing:-.011em}"
        "a{color:inherit}"
        "code,kbd,pre{font-family:var(--font-mono)}"
        ".s-mark{width:28px;height:26px;flex:none}"
        ".s-brand{display:inline-flex;align-items:center;gap:8px;font:650 17px var(--font-head);color:var(--ink);"
        "letter-spacing:-.01em;text-decoration:none}"
        ".s-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;"
        "padding:0 16px;border:1px solid transparent;border-radius:var(--radius-sm);background:var(--accent);"
        "color:var(--coral-ink);font:600 14px var(--font-text);text-decoration:none;cursor:pointer;"
        "transition:background .12s,border-color .12s}"
        ".s-btn:hover{background:var(--accent-hover);text-decoration:none}"
        ".s-btn.ghost{background:var(--card);color:var(--ink);border-color:var(--line2)}"
        ".s-btn.ghost:hover{background:var(--card2)}"
        ".s-btn.send{background:var(--send);color:#fff}.s-btn.danger{background:var(--danger);color:#fff}"
        ".s-btn.sm{min-height:32px;padding:0 12px;font-size:13px}"
        ".s-btn:focus-visible,.s-focus:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
        ".s-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px}"
        ".s-pill{display:inline-flex;align-items:center;gap:6px;border-radius:var(--radius-sm);padding:2px 8px;"
        "font:600 12px var(--font-text)}"
        ".s-pill.live,.s-pill.ok{background:#dcfce7;color:var(--send)}"
        ".s-pill.soon,.s-pill.warn{background:#fef3c7;color:var(--warn)}"
        ".s-pill.info{background:var(--accent-soft);color:var(--accent)}"
        ".s-pill.learn{background:#ede9fe;color:var(--learn)}"
        ".s-pill.bad{background:#fee2e2;color:var(--danger)}"
        ".s-eyebrow{font:600 12px var(--font-text);letter-spacing:.04em;text-transform:uppercase;color:var(--soft)}"
        ".s-field{width:100%;min-height:40px;padding:8px 12px;border-radius:var(--radius-sm);"
        "border:1px solid var(--line2);background:var(--card);color:var(--ink);font:400 15px var(--font-text)}"
        ".s-field:focus{outline:2px solid var(--accent);outline-offset:1px}"
    )
