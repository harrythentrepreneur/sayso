"""App parts shared by every Sayso work page: the self-hosted console and dashboard, and (later) Sayso Cloud.

The calm look's page craft, as small stdlib helpers that return HTML strings: a page header, the ONE status
badge, KPI tiles with a source line, segmented tabs with counts, a GET search field, a compact table that
becomes stacked rows on phones, relative times that carry the exact time in ``title``, money as "$12.00 USD",
the say-so trust line (exact version + fingerprint), and empty states that teach.

    from sayso.brand import app
    app.css()                                  # the component CSS (classes start with "sa-")
    app.badge("Waiting", "warn")
    app.tile("Refunded", app.money(1900, "usd"), source="Verified with the payment provider")

Rules the helpers keep:
- Every text argument is HTML-escaped here. Arguments named ``*_html`` are trusted HTML built by other
  helpers; never pass customer text in them.
- Status tones are semantic: neutral = waiting/info, warn = needs attention, good = done/verified,
  bad = failed only, info = a mode. The brand accent is never a status colour.
- No scripts and no external loads: everything works under a CSP with no script-src.
- Colours come from ``sayso.brand.TOKENS`` (names are a contract; values may change).
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

TONES = ("neutral", "good", "warn", "bad", "info")
SYMBOL = {"usd": "$", "aud": "$", "cad": "$", "nzd": "$", "sgd": "$", "hkd": "$", "eur": "\u20ac", "gbp": "\u00a3",
          "jpy": "\u00a5", "inr": "\u20b9"}
ZERO_DECIMAL = frozenset({"jpy", "krw", "vnd", "clp", "isk", "ugx", "xaf", "xof"})


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# -- times ------------------------------------------------------------------------------------------

def parse(value: Any) -> datetime | None:
    """An ISO time in UTC; None when it is not one (a naive time is read as UTC)."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)


def exact(value: Any) -> str:
    at = parse(value)
    return at.strftime("%d %b %Y, %H:%M UTC") if at else ("" if value is None else str(value))


def short_age(secs: float) -> str:
    """'now', '4m', '3h', '2d' - for dense rows."""
    secs = max(0, int(secs))
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def long_age(secs: float) -> str:
    """'just now', '4 min', '3 h', '2 days' - for sentences."""
    secs = max(0, int(secs))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        return f"{secs // 3600} h"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''}"


def reltime(value: Any, ref: datetime, *, long: bool = False, suffix: str = "") -> str:
    """A relative time with the exact time (UTC) in title= and datetime=. Unreadable -> a dash."""
    at = parse(value)
    if at is None:
        return "<span class=sa-faint>\u2014</span>"
    secs = (ref - at).total_seconds()
    text = long_age(secs) if long else short_age(secs)
    if suffix and text not in ("now", "just now"):
        text += suffix
    return f"<time datetime='{e(at.isoformat())}' title='{e(exact(value))}'>{e(text)}</time>"


# -- money --------------------------------------------------------------------------------------------

def money_plain(minor: Any, currency: Any) -> str:
    """'$12.00 USD' (symbol, two decimals, the code so two dollar currencies never look alike)."""
    cur = str(currency or "").lower()
    try:
        amount = int(minor)
    except (TypeError, ValueError):
        return "\u2014"
    value = f"{amount:,}" if cur in ZERO_DECIMAL else f"{amount / 100:,.2f}"
    return f"{SYMBOL.get(cur, '')}{value} {cur.upper()}".strip()


def money(minor: Any, currency: Any) -> str:
    """The amount bold with a muted currency code, tabular digits: <b>$12.00</b> <span>USD</span>."""
    text = money_plain(minor, currency)
    if text == "\u2014":
        return "<span class=sa-faint>\u2014</span>"
    amount, _, code = text.rpartition(" ")
    if not amount:
        amount, code = code, ""
    return f"<span class=sa-amt><b>{e(amount)}</b>{f' <span class=sa-cur>{e(code)}</span>' if code else ''}</span>"


# -- parts -------------------------------------------------------------------------------------------

def badge(text: str, tone: str = "neutral", *, title: str = "") -> str:
    """The ONE status badge. Sentence case. tone: neutral | good | warn | bad | info."""
    t = f" title='{e(title)}'" if title else ""
    return f"<span class='sa-badge {tone if tone in TONES else 'neutral'}'{t}>{e(text)}</span>"


def header(title: str, sub: str = "", *, actions_html: str = "", eyebrow_html: str = "", meta_html: str = "") -> str:
    """Page title (a noun), one plain subtitle line, actions on the right (one primary at most)."""
    eb = f"<div class=sa-eyebrow>{eyebrow_html}</div>" if eyebrow_html else ""
    meta = f"<span class=sa-ph-m>{meta_html}</span>" if meta_html else ""
    return (f"<div class=sa-ph><div class=sa-ph-l>{eb}<div class=sa-ph-t><h1>{e(title)}</h1>{meta}</div>"
            f"{f'<p class=sa-sub>{e(sub)}</p>' if sub else ''}</div>"
            f"{f'<div class=sa-ph-a>{actions_html}</div>' if actions_html else ''}</div>")


def tile(label: str, value_html: str, *, source: str = "", note: str = "", link_html: str = "",
         hot: bool = False) -> str:
    """A KPI tile: label, value, an optional note, and the line that says where the number came from.
    value_html is trusted (build it with e() or money()); show a dash plus a note when it cannot be read."""
    return (f"<div class='sa-tile{' hot' if hot else ''}'><span class=sa-tile-l>{e(label)}</span>"
            f"<span class=sa-tile-v>{value_html}</span>"
            f"{f'<span class=sa-tile-n>{e(note)}</span>' if note else ''}"
            f"{f'<span class=sa-tile-s>{e(source)}</span>' if source else ''}"
            f"{f'<span class=sa-tile-k>{link_html}</span>' if link_html else ''}</div>")


def tiles(items: Iterable[str]) -> str:
    return f"<div class=sa-tiles>{''.join(items)}</div>"


def tabs(items: Iterable[tuple[str, str, int | None, bool]], *, label: str = "Filter") -> str:
    """Segmented tabs that are plain links: [(label, href, count or None, current)]. A zero count is muted."""
    out = []
    for text, href, count, on in items:
        c = "" if count is None else f"<span class='sa-n{' z' if not count else ''}'>{int(count)}</span>"
        cur = " aria-current=page" if on else ""
        out.append(f"<a class='sa-tab{' on' if on else ''}' href='{e(href)}'{cur}>{e(text)}{c}</a>")
    return f"<div class=sa-tabs role=group aria-label='{e(label)}'>{''.join(out)}</div>"


def segmented(items: Iterable[tuple[str, str, bool]], *, label: str = "View") -> str:
    """A small segmented control of links: [(label, href, current)]."""
    out = "".join(f"<a class='{'on' if on else ''}' href='{e(href)}'{' aria-current=page' if on else ''}>{e(t)}</a>"
                  for t, href, on in items)
    return f"<div class=sa-seg role=group aria-label='{e(label)}'>{out}</div>"


def search(action: str, q: str = "", *, placeholder: str = "Search", keep: dict | None = None,
           label: str = "Search") -> str:
    """An inline GET search field with an icon. A submit button stays for no-JS keyboards (Enter works)."""
    hidden = "".join(f"<input type=hidden name='{e(k)}' value='{e(v)}'>" for k, v in (keep or {}).items() if v)
    return (f"<form class=sa-search method=get action='{e(action)}' role=search>{hidden}"
            f"<label class=sa-search-f><span class=sa-sr>{e(label)}</span>{ICON_SEARCH}"
            f"<input type=search name=q value='{e(q)}' placeholder='{e(placeholder)}' autocomplete=off></label>"
            f"<button class='sa-btn ghost sm' type=submit>Search</button></form>")


def table(columns: list, rows: list, *, empty_html: str = "", caption: str = "", cls: str = "") -> str:
    """A compact table that becomes stacked rows on phones.

    columns: [(label, align)] with align '' | 'num' (right, tabular). A bare string is a label.
    rows: [[cell_html, ...]] (cells are TRUSTED html) or {"cells": [...], "href": url, "cls": str} to make
    the row a link (the first cell carries the real <a>, so keyboards and screen readers get one link)."""
    cols = [(c, "") if isinstance(c, str) else (c[0], c[1] if len(c) > 1 else "") for c in columns]
    if not rows:
        return empty_html or empty("Nothing here yet", "Rows appear here as soon as there is something to show.")
    head = "".join(f"<th scope=col class='{e(a)}'>{e(lbl)}</th>" for lbl, a in cols)
    body = []
    for r in rows:
        cells, href, rc = (r.get("cells", []), r.get("href", ""), r.get("cls", "")) if isinstance(r, dict) else (r, "", "")
        tds = []
        for i, cell in enumerate(cells):
            lbl, al = cols[i] if i < len(cols) else ("", "")
            inner = f"<a class=sa-rowlink href='{e(href)}'>{cell}</a>" if href and i == 0 else cell
            tds.append(f"<td class='{e(al)}' data-l='{e(lbl)}'>{inner}</td>")
        klass = " ".join(x for x in ("sa-link" if href else "", rc) if x)
        body.append(f"<tr{f' class={chr(39)}{e(klass)}{chr(39)}' if klass else ''}>{''.join(tds)}</tr>")
    cap = f"<caption class=sa-sr>{e(caption)}</caption>" if caption else ""
    return (f"<div class='sa-table-w {e(cls)}'><table class=sa-table>{cap}<thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def empty(title: str, text: str, action_html: str = "") -> str:
    """What will appear here, why it is empty, and the one thing that fills it."""
    return f"<div class=sa-empty><b>{e(title)}</b><p>{e(text)}</p>{action_html}</div>"


def source(text: str) -> str:
    """'Source: ...' under a number or chart: every number says where it came from."""
    return f"<p class=sa-src>Source: {e(text)}</p>"


def trust_line(fingerprint: str, *, version: str = "", what: str = "this version") -> str:
    """The say-so lock: a Yes counts only for the exact version shown (identified by its fingerprint)."""
    fp = str(fingerprint or "")
    ver = f"approves only {e(version)}" if version else f"approves only {e(what)}"
    return (f"<div class=sa-trust>{ICON_LOCK}<span><b>Say-so lock</b> \u00b7 {ver}, fingerprint "
            f"<code title='{e(fp)}'>{e(fp[:12])}</code> \u00b7 any change to it voids this vote</span></div>")


def panel(title: str, body_html: str, *, sub: str = "", actions_html: str = "", cls: str = "") -> str:
    return (f"<section class='sa-panel {e(cls)}'><header><div><h2>{e(title)}</h2>"
            f"{f'<p>{e(sub)}</p>' if sub else ''}</div>{actions_html}</header>{body_html}</section>")


def hbars(rows: list[tuple[str, int]], *, total: int | None = None) -> str:
    """Horizontal bars with the count and share: [(label, n)]."""
    if not rows:
        return "<p class=sa-faint>No data yet.</p>"
    top = max([n for _, n in rows] + [1])
    whole = total if total else sum(n for _, n in rows) or 1
    return "".join(
        f"<div class=sa-hb><span class=sa-hb-l title='{e(k)}'>{e(k)}</span><span class=sa-hb-t>"
        f"<i style='width:{max(100 * n / top, 2 if n else 0):.0f}%'></i></span>"
        f"<span class=sa-hb-n>{n}<small>{100 * n / whole:.0f}%</small></span></div>" for k, n in rows)


def axis_chart(series: list[tuple[str, int]], *, second: list[tuple[str, int]] | None = None,
               label: str = "chart", w: int = 640, h: int = 200) -> str:
    """Daily bars on a real axis: y gridlines with values, x labels at start/middle/end, every bar titled.
    With `second`, each bar stacks `second` (red) on top of `series` (accent or green)."""
    n = len(series)
    extra = dict(second or [])
    peak = max([v + extra.get(k, 0) for k, v in series] + [1])
    step = max(1, -(-peak // 4))
    top = step * 4
    left, right, base, upper = 34, 6, h - 24, 10
    plot_w = w - left - right
    gap = 3 if n > 20 else 6
    bw = (plot_w - gap * (n - 1)) / max(n, 1)
    parts = []
    for i in range(5):
        y = base - (base - upper) * i / 4
        parts.append(f"<line x1='{left}' x2='{w - right}' y1='{y:.1f}' y2='{y:.1f}'/>"
                     f"<text class=y x='{left - 6}' y='{y + 4:.1f}' text-anchor='end'>{step * i}</text>")
    for i, (lbl, v) in enumerate(series):
        v2 = extra.get(lbl, 0)
        x = left + i * (bw + gap)
        bh = (base - upper) * v / top
        bh2 = (base - upper) * v2 / top
        tip = f"{lbl}: {v}" + (f" Yes, {v2} No" if second is not None else "")
        if v:
            parts.append(f"<rect class=b x='{x:.1f}' y='{base - bh:.1f}' width='{bw:.1f}' height='{bh:.1f}' rx='2'>"
                         f"<title>{e(tip)}</title></rect>")
        if v2:
            parts.append(f"<rect class='b no2' x='{x:.1f}' y='{base - bh - bh2:.1f}' width='{bw:.1f}' "
                         f"height='{bh2:.1f}' rx='2'><title>{e(tip)}</title></rect>")
        if not v and not v2:
            parts.append(f"<rect class='b z' x='{x:.1f}' y='{base - 1:.1f}' width='{bw:.1f}' height='1'>"
                         f"<title>{e(tip)}</title></rect>")
        if i in (0, n // 2, n - 1):
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            tx = x if i == 0 else (x + bw if i == n - 1 else x + bw / 2)
            parts.append(f"<text x='{tx:.1f}' y='{h - 6}' text-anchor='{anchor}'>{e(lbl)}</text>")
    return (f"<svg class=sa-chart viewBox='0 0 {w} {h}' role='img' aria-label='{e(label)}'>"
            f"{''.join(parts)}</svg>")


def qs(base: str, **params: Any) -> str:
    """base?k=v for the params that are set (links keep the other filters)."""
    kept = {k: v for k, v in params.items() if v not in (None, "", 0)}
    return f"{base}?{urlencode(kept)}" if kept else base


ICON_SEARCH = ("<svg class=sa-ic viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
               "stroke-linecap='round' aria-hidden='true'><path d='M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4-4'/></svg>")
ICON_LOCK = ("<svg class=sa-ic viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
             "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
             "<path d='M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 017 0v3'/></svg>")


def css() -> str:
    """Component CSS. Reads the sayso.brand tokens as CSS variables (load brand.css() first)."""
    return _CSS


_CSS = """
.sa-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.sa-faint{color:var(--faint)}.sa-ic{width:16px;height:16px;flex:none}
.sa-ph{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 22px}
.sa-ph-l{min-width:0;flex:1 1 320px}.sa-ph-t{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sa-ph h1{font-size:22px;line-height:1.25;margin:0;letter-spacing:-.015em}
.sa-ph-m{display:inline-flex;gap:6px;flex-wrap:wrap}
.sa-sub{margin:4px 0 0;color:var(--soft);font-size:14px}.sa-eyebrow{font-size:13px;color:var(--faint);margin-bottom:4px}
.sa-eyebrow a{color:var(--faint)}.sa-ph-a{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.sa-badge{display:inline-flex;align-items:center;gap:6px;padding:1px 8px;border-radius:var(--radius-sm);font-size:12px;
font-weight:550;line-height:1.6;white-space:nowrap;border:1px solid transparent}
.sa-badge.neutral{background:#f1f1ee;color:var(--ink2);border-color:var(--line)}
.sa-badge.info{background:#eaf0fd;color:var(--info);border-color:#c7d6f8}
.sa-badge.good{background:#e8f5ec;color:var(--send);border-color:#bfe3cb}
.sa-badge.warn{background:#fdf4e3;color:var(--warn);border-color:#f1d9a6}
.sa-badge.bad{background:#fdecec;color:var(--danger);border-color:#f1b5b5}
.sa-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:36px;padding:0 14px;
border:1px solid transparent;border-radius:var(--radius-sm);background:var(--accent);color:var(--coral-ink);
font:600 13.5px/1 var(--font-text);text-decoration:none;cursor:pointer;white-space:nowrap}
.sa-btn:hover{background:var(--accent-hover);text-decoration:none}
.sa-btn.ghost{background:var(--card);color:var(--ink);border-color:var(--line2)}.sa-btn.ghost:hover{background:var(--card2)}
.sa-btn.sm{min-height:30px;padding:0 10px;font-size:13px}
.sa-btn:focus-visible,.sa-tab:focus-visible,.sa-seg a:focus-visible,.sa-rowlink:focus-visible{outline:2px solid var(--accent);
outline-offset:2px}
.sa-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));border:1px solid var(--line);
border-radius:var(--radius);background:var(--card);margin:0 0 22px;overflow:hidden}
.sa-tile{display:flex;flex-direction:column;gap:3px;padding:14px 18px;min-width:0;box-shadow:-1px 0 0 var(--line),0 -1px 0 var(--line)}
.sa-tile-l{font-size:13px;color:var(--soft);font-weight:500}
.sa-tile-v{font-size:22px;font-weight:600;color:var(--ink);letter-spacing:-.02em;line-height:1.25;
font-variant-numeric:tabular-nums}.sa-tile-v .sa-cur{font-size:12px;font-weight:500;letter-spacing:0}
.sa-tile.hot .sa-tile-v{color:var(--accent)}
.sa-tile-n{font-size:12.5px;color:var(--ink2)}.sa-tile-s{font-size:12px;color:var(--faint)}
.sa-tile-k{font-size:12.5px;font-weight:600}.sa-tile-k a{color:var(--accent)}
.sa-amt{white-space:nowrap;font-variant-numeric:tabular-nums}.sa-amt b{color:var(--ink);font-weight:600}
.sa-cur{color:var(--faint);font-size:.85em}
.sa-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
border-bottom:1px solid var(--line);margin:0 0 14px}
.sa-tabs{display:flex;gap:2px;flex-wrap:wrap;margin-bottom:-1px}
.sa-tab{display:inline-flex;align-items:center;gap:7px;padding:9px 10px;font-size:13.5px;font-weight:500;color:var(--soft);
text-decoration:none;border-bottom:2px solid transparent;min-height:40px}
.sa-tab:hover{color:var(--ink);text-decoration:none}.sa-tab.on{color:var(--ink);font-weight:600;border-bottom-color:var(--ink)}
.sa-n{font-size:12px;font-weight:600;color:var(--ink2);background:#f1f1ee;border-radius:5px;padding:0 6px;
font-variant-numeric:tabular-nums}.sa-n.z{background:none;color:var(--faint);font-weight:500}
.sa-seg{display:inline-flex;border:1px solid var(--line2);border-radius:var(--radius-sm);overflow:hidden;background:var(--card)}
.sa-seg a{padding:6px 11px;font-size:13px;color:var(--ink2);text-decoration:none;border-left:1px solid var(--line);min-height:32px;
display:inline-flex;align-items:center}
.sa-seg a:first-child{border-left:0}.sa-seg a.on{background:#f1f1ee;color:var(--ink);font-weight:600}
.sa-seg a:hover{background:var(--card2);text-decoration:none}
.sa-search{display:flex;gap:6px;align-items:center;margin:6px 0}
.sa-search-f{display:flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:var(--radius-sm);
background:var(--card);padding:0 10px;min-height:34px;color:var(--faint)}
.sa-search-f input{border:0;outline:0;background:none;font:400 14px var(--font-text);color:var(--ink);width:200px;min-height:32px}
.sa-search-f:focus-within{outline:2px solid var(--accent);outline-offset:1px}
.sa-table-w{border:1px solid var(--line);border-radius:var(--radius);background:var(--card);overflow:hidden}
.sa-table{width:100%;border-collapse:collapse;table-layout:auto}
.sa-table th{font-size:12.5px;font-weight:500;color:var(--soft);text-align:left;padding:9px 14px;background:var(--card2);
border-bottom:1px solid var(--line);white-space:nowrap}
.sa-table td{padding:10px 14px;border-bottom:1px solid var(--line);font-size:13.5px;vertical-align:top;
overflow-wrap:anywhere;color:var(--ink2)}
.sa-table tr:last-child td{border-bottom:0}.sa-table .num{text-align:right;font-variant-numeric:tabular-nums}
.sa-table tr.sa-link:hover td{background:#fafaf8}.sa-rowlink{color:inherit;text-decoration:none;display:block}
.sa-rowlink:hover{text-decoration:none}
.sa-cell-t{color:var(--ink);font-weight:550}.sa-cell-s{font-size:12.5px;color:var(--faint);margin-top:1px}
.sa-empty{text-align:center;padding:44px 20px;border:1px dashed var(--line2);border-radius:var(--radius);background:var(--card)}
.sa-empty b{display:block;color:var(--ink);font-size:15px;margin-bottom:4px}.sa-empty p{margin:0 auto 12px;max-width:52ch;
color:var(--soft)}.sa-empty>:last-child{margin-bottom:0}
.sa-src{font-size:12px;color:var(--faint);margin:10px 0 0}
.sa-trust{display:flex;gap:10px;align-items:flex-start;background:var(--card2);border-radius:var(--radius-sm);padding:10px 12px;
font-size:13px;color:var(--ink2);margin:12px 0 0}
.sa-trust code{color:var(--ink);background:none;padding:0;font-size:12.5px}.sa-trust .sa-ic{margin-top:2px;color:var(--ink)}
.sa-panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;min-width:0}
.sa-panel>header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}
.sa-panel h2{font-size:14.5px;margin:0;font-weight:600}.sa-panel>header p{margin:2px 0 0;font-size:12.5px;color:var(--faint)}
.sa-hb{display:grid;grid-template-columns:minmax(80px,36%) 1fr 70px;gap:10px;align-items:center;font-size:13px;margin:7px 0}
.sa-hb-l{color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sa-hb-t{height:8px;border-radius:4px;background:var(--bg2);overflow:hidden}.sa-hb-t i{display:block;height:100%;
background:var(--ink2);border-radius:4px}
.sa-hb-n{text-align:right;color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.sa-hb-n small{font-weight:400;color:var(--faint);margin-left:6px}
.sa-chart{width:100%;height:auto;display:block}.sa-chart line{stroke:var(--line)}
.sa-chart text{fill:var(--soft);font-size:11px;font-family:var(--font-text)}
.sa-chart rect.b{fill:var(--accent)}.sa-chart.yn rect.b{fill:#3f9a62}.sa-chart rect.b.no2{fill:#d0544a}
.sa-chart rect.b.z{fill:var(--line2)}
@media (max-width:640px){
.sa-ph{margin-bottom:16px}.sa-ph h1{font-size:20px}.sa-ph-a{width:100%}
.sa-tiles{grid-template-columns:1fr 1fr}.sa-tile{padding:12px 14px}.sa-tile-v{font-size:19px}
.sa-search{width:100%}.sa-search-f{flex:1}.sa-search-f input{width:100%}
.sa-tabs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;max-width:100%}.sa-tab{white-space:nowrap}
.sa-bar{flex-direction:column;align-items:stretch;gap:4px}
.sa-table-w{border:0;background:none;border-radius:0}.sa-table thead{display:none}
.sa-table,.sa-table tbody,.sa-table tr,.sa-table td{display:block;width:100%}
.sa-table tr{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);margin:0 0 10px;padding:6px 0}
.sa-table td{border:0;padding:4px 14px;display:flex;gap:12px;justify-content:space-between;align-items:baseline;text-align:right}
.sa-table td:before{content:attr(data-l);color:var(--faint);font-size:12px;text-align:left;flex:none}
.sa-table td:first-child{display:block;text-align:left;padding-top:8px}.sa-table td:first-child:before{content:none}
.sa-table td[data-l='']:before{content:none}.sa-table .num{text-align:right}
.sa-hb{grid-template-columns:minmax(70px,40%) 1fr 64px}}
"""
