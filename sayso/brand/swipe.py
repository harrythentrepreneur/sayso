"""Swipe deck: one decision at a time, drag right for Yes, left for No (Kaviru, 14 Oct 2026).

Shared by the self-hosted console and Sayso Cloud, so both feel the same. The deck is progressive:

* The server renders each open decision as an ordinary form (`card_form`) with real Yes / No buttons,
  bound to the exact fingerprint shown. Without JavaScript the buttons work as before.
* `swipe.js` (served by the host from its own origin, see `script_bytes`) makes the top card draggable.
  A released swipe posts the SAME form with the header ``X-Sayso-Swipe: 1``. The host answers JSON:
  ``{"ok": true}`` or ``{"error": "<why>"}`` with a 4xx. Nothing about what a vote means changes.

The page that shows the deck must allow ``script-src 'self'`` and ``connect-src 'self'`` and nothing else.
`DECK_CSP` is that policy.
"""
from __future__ import annotations

from html import escape
from importlib import resources

SWIPE_HEADER = "X-Sayso-Swipe"
SCRIPT_NAME = "swipe.js"
DECK_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def script_bytes() -> bytes:
    """The deck script, to serve from your own origin as application/javascript."""
    return resources.files("sayso.brand").joinpath(SCRIPT_NAME).read_bytes()


def is_swipe(headers) -> bool:
    """True when a POST came from the deck script (it then wants JSON, not a redirect)."""
    try:
        return str(headers.get(SWIPE_HEADER, "")).strip() == "1"
    except AttributeError:
        return False


def css() -> str:
    """Deck styles. Uses the brand tokens (load brand.css() first)."""
    return (
        ".s-deck{--sw-card:var(--card,var(--bg,#1b1720));--sw-card2:var(--card2,var(--side,#241f2a));"
        "--sw-line:var(--line,rgba(255,255,255,.07));--sw-line2:var(--line2,var(--line,rgba(255,255,255,.12)));"
        "--sw-ink:var(--ink,#fff6f1);--sw-ink2:var(--ink2,var(--text,#d9d0d8));--sw-soft:var(--soft,var(--mute,#a097a4));"
        "--sw-faint:var(--faint,#6f6673);--sw-send:var(--send,var(--green,#2fdc8a));--sw-danger:var(--danger,var(--red,#ff5d6c));"
        "position:relative;max-width:560px;margin:0 auto;overflow-x:clip}"
        ".s-deck-top{display:flex;justify-content:space-between;align-items:center;margin:0 0 12px;"
        "color:var(--sw-soft);font-size:14px}"
        ".s-deck-stack{position:relative;display:grid}"
        ".s-deck.is-live .s-deck-stack>.s-card-swipe{grid-area:1/1}"
        ".s-card-swipe{position:relative;background:var(--sw-card);border:1px solid var(--sw-line2);"
        "border-radius:var(--radius,18px);padding:18px 18px 14px;margin:0 0 14px;color:var(--sw-ink2);"
        "box-shadow:0 18px 40px rgba(0,0,0,.35),0 0 0 calc(var(--glow,0)*3px) rgba(255,122,77,.0);"
        "transform-origin:50% 80%;touch-action:pan-y;user-select:none;-webkit-user-select:none}"
        ".s-deck.is-live .s-card-swipe{margin:0}"
        ".s-deck.is-live .s-card-swipe:not(.is-top){pointer-events:none;box-shadow:0 6px 16px rgba(0,0,0,.12)}"
        ".s-deck.is-live .s-card-swipe:not(.is-top)>*{visibility:hidden}"
        ".s-card-swipe.is-dragging{cursor:grabbing;transition:none}"
        ".s-card-swipe.is-yes{box-shadow:0 18px 40px rgba(0,0,0,.35),0 0 0 3px rgba(47,220,138,calc(var(--glow,0)*.9))}"
        ".s-card-swipe.is-no{box-shadow:0 18px 40px rgba(0,0,0,.35),0 0 0 3px rgba(160,151,164,calc(var(--glow,0)*.9))}"
        ".s-stamp{position:absolute;top:auto;bottom:40%;background:var(--sw-card);padding:4px 12px;border:3px solid;border-radius:10px;"
        "font:700 22px var(--font-head,inherit);letter-spacing:.06em;opacity:0;pointer-events:none;z-index:2}"
        ".s-stamp-yes{left:14px;color:var(--sw-send);border-color:var(--sw-send);transform:rotate(-10deg)}"
        ".s-stamp-no{right:14px;color:var(--sw-soft);border-color:var(--sw-soft);transform:rotate(10deg)}"
        ".s-card-swipe .s-card-q{font:600 19px/1.3 var(--font-head,inherit);color:var(--sw-ink);margin:0 0 6px}"
        ".s-card-swipe .s-card-who{font-size:14px;color:var(--sw-soft);margin:0 0 12px}"
        ".s-card-swipe .s-card-body{max-height:max(140px,calc(100svh - 300px));overflow:auto;-webkit-overflow-scrolling:touch;"
        "border-top:1px solid var(--sw-line);padding-top:12px}"
        ".s-card-hint{display:flex;justify-content:space-between;gap:10px;margin:12px 0 0;font-size:13px;color:var(--sw-faint)}"
        ".s-card-btns{display:flex;gap:10px;margin:12px 0 0}"
        ".s-card-btns button{flex:1;min-height:46px;border-radius:999px;font:700 16px var(--font-text,inherit);"
        "cursor:pointer;border:0}"
        ".s-card-btns .s-no{background:var(--sw-card2);color:var(--sw-ink2);box-shadow:inset 0 0 0 1px var(--sw-line2)}"
        ".s-card-btns .s-yes{background:var(--sw-send);color:#fff;text-shadow:0 1px 1px rgba(0,0,0,.25)}"
        ".s-deck-msg{min-height:22px;margin:12px 0 0;font-size:14px;color:var(--sw-soft);text-align:center}"
        ".s-deck-msg.is-bad{color:var(--sw-danger)}"
        ".s-deck-done{text-align:center;padding:30px 10px;color:var(--sw-soft)}"
        "@media (prefers-reduced-motion:reduce){.s-card-swipe{transition:none!important}}"
    )


def card_form(*, action: str, csrf: str, key: str, fingerprint: str, question: str, who: str,
              body_html: str, yes_label: str = "Yes, send", no_label: str = "No, skip",
              yes_hint: str = "Yes", no_hint: str = "No",
              extra_fields: dict | None = None) -> str:
    """One decision as a swipe card. `body_html` must already be escaped by the caller."""
    hidden = {"csrf": csrf, "key": key, "fp": fingerprint, **(extra_fields or {})}
    fields = "".join(f"<input type=hidden name={escape(k)} value='{escape(str(v), quote=True)}'>"
                     for k, v in hidden.items())
    return (f"<form class=s-card-swipe method=post action='{escape(action, quote=True)}'>{fields}"
            "<span class='s-stamp s-stamp-yes' aria-hidden=true>YES</span>"
            "<span class='s-stamp s-stamp-no' aria-hidden=true>NO</span>"
            f"<p class=s-card-q>{escape(question)}</p><p class=s-card-who>{escape(who)}</p>"
            f"<div class=s-card-body>{body_html}</div>"
            f"<div class=s-card-btns><button class=s-no name=answer value=no>{escape(no_label)}</button>"
            f"<button class=s-yes name=answer value=yes>{escape(yes_label)}</button></div>"
            f"<div class=s-card-hint><span>&larr; {escape(no_hint)}</span>"
            f"<span>{escape(yes_hint)} &rarr;</span></div></form>")


def deck(cards: list[str], *, script_url: str, done_html: str = "Nothing waiting. New decisions appear here.") -> str:
    """The deck around pre-rendered `card_form` cards. `done_html` must already be escaped."""
    n = len(cards)
    return (f"<section class=s-deck data-sayso-deck>"
            f"<div class=s-deck-top><span>Swipe right for Yes, left for No. Arrow keys work too.</span>"
            f"<b data-deck-left>{n} left</b></div>"
            f"<div class=s-deck-stack>{''.join(cards)}</div>"
            f"<div class=s-deck-done data-deck-done{'' if not n else ' hidden'}>{done_html}</div>"
            f"<p class=s-deck-msg data-deck-msg role=status aria-live=polite></p>"
            f"<script src='{escape(script_url, quote=True)}' defer></script></section>")
