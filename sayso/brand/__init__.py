"""Sayso design system: one look for every Sayso surface (Kaviru, 13 Oct 2026).

The open-source project site, the self-hosted console and the hosted Sayso Cloud all read their colours,
fonts, logo and basic parts from HERE, so a change lands everywhere at once. Sayso Cloud installs this
package at a pinned commit, so a design change reaches the paid app only when Cloud moves its pin.

Look: dark, coral, rounded type (Fredoka for headings, Nunito for text), the smiling speech-bubble mark.

    from sayso import brand
    brand.TOKENS["coral"]            # one colour
    brand.css()                      # :root variables + base type + parts (.s-btn, .s-card, .s-pill ...)
    brand.font_faces("/brand/fonts") # @font-face rules pointing at a URL prefix you serve
    brand.font_bytes("fredoka")      # the woff2 itself, to serve from your own origin
    brand.mark_svg()                 # the logo, as an inline <svg>

Fonts are bundled (SIL Open Font License 1.1, licences next to the files) so a surface never has to load
anything from another origin. Only the Latin subset is bundled: Sayso's UI text is English.
"""
from __future__ import annotations

from importlib import resources

TOKENS = {
    "bg": "#0f0d12", "bg2": "#141118", "card": "#1b1720", "card2": "#241f2a",
    "line": "rgba(255,255,255,.07)", "line2": "rgba(255,255,255,.12)",
    "ink": "#fff6f1", "ink2": "#d9d0d8", "soft": "#a097a4", "faint": "#6f6673",
    "coral": "#ff7a4d", "coral-ink": "#1a0f0a", "send": "#2fdc8a", "learn": "#b49cff", "warn": "#ffc53d",
    "danger": "#ff5d6c",
    "radius": "18px", "radius-sm": "12px", "pill": "999px",
}
FONTS = {"fredoka": "fredoka-latin.woff2", "nunito": "nunito-latin.woff2"}
HEADING_FONT = '"Fredoka","Nunito",system-ui,sans-serif'
TEXT_FONT = '"Nunito",system-ui,sans-serif'


def _file(name: str):
    return resources.files("sayso.brand").joinpath(name)


def font_bytes(family: str) -> bytes:
    """The bundled woff2 for 'fredoka' or 'nunito'. Raises KeyError for anything else."""
    return _file("fonts/" + FONTS[family]).read_bytes()


def mark_svg(css_class: str = "s-mark") -> str:
    svg = _file("sayso-mark.svg").read_text(encoding="utf-8").strip()
    return svg.replace("<svg ", f'<svg class="{css_class}" aria-hidden="true" ', 1)


def font_faces(prefix: str) -> str:
    """@font-face rules for both families, served from `prefix` (e.g. '/brand/fonts'). Same-origin only."""
    if "//" in prefix:
        raise ValueError("serve the fonts from your own origin")
    p = prefix.rstrip("/")
    return (f'@font-face{{font-family:"Fredoka";font-style:normal;font-weight:300 700;font-display:swap;'
            f'src:url({p}/{FONTS["fredoka"]}) format("woff2")}}'
            f'@font-face{{font-family:"Nunito";font-style:normal;font-weight:200 1000;font-display:swap;'
            f'src:url({p}/{FONTS["nunito"]}) format("woff2")}}')


def css() -> str:
    """Tokens as CSS variables, base type and the shared parts. Scoped to .s-* classes plus :root/body."""
    root = ";".join(f"--{k}:{v}" for k, v in TOKENS.items())
    return (
        f":root{{{root};--font-head:{HEADING_FONT};--font-text:{TEXT_FONT};color-scheme:dark}}"
        "body{font-family:var(--font-text);background:var(--bg);color:var(--ink2);-webkit-font-smoothing:antialiased}"
        "h1,h2,h3,h4{font-family:var(--font-head);color:var(--ink);font-weight:600;letter-spacing:-.01em}"
        "a{color:inherit}"
        ".s-mark{width:34px;height:31px;flex:none}"
        ".s-brand{display:inline-flex;align-items:center;gap:9px;font:600 23px var(--font-head);color:var(--coral);"
        "text-decoration:none}"
        ".s-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;"
        "padding:0 22px;border:0;border-radius:var(--pill);background:var(--coral);color:var(--coral-ink);"
        "font:600 16px var(--font-head);text-decoration:none;cursor:pointer;transition:transform .15s,filter .15s}"
        ".s-btn:hover{transform:translateY(-1px);filter:brightness(1.05);text-decoration:none}"
        ".s-btn.ghost{background:transparent;color:var(--ink);box-shadow:inset 0 0 0 1.5px var(--line2)}"
        ".s-btn.ghost:hover{background:var(--card)}"
        ".s-btn.send{background:var(--send);color:#06210f}.s-btn.danger{background:var(--danger);color:#2a0609}"
        ".s-btn.sm{min-height:36px;padding:0 14px;font-size:14px}"
        ".s-btn:focus-visible,.s-focus:focus-visible{outline:3px solid var(--coral);outline-offset:2px}"
        ".s-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px}"
        ".s-pill{display:inline-flex;align-items:center;gap:6px;border-radius:var(--pill);padding:3px 10px;"
        "font:900 11px var(--font-text);letter-spacing:.06em;text-transform:uppercase}"
        ".s-pill.live,.s-pill.ok{background:rgba(47,220,138,.15);color:var(--send)}"
        ".s-pill.soon,.s-pill.warn{background:rgba(255,197,61,.14);color:var(--warn)}"
        ".s-pill.info{background:rgba(255,122,77,.15);color:var(--coral)}"
        ".s-pill.learn{background:rgba(180,156,255,.15);color:var(--learn)}"
        ".s-pill.bad{background:rgba(255,93,108,.15);color:var(--danger)}"
        ".s-eyebrow{font:800 13px var(--font-text);letter-spacing:.06em;text-transform:uppercase;color:var(--coral)}"
        ".s-field{width:100%;min-height:44px;padding:10px 14px;border-radius:var(--radius-sm);"
        "border:1px solid var(--line2);background:var(--bg2);color:var(--ink);font:500 16px var(--font-text)}"
        ".s-field:focus{outline:2px solid var(--coral);outline-offset:1px}"
    )
