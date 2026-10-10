"""Read-only web dashboard for one or more products.

    sayso dashboard --config acme.toml [--config other.toml] [--port 8765]
    sayso dashboard --demo            # fake data, safe anywhere

What it guarantees:
- It NEVER writes. It reads the state directory with plain file reads (no
  locks, no temp files, no mkdir) and never builds the loop's runtime, so it
  cannot move a card, open a vote or send anything. GET and HEAD only; every
  other method gets 405.
- It binds to loopback only. There is no login, so it refuses any other host.
- Every value from state or from a customer is HTML-escaped. Customer email is
  untrusted input and is shown as text, never as markup.

Stage tags come from the board, which owns them. With fake adapters they are
read from the fake world file. With a real board they are read through the
board adapter's get_tags (a read call); if that fails the stage shows as
"unreadable" rather than a guess.
"""
from __future__ import annotations

import hashlib
import html
import re
import ipaddress
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from sayso import stages
from sayso.config import Config
from sayso.store import parse_iso

HEALTH_JOBS = ("intake", "draft", "votes", "sender", "money", "dev", "qa", "release", "close", "reconcile", "stall", "health")
STAGE_ORDER = (stages.NEW, stages.IN_SUPPORT, stages.IN_DEV, stages.IN_QA, stages.AWAITING, stages.DONE,
               stages.BLOCKED)


class RemoteBindRefused(ValueError):
    """The dashboard has no login, so it only listens on this machine."""


# -- reading (never writing) ---------------------------------------------------

def _read_json(path: Path, default):
    """Plain read. Missing -> default. Corrupt -> raise (never shown as empty)."""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class Snapshot:
    """Everything one page needs, read once from one product's state directory."""

    def __init__(self, config: Config, board_reader: Callable[[str], list[str]] | None = None,
                 now: datetime | None = None):
        self.config = config
        root = config.state_dir
        self.root = root
        self.cases: dict[str, Any] = _read_json(root / "cases.json", {})
        self.votes: dict[str, Any] = _read_json(root / "votes.json", {})
        self.attempts: dict[str, Any] = _read_json(root / "attempts.json", {})
        self.beats: dict[str, Any] = _read_json(root / "heartbeats.json", {})
        self.journal = _read_journal(root / "journal.jsonl")
        self.paused = (root / "PAUSED").exists()
        self.pause_reason = (root / "PAUSED").read_text(encoding="utf-8").strip() if self.paused else ""
        self.world = _read_json(root / "fake-world.json", None)
        self.web_votes: dict[str, Any] = _read_json(root / "web-votes.json", {})
        self.now = now or datetime.now(timezone.utc)
        self._board_reader = board_reader
        self.label: str | None = None
        self.console: dict | None = None  # set by the console when a signed-in operator is viewing

    # tags / messages come from the board, which owns them
    def tags(self, card_id: str) -> list[str]:
        if self.world is not None:
            card = self.world.get("cards", {}).get(card_id)
            return list(card["tags"]) if card else ["unreadable"]
        if self._board_reader is None:
            return ["unreadable"]
        try:
            return list(self._board_reader(card_id))
        except Exception:  # noqa: BLE001 - show unknown, never guess
            return ["unreadable"]

    def card_messages(self, card_id: str) -> list[str]:
        if self.world is not None:
            return list(self.world.get("cards", {}).get(card_id, {}).get("messages", []))
        return []

    def stage(self, card_id: str) -> str:
        tags = self.tags(card_id)
        try:
            return stages.stage_of(tags)
        except Exception:  # noqa: BLE001
            return "unreadable"

    def open_votes(self) -> list[dict]:
        return sorted((v for v in self.votes.values() if v.get("status") == "open"),
                      key=lambda v: v.get("opened_at", ""))

    def draft_text(self, vote: dict) -> str | None:
        path = (vote.get("spec") or {}).get("path")
        if not path:
            return None
        p = Path(path)
        try:
            p.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None  # only files inside this product's state dir are shown
        return p.read_text(encoding="utf-8") if p.exists() else None

    def health(self) -> list[dict]:
        rows = []
        for job in HEALTH_JOBS:
            beat = self.beats.get(job)
            if not beat:
                rows.append({"job": job, "state": "never", "at": "", "result": ""})
                continue
            at = parse_iso(beat["at"])
            limit = timedelta(seconds=3 * self.config.jobs.get(job, 600))
            state = "stale" if self.now - at > limit else "ok"
            rows.append({"job": job, "state": state, "at": beat["at"], "result": beat.get("result", "")})
        return rows

    def money_rows(self) -> list[dict]:
        rows = []
        for key, v in sorted(self.votes.items()):
            if v.get("kind") != "money":
                continue
            att = self.attempts.get(key, {})
            rows.append({"key": key, "case_key": v.get("case_key"), "subject": v.get("subject"),
                         "spec": v.get("spec") or {}, "vote": v.get("status"),
                         "execution": att.get("status", "-"), "decided_by": v.get("decided_by", "")})
        return rows

    def as_json(self) -> dict:
        return {"product": self.config.slug, "paused": self.paused,
                "stages": dict(Counter(self.stage(r["card_id"]) for r in self.cases.values() if r.get("card_id"))),
                "open_votes": [{k: v.get(k) for k in ("key", "kind", "subject", "question", "opened_at")}
                               for v in self.open_votes()],
                "health": self.health()}


# -- rendering ------------------------------------------------------------------

def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# -- presentation helpers -------------------------------------------------------

KIND_LABEL = {"reply": "Send reply", "money": "Money", "merge": "Merge fix", "close": "Close case"}
STATUS_LABEL = {"open": "Waiting", "approved": "Approved", "done": "Done", "void": "Voided",
                "declined": "Declined", "expired": "Expired", "running": "Running", "paused": "Paused",
                "ok": "Healthy", "stale": "Stale", "never": "Not run yet", "pending": "Pending",
                "unreadable": "Unreadable"}
EXEC_LABEL = {"SENT_AND_VERIFIED": "Sent · verified", "VERIFIED_WITH_PROVIDER": "Verified with provider",
              "UNVERIFIED": "Unverified", "pending": "In progress"}
TONE = {"open": "amber", "pending": "amber", "never": "grey", "approved": "violet", "done": "green", "ok": "green",
        "running": "green", "SENT_AND_VERIFIED": "green", "VERIFIED_WITH_PROVIDER": "green", "void": "grey",
        "expired": "grey", "declined": "red", "stale": "red", "UNVERIFIED": "red", "paused": "amber",
        "unreadable": "red"}
STAGE_TONE = {stages.NEW: "blue", stages.IN_SUPPORT: "indigo", stages.IN_DEV: "violet", stages.IN_QA: "cyan",
              stages.AWAITING: "amber", stages.DONE: "green", stages.BLOCKED: "red"}
STAGE_HINT = {stages.NEW: "Just arrived", stages.IN_SUPPORT: "Being answered", stages.IN_DEV: "A fix is being coded",
              stages.IN_QA: "Fix ready, checking", stages.AWAITING: "Needs a human Yes", stages.DONE: "Closed",
              stages.BLOCKED: "Stuck, needs a person"}
JOB_HINT = {"intake": "Turns new email into cards", "draft": "Writes reply drafts", "votes": "Counts Yes/No votes",
            "sender": "Sends approved replies, then proves them", "money": "Runs approved refunds, then proves them",
            "dev": "Starts one coding run per dev card", "release": "Merges the exact approved PR head",
            "close": "Asks to close quiet cases", "reconcile": "Reports drift, never repairs",
            "health": "Watches the other jobs"}

ICONS = {
    "inbox": '<path d="M3 13h5l1.5 2.5h5L16 13h5M5 5h14l2 8v6H3v-6z"/>',
    "board": '<path d="M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v13h-4z"/>',
    "money": '<path d="M3 6h18v12H3zM12 9a3 3 0 100 6 3 3 0 000-6zM6 9v6M18 9v6"/>',
    "health": '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    "audit": '<path d="M6 3h9l4 4v14H6zM9 11h7M9 15h7M9 7h4"/>',
    "lock": '<path d="M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 017 0v3"/>',
    "grid": '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
    "mail": '<path d="M3 6h18v12H3zM3 7l9 6 9-6"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
}


def icon(name: str) -> str:
    return (f'<svg class=ic viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, "")}</svg>')


def pill(value: Any, label: str | None = None) -> str:
    raw = "" if value is None else str(value)
    text = label or STATUS_LABEL.get(raw) or EXEC_LABEL.get(raw) or raw
    return f'<span class="pill t-{TONE.get(raw, "grey")}" title="{e(raw)}">{e(text)}</span>'


def exec_pill(value: Any) -> str:
    """Execution proof: friendly words plus the exact machine status."""
    if not value or value == "-":
        return '<span class=mute>—</span>'
    return f'{pill(value)} <code class=raw>{e(value)}</code>'


def stage_pill(stage: str) -> str:
    return f'<span class="stage s-{STAGE_TONE.get(stage, "grey")}"><i></i>{e(stage)}</span>'


def ref_time(s: "Snapshot") -> datetime:
    """'Now' for ages. A demo clock can run ahead of the wall clock; then use its newest event."""
    latest = None
    for j in s.journal:
        try:
            at = parse_iso(j["at"])
        except Exception:  # noqa: BLE001
            continue
        latest = at if latest is None or at > latest else latest
    return latest if latest is not None and latest > s.now else s.now


def ago(value: Any, ref: datetime) -> str:
    try:
        at = parse_iso(str(value))
    except Exception:  # noqa: BLE001
        return e(value)
    secs = int((ref - at).total_seconds())
    if secs < 0:
        text = "just now"
    elif secs < 60:
        text = "just now"
    elif secs < 3600:
        text = f"{secs // 60} min ago"
    elif secs < 86400:
        text = f"{secs // 3600} h ago"
    else:
        text = f"{secs // 86400} d ago"
    return f'<time datetime="{e(value)}" title="{e(when(value))}">{text}</time>'


def when(value: Any) -> str:
    try:
        return parse_iso(str(value)).strftime("%d %b %Y, %H:%M UTC")
    except Exception:  # noqa: BLE001
        return "" if value is None else str(value)


def initials(addr: Any) -> str:
    raw = str(addr or "?")
    if raw.startswith("Customer ") and len(raw.split()) == 2 and raw.split()[1].isalnum():
        return raw.split()[1][:2].upper()          # "Customer B730" -> "B7"
    if not any(ch.isalnum() for ch in raw.split("@")[0]) or "(" in raw:
        return "?"
    name = raw.split("@")[0].replace(".", " ").replace("_", " ").split()
    return "".join(p[0] for p in name[:2]).upper() or "?"


def avatar(addr: Any) -> str:
    hue = int(hashlib.sha256(str(addr).encode()).hexdigest()[:4], 16) % 360
    return f'<span class=av style="--h:{hue}">{e(initials(addr))}</span>'


def money_text(spec: dict) -> str:
    if spec.get("description"):
        return str(spec["description"])
    if spec.get("action") == "refund":
        return f"refund {spec.get('amount', 0) / 100:.2f} {str(spec.get('currency', '')).upper()}"
    if spec.get("action") == "cancel_subscription":
        return "cancel subscription"
    return str(spec.get("action", "money action"))


def operator(s: "Snapshot", who: Any) -> str:
    return s.config.operators.get(str(who), "" if who is None else str(who))


def message_html(text: str) -> str:
    """A card message as text: a leading **Title** becomes a heading; no raw markdown shows."""
    raw = str(text)
    m = re.match(r"\*\*(.+?)\*\*\s*(.*)$", raw, re.S)
    if m:
        title, rest = m.group(1), m.group(2).strip()
        rest = re.sub(r"\n{3,}", "\n\n", rest)
        return f"<div class=mt>{e(title)}</div>{e(rest.replace('**', ''))}"
    return e(raw.replace("**", ""))


def event_text(s: "Snapshot", j: dict) -> str:
    ev = j.get("event")
    key = str(j.get("key") or "")
    kind = KIND_LABEL.get(key.split(":")[0], key.split(":")[0]) if key else ""
    if ev == "case_opened":
        return "Case opened from the customer's email"
    if ev == "mail_appended":
        return "Customer wrote again; live votes were voided"
    if ev == "vote_opened":
        return f"Vote opened · {kind}"
    if ev == "vote_approved":
        return f"{kind} approved by {operator(s, j.get('decided_by'))}"
    if ev == "vote_declined":
        return f"{kind} declined by {operator(s, j.get('decided_by'))}"
    if ev == "vote_void":
        return f"{kind} vote voided · {j.get('void_reason') or 'material changed'}"
    if ev == "vote_expired":
        return f"{kind} vote expired"
    if ev == "send_attempt":
        return "Sending the approved reply"
    if ev == "vote_done":
        r = j.get("receipt") or {}
        if r.get("status"):
            return f"{kind} done · {EXEC_LABEL.get(r['status'], r['status'])}"
        if r.get("provider_id"):
            return f"{kind} done · provider receipt {r.get('provider_id')}"
        if j.get("merged_head"):
            return f"{kind} done · merged at {str(j['merged_head'])[:12]}"
        return f"{kind} done"
    if ev == "alert":
        return f"Alert · {j.get('text', '')}"
    return str(ev)


EVENT_TONE = {"case_opened": "blue", "mail_appended": "blue", "vote_opened": "amber", "vote_approved": "violet",
              "vote_done": "green", "vote_void": "grey", "vote_declined": "red", "vote_expired": "grey",
              "alert": "red", "send_attempt": "indigo"}


CSS = """
:root{--ink:#1f1b24;--text:#2b2630;--mute:#6f6775;--faint:#9a929f;--line:#ece8e1;--line2:#f3f0ea;
--bg:#ffffff;--side:#fbfaf7;--hover:#f4f1ec;--accent:#e8653b;--accent-soft:#fdf0ea;--plum:#27212d;
--green:#2f7d4f;--green-bg:#eaf5ee;--amber:#9a5b00;--amber-bg:#fdf3e2;--red:#b93a32;--red-bg:#fbecea;
--violet:#6a4bb8;--violet-bg:#f1edfa;--blue:#2f6db5;--blue-bg:#ebf2fb;--cyan:#1f7a86;--cyan-bg:#e8f5f6;
--indigo:#c2521f;--indigo-bg:#fdf0ea;--grey:#6f6775;--grey-bg:#f2f0ec;--r:10px}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,
"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh;
-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}main a:not(.row):not(.pc):not(.btn){color:var(--accent)}main a:hover{text-decoration:underline}
code,.mono,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
code{background:var(--grey-bg);padding:1px 5px;border-radius:5px;color:var(--mute)}
.ic{width:17px;height:17px;flex:none;opacity:.75}
aside{background:var(--side);border-right:1px solid var(--line);padding:18px 12px;position:sticky;top:0;height:100vh;
display:flex;flex-direction:column;gap:20px;overflow-y:auto}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:650;font-size:15px;padding:4px 8px}
.logo{width:26px;height:26px;display:grid;place-items:center;flex:none}.logo svg{width:26px;height:26px;display:block}
.brand small{display:block;font-weight:400;color:var(--faint);font-size:11.5px;line-height:1.3}
.navlabel{font-size:11.5px;color:var(--faint);padding:0 10px;margin-bottom:4px;font-weight:500}
nav a,.prod a{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:7px;color:var(--mute);font-weight:500;
font-size:14px}
nav a:hover,.prod a:hover{background:var(--hover);color:var(--ink);text-decoration:none}
nav a.on,.prod a.on{background:var(--hover);color:var(--ink)}nav a.on .ic{opacity:1;color:var(--accent)}
nav a .n{margin-left:auto;color:var(--accent);font-size:12px;font-weight:650}
.prod a .dot{width:7px;height:7px;border-radius:99px;background:#5aa878;margin:0 5px}
.side-foot{margin-top:auto;font-size:12.5px;color:var(--faint);border-top:1px solid var(--line);padding:14px 10px 2px;
display:flex;gap:9px;align-items:flex-start}.side-foot b{color:var(--ink);font-weight:600;display:block}
main{padding:44px 56px 80px;max-width:1180px;width:100%}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:30px;flex-wrap:wrap}
.crumb{font-size:13px;color:var(--faint);margin-bottom:6px}.crumb a{color:var(--faint)!important}
h1{font-size:30px;line-height:1.2;margin:0;letter-spacing:-.02em;color:var(--ink);font-weight:700}
.sub{color:var(--mute);margin-top:6px;font-size:15px}
h2{font-size:15px;margin:40px 0 14px;color:var(--ink);font-weight:650;display:flex;align-items:center;gap:8px}
h2 .c,.count{color:var(--faint);font-weight:500;font-size:13px}
.ro{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--mute);padding:4px 0;margin-top:26px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0;border:1px solid var(--line);
border-radius:var(--r)}.metric{padding:18px 20px;border-left:1px solid var(--line)}.metric:first-child{border-left:0}
.metric .k{font-size:13px;color:var(--mute)}.metric .v{font-size:28px;font-weight:650;letter-spacing:-.02em;color:var(--ink);
margin-top:4px;line-height:1.2}.metric .h{font-size:12.5px;color:var(--faint);margin-top:2px}
.metric.hot .v{color:var(--accent)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}
.panel{border:1px solid var(--line);border-radius:var(--r);padding:20px 22px}.panel h3{margin:0 0 4px;font-size:14px;
color:var(--ink);font-weight:650}.panel .ph{font-size:12.5px;color:var(--faint);margin-bottom:16px}
.chart{width:100%;height:auto;display:block}.chart rect.b{fill:var(--accent)}.chart rect.b.no2{fill:#d4695f}.chart.yn rect.b:not(.no2):not(.z){fill:#5aa878}.chart rect.b.z{fill:var(--line)}
.chart text{fill:var(--mute);font-size:12px}.chart line{stroke:var(--line2)}
.hb{display:grid;grid-template-columns:minmax(90px,38%) 1fr 40px;gap:10px;align-items:center;font-size:13px;margin:7px 0}
.hb .l{color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hb .t{height:8px;border-radius:99px;
background:var(--line2);overflow:hidden}.hb .t i{display:block;height:100%;border-radius:99px;background:var(--accent)}
.hb .n{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.stack{display:flex;height:12px;border-radius:99px;overflow:hidden;background:var(--line2);margin:6px 0 14px}
.stack i{display:block;height:100%}.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--mute)}
.legend span{display:inline-flex;align-items:center;gap:7px}.legend b{color:var(--ink);font-weight:600}
.sw{width:9px;height:9px;border-radius:3px;display:inline-block}
.c-green{background:#5aa878}.c-red{background:#d4695f}.c-grey{background:#cfc8bf}.c-amber{background:#e8a33b}
.c-violet{background:#8d74cf}.c-accent{background:var(--accent)}
.notmeasured{font-size:12.5px;color:var(--faint);margin-top:26px}
.card{border:1px solid var(--line);border-radius:var(--r);background:#fff}.pad{padding:18px 20px}
.mute{color:var(--mute)}.faint{color:var(--faint)}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:500;white-space:nowrap;color:var(--mute)}
.pill:before{content:'';width:7px;height:7px;border-radius:99px;background:currentColor}
.raw{color:var(--faint);font-size:11px;background:none;padding:0}
.t-green{color:var(--green)}.t-amber{color:var(--amber)}.t-red{color:var(--red)}.t-violet{color:var(--violet)}
.t-grey{color:var(--faint)}.t-blue{color:var(--blue)}.t-indigo{color:var(--indigo)}
.stage{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:500;color:var(--mute)}
.stage i{width:8px;height:8px;border-radius:99px;background:var(--grey)}
.s-blue i{background:#5b8fd0}.s-indigo i{background:var(--accent)}.s-violet i{background:#8d74cf}.s-cyan i{background:#4aa3ae}
.s-amber i{background:#e8a33b}.s-green i{background:#5aa878}.s-red i{background:#d4695f}
.av{--h:220;width:26px;height:26px;border-radius:99px;display:inline-grid;place-items:center;font-size:10.5px;font-weight:650;
flex:none;color:var(--mute);background:var(--grey-bg)}
.banner{display:flex;gap:10px;align-items:center;padding:12px 16px;border-radius:var(--r);margin-bottom:24px;
background:var(--amber-bg);color:#6b4200}
.note{display:flex;gap:10px;align-items:center;font-size:13.5px;color:var(--mute);margin:0 0 18px}
.item{border-bottom:1px solid var(--line);padding:26px 0}.item:first-of-type{border-top:1px solid var(--line)}
.item-head{display:flex;gap:14px;align-items:flex-start}.item-head .who{flex:1;min-width:0}
.eyebrow{font-size:12.5px;color:var(--faint)}.eyebrow b{color:var(--accent);font-weight:600}
.item .title{font-size:17px;font-weight:650;color:var(--ink);margin:2px 0 2px;line-height:1.35}.q{color:var(--mute)}
.meta{text-align:right;font-size:12.5px;color:var(--faint);white-space:nowrap;display:flex;flex-direction:column;gap:6px;
align-items:flex-end}
.item-body{padding:16px 0 0 40px}.item-foot{padding:14px 0 0 40px;font-size:13px;color:var(--faint);display:flex;gap:14px;
flex-wrap:wrap}
.mail{background:var(--side);border-radius:var(--r);overflow:hidden}
.mail .mh{padding:12px 18px 0;font-size:12.5px;color:var(--faint);display:grid;grid-template-columns:auto 1fr;gap:1px 12px}
.mail .mh b{font-weight:500}.mail .mb{padding:12px 18px 16px;white-space:pre-wrap;word-break:break-word;font-size:14.5px;
line-height:1.7;color:var(--text);max-width:72ch}
.amount{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.amount .big{font-size:15px;font-weight:650;
letter-spacing:-.02em;color:var(--ink)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;font-size:13.5px;margin-top:10px}
.kv dt{color:var(--faint)}.kv dd{margin:0;color:var(--text);min-width:0;overflow-wrap:anywhere}
details{margin-top:10px}summary{cursor:pointer;color:var(--mute);font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--side);border-radius:8px;padding:12px 14px;margin:8px 0 0}
.empty{padding:56px 20px;text-align:center;color:var(--mute)}.empty b{display:block;color:var(--ink);font-size:17px;
margin-bottom:4px}
.act{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:18px 0 0 40px}
.act button,form.inline button,.btn{border:0;border-radius:8px;padding:9px 16px;font:600 13.5px/1 inherit;cursor:pointer}
.act .faint{font-size:12.5px;flex:1 1 240px}
.yes{background:var(--plum);color:#fff}.yes:hover{background:#000}
.no{background:#fff;color:var(--text);box-shadow:inset 0 0 0 1px var(--line)}.no:hover{background:var(--hover)}
.ghost{background:var(--hover);color:var(--ink)}form.inline{display:inline-block;margin:0 0 22px}
.link{background:none;border:0;color:var(--faint);padding:4px 0 0;cursor:pointer;font:inherit;text-decoration:underline}
.ballots{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:14px 0 0 40px;font-size:12.5px}
.ballot{border-radius:99px;padding:2px 10px;font-weight:600}.ballot small{font-weight:400;opacity:.8}
.b-yes{background:var(--green-bg);color:var(--green)}.b-no{background:var(--red-bg);color:var(--red)}
.group{border-top:1px solid var(--line);padding:4px 0}.group:last-of-type{border-bottom:1px solid var(--line)}
.group>summary{list-style:none;display:flex;align-items:center;gap:10px;padding:12px 4px;color:var(--ink);font-weight:600;
font-size:14px}.group>summary::-webkit-details-marker{display:none}
.group>summary:before{content:'›';color:var(--faint);width:12px;display:inline-block;text-align:center;transition:transform .15s}
.group[open]>summary:before{transform:rotate(90deg)}.group .hint{color:var(--faint);font-weight:400;font-size:13px}
.group.empty>summary{color:var(--faint);font-weight:500;pointer-events:none}.group.empty>summary:before{opacity:0}
.rows{margin:0 0 10px}
.row{display:grid;grid-template-columns:minmax(0,1fr) 190px 150px 90px;gap:16px;align-items:center;padding:10px 12px 10px 24px;
border-radius:8px;color:var(--text)}.row:hover{background:var(--hover);text-decoration:none}
.row .tt{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.row .cu{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mute);min-width:0}
.row .cu span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .kd{font-size:12.5px;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .ag{font-size:12.5px;color:var(--faint);text-align:right}
.tags{display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}.tag{font-size:11.5px;background:var(--grey-bg);color:var(--mute);
border-radius:5px;padding:0 7px}.tag.w{background:var(--grey-bg);color:var(--ink)}
.case{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:48px;align-items:start}
.props{position:sticky;top:24px}.props h3{font-size:15px;color:var(--ink);margin:30px 0 10px;font-weight:650}
.props h3:first-child{margin-top:0}
.prop{display:grid;grid-template-columns:96px 1fr;gap:10px;padding:6px 0;font-size:13.5px}.prop dt{color:var(--faint)}
.prop dd{margin:0;min-width:0;overflow-wrap:anywhere;color:var(--ink)}
.thread h2:first-child{margin-top:0}
.ev{display:flex;gap:14px;margin-bottom:12px}.ev .dot{width:7px;height:7px;border-radius:99px;margin-top:9px;flex:none;
background:#cfc8bf}.ev .dot.t-green{background:#5aa878}.ev .dot.t-amber{background:#e8a33b}.ev .dot.t-violet{background:#8d74cf}
.ev .dot.t-blue{background:#5b8fd0}.ev .dot.t-red{background:#d4695f}.ev .dot.t-indigo{background:var(--accent)}
.ev .body{flex:1;min-width:0}.ev .when{font-size:12px;color:var(--faint)}
.msg .mt{font-weight:650;margin-bottom:4px;white-space:normal}.msg{white-space:pre-wrap;word-break:break-word;font-size:14.5px;
line-height:1.7}.msg.cust{background:var(--side);border-radius:var(--r);padding:16px 18px}
.vote{padding:10px 0;border-top:1px solid var(--line2);font-size:13.5px}.vote:first-of-type{border-top:0}
.vote .vw{font-size:12.5px;color:var(--faint)}.vote .vh{display:flex;justify-content:space-between;gap:8px}
table{width:100%;border-collapse:collapse;table-layout:fixed}td{overflow-wrap:anywhere}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);
vertical-align:top;font-size:13.5px}th{color:var(--faint);font-weight:500;font-size:12.5px}
.jobs{border-top:1px solid var(--line)}.job{display:grid;grid-template-columns:18px 170px 1fr 130px;gap:14px;padding:14px 4px;
border-bottom:1px solid var(--line);align-items:start;font-size:13.5px}.job .led{width:8px;height:8px;border-radius:99px;
margin-top:7px}.led.t-green{background:#5aa878}.led.t-red{background:#d4695f}.led.t-grey{background:#cfc8bf}
.job b{text-transform:capitalize;color:var(--ink);font-weight:600}.job .res{font-size:12px;color:var(--faint);margin-top:2px;
word-break:break-word}
.products{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.pc{display:block;color:var(--text)}.pc:hover{background:var(--side);text-decoration:none}
.pc .prow{display:flex;gap:22px;margin-top:14px}.pc .prow div{font-size:12.5px;color:var(--faint)}.pc .prow b{display:block;
font-size:20px;color:var(--ink);font-weight:650}
@media (max-width:1000px){.grid2{grid-template-columns:1fr}.row{grid-template-columns:minmax(0,1fr) 90px}.row .cu,.row .kd{display:none}
.job{grid-template-columns:18px 1fr}.job>div:nth-child(3),.job>div:nth-child(4){grid-column:2}}
@media (max-width:860px){body{grid-template-columns:1fr}aside{position:sticky;top:0;z-index:5;height:auto;gap:8px;padding:10px 12px;
border-right:0;border-bottom:1px solid var(--line)}aside .navlabel,.side-foot,.prod{display:none}nav{display:flex;flex-wrap:wrap;gap:2px 4px}
nav a{white-space:nowrap;padding:6px 8px}main{padding:24px 18px 48px}h1{font-size:24px}.case{grid-template-columns:1fr;gap:28px}
.props{position:static}.metrics{grid-template-columns:1fr 1fr}.metric:nth-child(3){border-left:0}.metric:nth-child(n+3){border-top:1px solid var(--line)}
.item-body,.item-foot,.act,.ballots{padding-left:0}.meta{display:none}}
"""

NAV = (("", "Inbox", "inbox"), ("overview", "Overview", "chart"), ("board", "Cases", "board"),
       ("money", "Money", "money"), ("health", "Health", "health"), ("audit", "Audit log", "audit"))
ICONS["chart"] = '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'

SAYSO_MARK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true"> <path d="M14 8h30a10 10 0 0 1 10 10v16a10 10 0 0 1-10 10H30l-11 9v-9h-5A10 10 0 0 1 4 34V18A10 10 0 0 1 14 8z" fill="#e8653b"/> <path d="M19 27l8 8 16-17" fill="none" stroke="#27212d" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/> </svg>'
SAYSO_ICON = __import__("urllib.parse").parse.quote(SAYSO_MARK.replace('aria-hidden="true"', ''))
RO_TEXT = "Read-only · cannot send, charge or merge"
CON_TEXT = "Signed in as an operator"
VIEW_TEXT = "Signed in · view only for this product"


def page(title: str, body: str, products: list[str], current: str | None, tab: str = "",
         snap: "Snapshot | None" = None) -> str:
    nav = ""
    if current:
        waiting = len(snap.open_votes()) if snap is not None else 0
        for name, label, ic in NAV:
            href = f"/p/{e(current)}/{name}" if name else f"/p/{e(current)}/"
            badge = f'<span class=n>{waiting}</span>' if name == "" and waiting else ""
            if name == "health" and snap is not None and any(h["state"] == "stale" for h in snap.health()):
                badge = "<span class=n title='a job is stale' style='color:var(--red)'>●</span>"
            nav += f'<a class="{"on" if tab == name else ""}" href="{href}">{icon(ic)}{label}{badge}</a>'
        nav = f'<div><nav>{nav}</nav></div>'
    prods = "".join(f'<a class="{"on" if p == current else ""}" href="/p/{e(p)}/"><span class=dot></span>{e(p)}</a>'
                    for p in products)
    mode = getattr(snap, "label", None) or "Live view"
    con = getattr(snap, "console", None)
    stamp = f"<br>{e(when(snap.now.isoformat()))}" if snap is not None and mode != "Live view" else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><meta name=robots content=noindex>"
            f"<title>{e(title)} · Sayso</title><link rel=icon href='data:image/svg+xml,{SAYSO_ICON}'><style>{CSS}</style></head><body>"
            f"<aside><a class=brand href='/'><span class=logo>{SAYSO_MARK}</span><span>Sayso"
            f"<small>nothing goes out without your say-so</small></span></a>{nav}"
            f"<div><div class=navlabel>Products</div><div class=prod>"
            f"<a class='{'on' if current is None else ''}' href='/'>{icon('grid')}All products</a>{prods}</div></div>"
            + (f"<div class=side-foot>{avatar(con['user'])}<span><b>{e(con['user'])}</b>Operator · {e(mode)}"
               f"<form method=post action=/logout><input type=hidden name=csrf value='{e(con['csrf'])}'>"
               f"<button class=link>Sign out</button></form></span></div></aside>" if con else
               f"<div class=side-foot>{icon('lock')}<span><b>Read-only</b>{e(mode)}{stamp}</span></div></aside>")
            + f"<main>{body.replace(RO_TEXT, (CON_TEXT if con.get('can_vote', True) else VIEW_TEXT)) if con else body}"
            f"</main></body></html>")


def head(title: str, sub: str = "", crumb: str = "") -> str:
    return (f"<div class=top><div>{f'<div class=crumb>{crumb}</div>' if crumb else ''}<h1>{e(title)}</h1>"
            f"{f'<div class=sub>{sub}</div>' if sub else ''}</div>"
            f"<span class=ro>{icon('lock')}{RO_TEXT}</span></div>")


def _strip_subject(text: str) -> str:
    """Drafts may start with their own 'Subject:' line; the header already shows it."""
    return re.sub(r"\ASubject:[^\n]*\n+", "", text)


def _paused_banner(s: Snapshot) -> str:
    if not s.paused:
        return ""
    return (f'<div class=banner>{icon("pause")}<div><b>Paused.</b> {e(s.pause_reason)} — jobs that act are '
            f'stopped. Resume with <code>sayso resume --config …</code></div></div>')


def _live_cases(s: Snapshot):
    return [(k, r) for k, r in s.cases.items() if r.get("card_id") and not r.get("folded_into")]


def _metric(k: str, v: Any, h: str = "", hot: bool = False) -> str:
    return (f"<div class='metric{' hot' if hot else ''}'><div class=k>{e(k)}</div><div class=v>{e(v)}</div>"
            f"<div class=h>{e(h)}</div></div>")


def render_home(snaps: dict[str, Snapshot]) -> str:
    cards = ""
    for slug, s in snaps.items():
        stale = sum(1 for h in s.health() if h["state"] == "stale")
        open_cases = sum(1 for _, r in _live_cases(s) if s.stage(r["card_id"]) != stages.DONE)
        cards += (f"<a class='card pad pc' href='/p/{e(slug)}/'><div style='display:flex;align-items:center;gap:10px'>"
                  f"<b style='font-size:16px;color:var(--ink)'>{e(s.config.name)}</b><span style='margin-left:auto'>"
                  f"{pill('paused') if s.paused else pill('running')} {pill('stale', 'Jobs stale') if stale else ''}"
                  f"</span></div><div class=faint style='font-size:12.5px'>{e(s.config.support_address)}</div>"
                  f"<div class=prow><div><b>{len(s.open_votes())}</b>decisions waiting</div>"
                  f"<div><b>{open_cases}</b>open cases</div><div><b>{len(s.cases)}</b>cases total</div></div></a>")
    body = (head("All products", "Every product runs the same loop from its own settings file.")
            + f"<div class=products>{cards}</div>")
    first = next(iter(snaps.values()), None)
    return page("Products", body, list(snaps), None, snap=first)


# -- numbers for the overview (all read from state; nothing is estimated) -----------

def _at(value: Any) -> datetime | None:
    try:
        return parse_iso(str(value))
    except Exception:  # noqa: BLE001
        return None


def first_decisions(s: Snapshot) -> list[tuple[str, datetime, str]]:
    """(key, when, "yes"|"no") of each vote's FIRST Yes/No, from the journal. A decision counts once."""
    seen: dict[str, tuple[datetime, str]] = {}
    for j in s.journal:
        key, at = str(j.get("key") or ""), _at(j.get("at"))
        if key and at is not None and j.get("event") in ("vote_approved", "vote_declined") and key not in seen:
            seen[key] = (at, "yes" if j["event"] == "vote_approved" else "no")
    return [(k, at, a) for k, (at, a) in seen.items()]


def decision_times(s: Snapshot) -> list[float]:
    """Hours from a vote opening to its first Yes/No, from the append-only journal."""
    opened: dict[str, datetime] = {}
    done: set[str] = set()
    out = []
    for j in s.journal:
        key, at = str(j.get("key") or ""), _at(j.get("at"))
        if not key or at is None:
            continue
        if j.get("event") == "vote_opened":
            opened.setdefault(key, at)
        elif j.get("event") in ("vote_approved", "vote_declined") and key in opened and key not in done:
            done.add(key)
            out.append(max((at - opened[key]).total_seconds() / 3600, 0.0))
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{max(int(h * 60), 1)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


def daily(s: Snapshot, dates: list[datetime | None], days: int = 14) -> list[tuple[str, int]]:
    end = ref_time(s).date()
    counts = Counter(d.date() for d in dates if d is not None)
    out = []
    for i in range(days - 1, -1, -1):
        day = end - timedelta(days=i)
        out.append((day.strftime("%d %b"), counts.get(day, 0)))
    return out


def bar_chart(series: list[tuple[str, int]], w: int = 560, h: int = 150,
              second: list[tuple[str, int]] | None = None) -> str:
    """Bars per day. With `second`, each bar is stacked: series (accent) under second (red)."""
    n = len(series)
    extra = dict(second or [])
    top = max([v + extra.get(k, 0) for k, v in series] + [1])
    gap, base, headroom = 6, h - 22, 18
    bw = (w - gap * (n - 1)) / max(n, 1)
    parts = [f"<line x1='0' x2='{w}' y1='{base + .5}' y2='{base + .5}'/>"]
    for i, (label, v) in enumerate(series):
        v2 = extra.get(label, 0)
        bh = (base - headroom) * v / top
        bh2 = (base - headroom) * v2 / top
        x = i * (bw + gap)
        cls = "b" if v or v2 else "b z"
        parts.append(f"<rect class='{cls}' x='{x:.1f}' y='{base - max(bh, 2):.1f}' width='{bw:.1f}' "
                     f"height='{max(bh, 2):.1f}' rx='3'><title>{e(label)}: {v}</title></rect>")
        if v2:
            bh2 = max(bh2, 4)
            parts.append(f"<rect class='b no2' x='{x:.1f}' y='{base - bh - bh2:.1f}' width='{bw:.1f}' "
                         f"height='{bh2:.1f}' rx='3'><title>{e(label)}: {v2} No</title></rect>")
        if v or v2:
            parts.append(f"<text x='{x + bw / 2:.1f}' y='{base - bh - bh2 - 5:.1f}' text-anchor='middle'>{v + v2}</text>")
        if i in (0, n // 2, n - 1):
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            tx = x if i == 0 else (x + bw if i == n - 1 else x + bw / 2)
            parts.append(f"<text x='{tx:.1f}' y='{h - 5}' text-anchor='{anchor}'>{e(label)}</text>")
    return f"<svg class=chart viewBox='0 0 {w} {h}' role='img' aria-label='bar chart'>{''.join(parts)}</svg>"


def hbars(rows: list[tuple[str, int]]) -> str:
    top = max([v for _, v in rows] + [1])
    return "".join(f"<div class=hb><span class=l title='{e(k)}'>{e(k)}</span><span class=t><i style='width:"
                   f"{max(100 * v / top, 2 if v else 0):.0f}%'></i></span><span class=n>{v}</span></div>" for k, v in rows) \
        or "<p class=faint>No data yet.</p>"


OUTCOMES = (("Approved", ("approved", "done"), "c-green"), ("Declined", ("declined",), "c-red"),
            ("Voided (changed)", ("void",), "c-grey"), ("Expired", ("expired",), "c-violet"),
            ("Waiting", ("open",), "c-amber"))


def render_overview(s: Snapshot, products: list[str]) -> str:
    live = _live_cases(s)
    open_cases = sum(1 for _, r in live if s.stage(r["card_id"]) != stages.DONE)
    waiting = s.open_votes()
    times = decision_times(s)
    status = Counter(v.get("status") for v in s.votes.values())
    decided = sum(status.get(x, 0) for x in ("approved", "done", "declined"))
    approved = sum(status.get(x, 0) for x in ("approved", "done"))
    health = s.health()
    ran = [h for h in health if h["state"] != "never"]
    healthy = sum(1 for h in ran if h["state"] == "ok")
    metrics = (_metric("Waiting on you", len(waiting), "decisions in the inbox", hot=bool(waiting))
               + _metric("Open cases", open_cases, f"{len(live) - open_cases} closed")
               + _metric("Time to a decision", _hours(_median(times)), f"median, {len(times)} with a recorded time")
               + _metric("Approval rate", f"{100 * approved / decided:.0f}%" if decided else "—",
                         f"{approved} of {decided} decided"))
    new_cases = bar_chart(daily(s, [_at(r.get("created_at")) for _, r in live]))
    firsts = first_decisions(s)
    decisions = bar_chart(daily(s, [at for _, at, a in firsts if a == "yes"]),
                          second=daily(s, [at for _, at, a in firsts if a == "no"])).replace("<svg class=chart", "<svg class='chart yn'", 1)
    total = sum(status.values()) or 1
    stack = "".join(f"<i class='{c}' style='width:{max(100 * sum(status.get(x, 0) for x in keys) / total, 1.5 if sum(status.get(x, 0) for x in keys) else 0):.2f}%' "
                    f"title='{e(name)}'></i>" for name, keys, c in OUTCOMES)
    legend = "".join(f"<span><i class='sw {c}'></i>{e(name)} <b>{sum(status.get(x, 0) for x in keys)}</b></span>"
                     for name, keys, c in OUTCOMES if sum(status.get(x, 0) for x in keys))
    kind_counts = Counter(str(r.get("kind") or "other") for _, r in live)
    kinds = hbars(kind_counts.most_common(6) + ([("everything else", sum(kind_counts.values())
                                                  - sum(v for _, v in kind_counts.most_common(6)))]
                                                if len(kind_counts) > 6 else []))
    stage_rows = Counter(s.stage(r["card_id"]) for _, r in live)
    where = hbars([(st, stage_rows.get(st, 0)) for st in STAGE_ORDER if stage_rows.get(st)])
    stale = [h["job"] for h in health if h["state"] == "stale"]
    alert = (f"<div class=banner style='background:var(--red-bg);color:var(--red)'>{icon('health')}<div><b>Needs a look:"
             f"</b> {e(', '.join(stale))} missed its last runs. <a href='/p/{e(s.config.slug)}/health'>Open health →</a>"
             f"</div></div>" if stale else "")
    body = (f"{_paused_banner(s)}{alert}"
            + head("Overview", "How support is going, from what the loop has recorded.", crumb=e(s.config.name))
            + f"<div class=metrics>{metrics}</div>"
            + "<div class=grid2 style='margin-top:20px'>"
            f"<div class=panel><h3>New cases</h3><div class=ph>Per day, last 14 days</div>{new_cases}</div>"
            f"<div class=panel><h3>Decisions made</h3><div class=ph>Per day, last 14 days · <span class='sw c-green'></span> Yes "
            f"&nbsp;<span class='sw c-red'></span> No</div>{decisions}</div>"
            f"<div class=panel><h3>Decision outcomes</h3><div class=ph>All {sum(status.values())} decisions · "
            f"approval rate counts only Yes and No</div>"
            f"<div class=stack>{stack}</div><div class=legend>{legend}</div></div>"
            f"<div class=panel><h3>Where cases are now</h3><div class=ph>By stage on the board</div>{where}</div>"
            f"<div class=panel><h3>Loop health</h3><div class=ph>Jobs that have run here: {healthy} of {len(ran)} healthy"
            f"</div>"
            + "".join(f"<div class=hb style='grid-template-columns:1fr auto'><span class=l style='text-transform:"
                      f"capitalize'>{e(h['job'])}</span>{pill(h['state'])}</div>" for h in ran)
            + f"<p style='margin:10px 0 0'><a href='/p/{e(s.config.slug)}/health'>All jobs →</a></p></div>"
            + f"<div class=panel><h3>Cases by type</h3><div class=ph>Open and closed</div>{kinds}</div>"
            + "</div>"
            "<p class=notmeasured>Not measured yet, so not shown: AI cost per case and customer satisfaction.</p>")
    return page("Overview", body, products, s.config.slug, "overview", snap=s)


def _decision_material(s: Snapshot, v: dict, rec: dict) -> str:
    kind, sp = v.get("kind"), (v.get("spec") or {})
    if kind == "reply":
        text = s.draft_text(v)
        if text is None:
            return "<p class=faint>Draft file not readable.</p>"
        return (f"<div class=mail><div class=mh><b>From</b><span>{e(s.config.support_address)}</span><b>To</b>"
                f"<span>{e(rec.get('customer'))}</span><b>Subject</b><span>Re: {e(rec.get('title'))}</span></div>"
                f"<div class=mb>{e(_strip_subject(text))}</div></div>"
                f"<div class=faint style='font-size:12px;margin-top:8px'>Version <code>{e(str(sp.get('sha256', ''))[:12])}"
                f"</code></div>")
    if kind == "money":
        cap = s.config.policy.refund_max_minor
        cur = ", ".join(s.config.policy.currencies).upper()
        ids = "".join(f"<dt>{lbl}</dt><dd><code>{e(sp[k])}</code></dd>" for lbl, k in
                      (("Charge", "charge"), ("Subscription", "subscription"), ("Customer", "customer")) if sp.get(k))
        return (f"<div class=amount><span class=big>{e(money_text(sp))}</span>"
                + (f"<span class=faint>refund cap {cap / 100:.2f} {e(cur)}</span>" if cap else "") + "</div>"
                + (f"<dl class=kv>{ids}</dl>" if ids else "")
                + f"<details><summary>Exact spec being voted on</summary>"
                f"<pre>{e(json.dumps(sp, indent=1, sort_keys=True))}</pre></details>")
    if kind == "merge":
        return (f"<dl class=kv><dt>Pull request</dt><dd><b>#{e(sp.get('pr'))}</b></dd><dt>Exact head</dt>"
                f"<dd><code>{e(sp.get('head'))}</code></dd></dl><div class=faint style='font-size:12.5px;margin-top:8px'>"
                f"If the PR moves to a new commit, this vote is void and a new one opens.</div>")
    if kind == "close":
        return (f"<p class=mute style='margin:0'>Our last reply went out {ago(sp.get('last_sent'), ref_time(s))} "
                f"and the customer has not written since. A new email reopens the case.</p>")
    return ""


def ballots_for(s: Snapshot, v: dict) -> list[tuple[str, str, str]]:
    """(name, answer, where) for everyone who has voted: board ballots seen at the last tally,
    plus console votes recorded since, for this exact revision."""
    seen: dict[str, tuple[str, str]] = {uid: (b.get("answer", ""), b.get("via", "board"))
                                        for uid, b in (v.get("ballots") or {}).items()}
    for uid, w in (s.web_votes.get(v.get("key"), {}) or {}).items():
        if w.get("fingerprint") == v.get("fingerprint"):
            seen[uid] = (w.get("answer", ""), "console")
    return [(s.config.operators.get(uid, "not an operator"), a, via) for uid, (a, via) in sorted(seen.items())]


def _ballots_html(s: Snapshot, v: dict) -> str:
    rows = ballots_for(s, v)
    if not rows:
        return ""
    chips = "".join(f"<span class='ballot b-{e(a)}'>{e(n)} · {'Yes' if a == 'yes' else 'No'} "
                    f"<small>via {e(via)}</small></span>" for n, a, via in rows)
    return f"<div class=ballots>{chips}<span class=faint>counted on the next run</span></div>"


def _vote_form(s: Snapshot, v: dict) -> str:
    """Yes / No buttons, only for a signed-in operator in the console."""
    if not s.console:
        return ""
    if not s.console.get("can_vote", True):
        return (f"<div class=act><span class=faint>{icon('lock')} Voting from here is switched off for this "
                f"product: {e(s.console.get('vote_off_reason', 'not connected'))}</span></div>")
    hidden = (f"<input type=hidden name=csrf value='{e(s.console['csrf'])}'>"
              f"<input type=hidden name=key value='{e(v.get('key'))}'><input type=hidden name=fp "
              f"value='{e(v.get('fingerprint'))}'>")
    return (f"<form class=act method=post action='/p/{e(s.config.slug)}/vote'>{hidden}"
            f"<button class=yes name=answer value=yes>Yes, approve</button>"
            f"<button class=no name=answer value=no>No, decline</button>"
            f"<span class=faint>Acts on the next run, with every safety check.</span></form>")


def _console_form(s: Snapshot, action: str, label: str, cls: str = "") -> str:
    return (f"<form class=inline method=post action='/p/{e(s.config.slug)}/{action}'>"
            f"<input type=hidden name=csrf value='{e(s.console['csrf'])}'><button class='{cls}'>{e(label)}</button></form>")


def render_decisions(s: Snapshot, products: list[str]) -> str:
    slug, ref = s.config.slug, ref_time(s)
    waiting = s.open_votes()
    items = ""
    for v in waiting:
        rec = s.cases.get(v.get("case_key"), {})
        kind = v.get("kind")
        label = KIND_LABEL.get(kind, kind)
        if kind == "money":
            label = "Refund" if (v.get("spec") or {}).get("action") == "refund" else "Money"
        items += (f"<article class=item><div class=item-head>{avatar(rec.get('customer'))}"
                  f"<div class=who><div class=eyebrow><b>{e(label)}</b> · {e(rec.get('customer'))}</div>"
                  f"<div class=title>{e(v.get('subject'))}</div><div class=q>{e(v.get('question'))}</div></div>"
                  f"<div class=meta><span>{ago(v.get('opened_at'), ref)}</span></div></div>"
                  f"<div class=item-body>{_decision_material(s, v, rec)}</div>"
                  f"{_ballots_html(s, v)}{_vote_form(s, v)}<div class=item-foot><span>Case: {e(rec.get('title'))}</span>"
                  f"<a href='/p/{e(slug)}/case/{e(v.get('case_key'))}'>Open case →</a></div></article>")
    if not items:
        items = ("<div class=empty><b>Inbox zero</b>Nothing is waiting for your say-so. "
                 "New drafts, refunds and merges will appear here.</div>")
    n = len(waiting)
    body = (f"{_paused_banner(s)}"
            + head("Inbox", f"{n} decision{'s' if n != 1 else ''} waiting for your say-so" if n else
                   "Nothing waiting", crumb=e(s.config.name))
            + ((f"<div class=note>{icon('lock')}<span>Your Yes or No is recorded against the exact version shown. "
                f"If it changes before the loop runs, the vote is void and asked again.</span></div>" if s.console else
                f"<div class=note>{icon('lock')}<span>Approve or decline with Yes / No on the board poll. "
                f"This page cannot approve, so nothing here can act by mistake.</span></div>") if waiting else "")
            + (_console_form(s, "tick", "Run the loop once (dry-run only)", "ghost") if s.console and
               s.console.get("can_tick") else "")
            + items)
    return page(s.config.name, body, products, slug, "", snap=s)


CASE_ORDER = (stages.AWAITING, stages.NEW, stages.IN_SUPPORT, stages.IN_DEV, stages.IN_QA, stages.BLOCKED, stages.DONE)


def render_board(s: Snapshot, products: list[str]) -> str:
    slug, ref = s.config.slug, ref_time(s)
    groups: dict[str, list[str]] = {st: [] for st in CASE_ORDER}
    other = []
    open_votes = s.open_votes()
    for key, rec in sorted(s.cases.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True):
        if not rec.get("card_id") or rec.get("folded_into"):
            continue
        tags = s.tags(rec["card_id"])
        st = s.stage(rec["card_id"])
        extra = "".join(f"<span class='tag{' w' if t == stages.WAITING else ''}'>{e(t)}</span>" for t in tags if t != st)
        n = sum(1 for v in open_votes if v.get("case_key") == key)
        wbadge = f"<span class='tag w'>{n} to decide</span>" if n else ""
        row = (f"<a class=row href='/p/{e(slug)}/case/{e(key)}'><div style='min-width:0'><div class=tt>"
               f"{e(rec.get('title'))}</div>{f'<div class=tags>{wbadge}{extra}</div>' if (extra or wbadge) else ''}"
               f"</div><div class=cu>{avatar(rec.get('customer'))}<span>{e(rec.get('customer'))}</span></div>"
               f"<div class=kd>{e(rec.get('kind') or '')}</div><div class=ag>{ago(rec.get('created_at'), ref)}</div></a>")
        (groups[st] if st in groups else other).append(row)
    html_groups = "".join(
        f"<details class='group{' empty' if not rows else ''}' aria-label=\"{e(st)} · {len(rows)}\""
        f"{' open' if rows and st != stages.DONE else ''}><summary>{stage_pill(st)}<span class=count>{len(rows)}</span>"
        f"<span class=hint>{e(STAGE_HINT.get(st, ''))}</span></summary><div class=rows>{''.join(rows)}</div></details>"
        for st, rows in groups.items())
    unread = (f"<h2>Unreadable</h2><div class=rows>{''.join(other)}</div>" if other else "")
    total = sum(len(r) for r in groups.values())
    body = (f"{_paused_banner(s)}" + head("Cases", f"{total} cases · one per customer per problem, grouped by stage.",
                                          crumb=e(s.config.name))
            + f"{html_groups}{unread}")
    return page("Cases", body, products, slug, "board", snap=s)


def render_case(s: Snapshot, key: str, products: list[str]) -> str | None:
    rec = s.cases.get(key)
    if rec is None:
        return None
    slug, ref = s.config.slug, ref_time(s)
    msgs = s.card_messages(rec["card_id"])
    first = (f"<div class='msg cust'>{message_html(msgs[0]).split('</div>', 1)[-1] if msgs[0].startswith('**') else e(msgs[0])}</div>" if msgs
             else "<p class=faint>Read the card on the board.</p>")
    events = [j for j in s.journal if j.get("case_key") == key or str(j.get("key", "")).find(key) >= 0]
    timeline = "".join(
        f"<div class=ev><span class='dot t-{EVENT_TONE.get(j.get('event'), 'grey')}'></span><div class=body>"
        f"<div>{e(event_text(s, j))}</div><div class=when>{e(when(j.get('at')))}</div></div></div>"
        for j in events) or "<p class=faint>No events yet.</p>"
    rest = "".join(f"<div class=ev><span class='dot'></span><div class=body><div class=msg>{message_html(m)}</div>"
                   f"</div></div>" for m in msgs[1:])
    vrows = ""
    for vk, v in sorted(s.votes.items(), key=lambda kv: kv[1].get("opened_at", "")):
        if v.get("case_key") != key:
            continue
        att = s.attempts.get(vk, {})
        who = operator(s, v.get("decided_by"))
        meta = f"decided by {e(who)}" if who else "opened " + ago(v.get("opened_at"), ref)
        if who and v.get("decided_via"):
            meta += f" · via {e(v.get('decided_via'))}"
        if v.get("void_reason"):
            meta += " · " + e(v.get("void_reason"))
        proof = f"<div style='margin-top:5px'>{exec_pill(att.get('status'))}</div>" if att else ""
        label = KIND_LABEL.get(v.get("kind"), v.get("kind"))
        if v.get("kind") == "reply":
            label += " · " + vk.rsplit(":", 1)[-1]
        vrows += (f"<div class=vote><div class=vh><b>{e(label)}</b>{pill(v.get('status'))}</div>"
                  f"<div class=mute>{e(v.get('question'))}</div><div class=vw>{meta}</div>{proof}</div>")
    drafts = ""
    ddir = s.root / "drafts" / key
    if ddir.is_dir():
        for p in sorted(ddir.glob("reply-v*.txt")):
            drafts += (f"<details><summary><code>{e(p.name)}</code></summary>"
                       f"<pre>{e(p.read_text(encoding='utf-8'))}</pre></details>")
    receipts = ""
    rdir = s.root / "receipts" / key
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.json")):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                receipts += (f"<div style='margin-bottom:8px'>{pill(r.get('status'))}<div class=faint "
                             f"style='font-size:12px;margin-top:2px'>{e(when(r.get('at')))} · "
                             f"<code>{e(r.get('message_id'))}</code></div></div>")
            except ValueError:
                receipts += f"<pre>{e(p.read_text(encoding='utf-8'))}</pre>"
    props = (f"<dl style='margin:0'><div class=prop><dt>Stage</dt><dd>{stage_pill(s.stage(rec['card_id']))}</dd></div>"
             f"<div class=prop><dt>Customer</dt><dd>{e(rec.get('customer'))}</dd></div>"
             f"<div class=prop><dt>Type</dt><dd>{e(rec.get('kind'))}</dd></div>"
             f"<div class=prop><dt>Ticket</dt><dd>{e(rec.get('ticket') or '—')}</dd></div>"
             f"<div class=prop><dt>Opened</dt><dd>{e(when(rec.get('created_at')))}</dd></div>"
             f"<div class=prop><dt>Case key</dt><dd><code>{e(key)}</code></dd></div></dl>")
    none = "<p class=faint style='margin:0;font-size:13px'>None</p>"
    body = (f"{_paused_banner(s)}"
            + head(str(rec.get("title")), "",
                   crumb=f"<a href='/p/{e(slug)}/board'>Cases</a> / {e(rec.get('ticket') or key)}")
            + "<div class=case><div class=thread>"
            f"<h2>Customer wrote</h2>{first}"
            f"<h2>Card thread <span class=c>{max(len(msgs) - 1, 0)}</span></h2>"
            f"{rest or '<p class=faint>No further messages.</p>'}"
            f"<h2>Timeline</h2>{timeline}</div>"
            f"<div class=props><h3 style='margin-top:0'>Properties</h3>{props}<h3>Votes</h3>{vrows or none}"
            + "".join(_vote_form(s, v) for v in s.open_votes() if v.get("case_key") == key)
            + f"<h3>Send receipts</h3>{receipts or none}<h3>Drafts</h3>{drafts or none}</div></div>")
    return page(str(rec.get("title")), body, products, slug, "board", snap=s)


def render_money(s: Snapshot, products: list[str]) -> str:
    rows = ""
    verified = awaiting = 0
    for r in s.money_rows():
        sp = r["spec"]
        amt = sp.get("amount", 0) if sp.get("action") == "refund" else 0
        if r["execution"] == "VERIFIED_WITH_PROVIDER":
            verified += amt
        if r["vote"] == "open":
            awaiting += amt
        rec = s.cases.get(r["case_key"], {})
        rows += (f"<tr><td><div style='display:flex;gap:10px;align-items:flex-start'>{avatar(rec.get('customer'))}"
                 f"<div><a href='/p/{e(s.config.slug)}/case/{e(r['case_key'])}'>{e(r['subject'])}</a>"
                 f"<div class=faint style='font-size:12px'>{e(rec.get('customer'))}</div></div></div></td>"
                 f"<td><b>{e(money_text(sp))}</b></td><td>{pill(r['vote'])}</td><td>{exec_pill(r['execution'])}</td>"
                 f"<td>{e(operator(s, r['decided_by'])) or '<span class=faint>—</span>'}</td></tr>")
    p = s.config.policy
    cur = e(", ".join(p.currencies).upper() or "-")
    metrics = (_metric("Refunded · verified", f"{verified / 100:.2f}", f"{cur} · read back from the provider")
               + _metric("Awaiting a Yes", f"{awaiting / 100:.2f}", cur, hot=bool(awaiting))
               + _metric("Refund cap", f"{p.refund_max_minor / 100:.2f}", "larger refunds are refused")
               + _metric("Currencies", cur, "others are refused"))
    body = (f"{_paused_banner(s)}" + head("Money", "Refunds and cancellations run only on a Yes, then are verified "
                                          "with the payment provider.", crumb=e(s.config.name))
            + f"<div class=metrics>{metrics}</div><h2>Requests</h2>"
            f"<table><colgroup><col style='width:32%'><col style='width:40%'><col style='width:10%'><col style='width:10%'>"
            f"<col style='width:8%'></colgroup><tr><th>Case</th><th>Action</th><th>Vote</th><th>Provider check</th>"
            f"<th>Decided by</th></tr>"
            f"{rows or '<tr><td colspan=5 class=faint>No money requests yet.</td></tr>'}</table>")
    return page("Money", body, products, s.config.slug, "money", snap=s)


def render_health(s: Snapshot, products: list[str]) -> str:
    ref = ref_time(s)
    jobs = ""
    for h in s.health():
        tone = TONE.get(h["state"], "grey")
        last = f"last run {ago(h['at'], ref)}" if h["at"] else "not run yet"
        jobs += (f"<div class=job><span class='led t-{tone}'></span><div><b>{e(h['job'])}</b><div>{pill(h['state'])}"
                 f"</div></div><div><div class=mute>{e(JOB_HINT.get(h['job'], ''))}</div>"
                 f"<div class=res>{e(h['result'])[:160]}</div></div><div class=faint style='text-align:right'>"
                 f"{last if h['at'] else ''}</div></div>")
    alerts = [j for j in s.journal if j.get("event") == "alert"][-20:][::-1]
    arows = "".join(f"<tr><td class=faint style='white-space:nowrap'>{e(when(a.get('at')))}</td>"
                    f"<td>{e(a.get('text'))}</td></tr>" for a in alerts)
    state = pill("paused") if s.paused else pill("running")
    body = (f"{_paused_banner(s)}"
            + head("Health", f"Loop is {state} &nbsp;· a job is stale after three missed intervals.",
                   crumb=e(s.config.name))
            + ((_console_form(s, "resume", "Resume the loop", "yes") if s.paused else
                _console_form(s, "pause", "Pause everything that acts", "no")) if s.console else "")
            + f"<div class=jobs>{jobs}</div><h2>Recent alerts <span class=c>{len(alerts)}</span></h2>"
            f"<table>{arows or '<tr><td class=faint>No alerts. The loop alerts instead of guessing.</td></tr>'}</table>")
    return page("Health", body, products, s.config.slug, "health", snap=s)


def render_audit(s: Snapshot, products: list[str]) -> str:
    rows = ""
    for j in s.journal[-300:][::-1]:
        fields = {k: v for k, v in j.items() if k not in ("at", "event")}
        case = j.get("case_key") or next((c for c in s.cases if c in str(j.get("key", ""))), None)
        link = (f"<a href='/p/{e(s.config.slug)}/case/{e(case)}'>{e(s.cases.get(case, {}).get('title', case))}</a>"
                if case in s.cases else "<span class=faint>—</span>")
        rows += (f"<tr><td class=faint style='white-space:nowrap'>{e(when(j.get('at')))}</td>"
                 f"<td><span class='pill t-{EVENT_TONE.get(j.get('event'), 'grey')}'>{e(j.get('event'))}</span></td>"
                 f"<td>{e(event_text(s, j))}<details><summary>raw</summary>"
                 f"<pre>{e(json.dumps(fields, sort_keys=True, default=str))[:600]}</pre></details></td><td>{link}</td></tr>")
    body = (head("Audit log", f"Newest first · last 300 of {len(s.journal)} events, from the append-only journal.",
                 crumb=e(s.config.name))
            + f"<table><tr><th>When</th><th>Event</th><th>What happened</th><th>Case</th></tr>"
            f"{rows or '<tr><td colspan=4 class=faint>Empty.</td></tr>'}</table>")
    return page("Audit", body, products, s.config.slug, "audit", snap=s)


# -- server ------------------------------------------------------------------------

def route(path: str, snapshot_for: Callable[[str], Snapshot], products: list[str]) -> tuple[int, str, str]:
    """Return (status, content type, body) for one GET path."""
    parts = [unquote(p) for p in urlparse(path).path.split("/") if p]
    if not parts:
        return 200, "text/html", render_home({p: snapshot_for(p) for p in products})
    if parts[0] == "api" and len(parts) == 2 and parts[1].removesuffix(".json") in products:
        return 200, "application/json", json.dumps(snapshot_for(parts[1].removesuffix(".json")).as_json(), default=str)
    if parts[0] != "p" or len(parts) < 2 or parts[1] not in products:
        return 404, "text/html", page("Not found", "<h1>Not found</h1>", products, None)
    s = snapshot_for(parts[1])
    tab = parts[2] if len(parts) > 2 else ""
    if tab == "" and len(parts) == 2:
        return 200, "text/html", render_decisions(s, products)
    simple = {"board": render_board, "money": render_money, "health": render_health, "audit": render_audit,
              "overview": render_overview}
    if tab in simple and len(parts) == 3:
        return 200, "text/html", simple[tab](s, products)
    if tab == "case" and len(parts) == 4:
        out = render_case(s, parts[3], products)
        if out is not None:
            return 200, "text/html", out
    return 404, "text/html", page("Not found", "<h1>Not found</h1>", products, parts[1])


def check_host(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise RemoteBindRefused(f"refusing to listen on {host!r}: the dashboard has no login; use 127.0.0.1")


def make_server(configs: list[Config], host: str = "127.0.0.1", port: int = 8765,
                board_readers: dict[str, Callable] | None = None) -> ThreadingHTTPServer:
    check_host(host)
    by_slug = {c.slug: c for c in configs}
    readers = board_readers or {}
    products = list(by_slug)

    def snapshot_for(slug: str) -> Snapshot:
        return Snapshot(by_slug[slug], readers.get(slug))

    class Handler(BaseHTTPRequestHandler):
        server_version = "sayso-dashboard"

        def _send(self, status: int, ctype: str, body: str, head: bool = False) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _get(self, head: bool) -> None:
            try:
                status, ctype, body = route(self.path, snapshot_for, products)
            except Exception as exc:  # noqa: BLE001 - corrupt state is shown, never hidden as empty
                status, ctype, body = 500, "text/plain", f"state could not be read: {type(exc).__name__}: {exc}"
            self._send(status, ctype, body, head)

        def do_GET(self):  # noqa: N802
            self._get(False)

        def do_HEAD(self):  # noqa: N802
            self._get(True)

        def _refuse(self):
            self._send(405, "text/plain", "read-only dashboard: GET only")

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

        def log_message(self, fmt, *args):  # keep request paths out of logs
            pass

    return ThreadingHTTPServer((host, port), Handler)


def board_reader_for(config: Config) -> Callable[[str], list[str]] | None:
    """A get_tags-only reader for a real board. Fakes are read from the world file instead."""
    section = config.section("board")
    if section.adapter in ("fake", "none") or not config.live:
        return None
    from sayso.adapters import reference
    board = reference.build("board", section.adapter, section, config)
    return board.get_tags


def build_demo_state(tmp: Path) -> Config:
    """Run the fake demo, then leave three more customers mid-flow so every screen has data."""
    from sayso import demo, votes
    from sayso.jobs import money_job
    ctx = demo.build(tmp)
    demo.scenario(ctx, say=lambda *_: None)
    w = ctx.world
    ctx.clock.advance(hours=1)
    w.customer_writes(sender="sam@school.test", subject="Can't log in after password reset",
                      body="I reset my password twice and it still says invalid. Class starts at 9.")
    w.customer_writes(sender="lee@customer.test", subject="Charged after I cancelled",
                      body="I cancelled last week but was charged again today. <b>Please fix</b>.")
    demo.tick(ctx, say=lambda *_: None)
    from sayso import cases
    lee = next(k for k, r in cases.load(ctx).items() if r["customer"] == "lee@customer.test")
    money_job.request(ctx, case_key=lee, subject="Lee - charged after cancelling",
                      spec={"action": "refund", "charge": "ch_demo0002", "amount": 2900, "currency": "usd",
                            "customer": "cus_demo0002"})
    ctx.clock.advance(minutes=5)
    w.customer_writes(sender="ana@customer.test", subject="Feature request: dark mode", body="Would love dark mode!")
    demo.tick(ctx, say=lambda *_: None)
    assert votes.load(ctx), "demo produced no votes"
    return ctx.config


def serve(configs: list[Config], host: str, port: int, readers: dict | None = None) -> None:
    srv = make_server(configs, host, port, readers)
    print(f"read-only dashboard: http://{host}:{srv.server_address[1]}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
