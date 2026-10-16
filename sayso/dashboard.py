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
from urllib.parse import parse_qs, unquote, urlparse

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
              "UNVERIFIED": "Unverified", "pending": "In progress", "PENDING_WITH_PROVIDER": "Waiting for provider approval"}
TONE = {"open": "amber", "pending": "amber", "never": "grey", "approved": "violet", "done": "green", "ok": "green",
        "running": "green", "SENT_AND_VERIFIED": "green", "VERIFIED_WITH_PROVIDER": "green", "void": "grey",
        "expired": "grey", "declined": "red", "stale": "red", "UNVERIFIED": "red", "paused": "amber", "PENDING_WITH_PROVIDER": "amber",
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
            "health": "Watches the other jobs", "qa": "Checks a fix on its exact head before a merge vote",
            "stall": "Reminds about cases and votes that stopped moving"}

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
    if ev == "notice":
        inner = key[len("vote:"):] if key.startswith("vote:") else key
        k2 = KIND_LABEL.get(inner.split(":")[0], inner.split(":")[0]) if inner else "decision"
        who = operator(s, j.get("mention")) if j.get("mention") else "any operator"
        return f"Notified {who} in the alerts channel · {k2} waiting"
    if ev == "web_vote":
        return f"{'Yes' if j.get('answer') == 'yes' else 'No'} recorded in the console by {j.get('by', '')}"
    if ev in ("paused", "resumed"):
        return f"Loop {ev}" + (f" · {j.get('reason') or j.get('by')}" if (j.get("reason") or j.get("by")) else "")
    if ev == "console_tick":
        return f"Dry-run pass started from the console by {j.get('by', '')}"
    return str(ev)


EVENT_TONE = {"case_opened": "blue", "mail_appended": "blue", "vote_opened": "amber", "vote_approved": "violet",
              "vote_done": "green", "vote_void": "grey", "vote_declined": "red", "vote_expired": "grey",
              "alert": "red", "send_attempt": "indigo"}


CSS = """
:root{--ink:#18181b;--text:#3f3f46;--mute:#5f5f68;--faint:#6b6b74;--line:#e6e5e1;--line2:#efefec;
--bg:#ffffff;--side:#f7f7f5;--hover:#f1f1ee;--accent:#b8461c;--accent-soft:#fbeee8;--plum:#27212d;
--green:#2f7d4f;--green-bg:#eaf5ee;--amber:#9a5b00;--amber-bg:#fdf3e2;--red:#b93a32;--red-bg:#fbecea;
--violet:#6a4bb8;--violet-bg:#f1edfa;--blue:#2f6db5;--blue-bg:#ebf2fb;--cyan:#1f7a86;--cyan-bg:#e8f5f6;
--indigo:#c2521f;--indigo-bg:#fdf0ea;--grey:#6f6775;--grey-bg:#f2f0ec;--r:10px}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.6 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif;display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh;
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
.chart{width:100%;max-width:640px;height:auto;display:block}.chart rect.b{fill:var(--accent)}.chart rect.b.no2{fill:#d4695f}.chart.yn rect.b:not(.no2):not(.z){fill:#5aa878}.chart rect.b.z{fill:var(--line)}
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
.note{display:flex;gap:10px;align-items:flex-start;font-size:13.5px;color:var(--mute);margin:0 0 18px}
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
.act button,form.inline button,.btn{border:0;border-radius:6px;padding:9px 16px;font:600 13.5px/1 inherit;cursor:pointer}
.act .faint{font-size:12.5px;flex:1 1 240px}
.yes{background:var(--accent);color:#fff}.yes:hover{background:#a03c18}
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

NAV = (("", "Decisions", "check"), ("board", "Inbox", "inbox"), ("money", "Refunds", "money"),
       ("overview", "Insights", "chart"), ("health", "Health", "health"), ("audit", "Audit log", "audit"))
NAV_GROUPS = {"": "", "health": "Admin"}        # daily work first (no label), then admin, drawn lighter
NAV_ALIAS = {"swipe": "", "case": "board"}      # Swipe is a mode of Decisions; a case lives under Inbox

# Page craft shared with Sayso Cloud (sayso.brand.app) plus the shell. Added AFTER CSS, so CSS (a name hosts
# import and edit) keeps every rule it had.
SHELL_CSS = """
body{background:var(--bg,#f7f7f5)}main{max-width:none;padding:28px 40px 72px}
button,input,select{font:inherit}
.demo-strip{grid-column:1/-1;display:flex;align-items:center;gap:10px;background:#18181b;color:#fff;font-size:13px;
padding:7px 18px;position:sticky;top:0;z-index:6}.demo-strip b{background:#fff;color:#18181b;border-radius:4px;padding:0 7px;
font-size:11.5px}
body.has-strip{grid-template-rows:auto 1fr}body.has-strip aside{top:34px;height:calc(100vh - 34px)}
.navgroup{display:flex;flex-direction:column;gap:1px}.navgroup+.navgroup{margin-top:14px}
.navgroup.admin a{color:var(--faint)}.navgroup.admin a.on{color:var(--ink)}
.acct{margin-top:auto;border-top:1px solid var(--line);padding-top:10px}
.acct summary{list-style:none;display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;cursor:pointer;
color:var(--ink);font-weight:600;font-size:14px}.acct summary::-webkit-details-marker{display:none}
.acct summary:hover{background:var(--hover)}.acct summary small{display:block;font-weight:400;color:var(--faint);font-size:12px}
.acct .menu{padding:6px 8px 2px;font-size:13px;color:var(--mute)}.acct .menu .link{padding:6px 0}
details.acct:not([open]) .menu{display:none}
@media (max-width:860px){body{grid-template-columns:minmax(0,1fr)}main{padding:20px 16px 48px;min-width:0}
aside{min-width:0;max-width:100vw}aside>div{min-width:0}body.has-strip aside{top:0;height:auto}
aside nav{display:flex;flex-wrap:nowrap;overflow-x:auto;gap:2px;
-webkit-mask-image:linear-gradient(to right,#000 85%,transparent);mask-image:linear-gradient(to right,#000 85%,transparent);padding-right:28px}
aside nav a{min-height:44px}.navgroup .navlabel{display:none}
.sa-ph-a{min-width:0}.sa-ph-a .sa-seg{max-width:100%;overflow-x:auto}.alog{overflow:hidden}.aw,.aa,.ac{min-width:0}
.demo-strip{position:static;font-size:12px;padding:6px 14px}.navgroup{flex-direction:row;flex-wrap:nowrap}
.navgroup+.navgroup{margin-top:0}aside nav,aside>div:has(nav){overflow-x:auto;scrollbar-width:none;max-width:100%}
aside nav{flex-wrap:nowrap!important}.acct{display:none}}
.qgroup{font-size:13px;color:var(--soft);font-weight:600;margin:26px 0 8px}.qgroup:first-of-type{margin-top:6px}
.item.dec{background:var(--card,#fff);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 12px}
.item.dec:first-of-type{border-top:1px solid var(--line)}.qflags{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.qamt{font-size:15px}.tickf{margin:0}
@media (min-width:1180px){.dsplit{display:grid;grid-template-columns:minmax(300px,360px) minmax(0,1fr);gap:20px;align-items:start}
.dsplit .queue{position:sticky;top:46px;margin:0}main .dsplit a.qrow{grid-template-columns:28px minmax(0,1fr) 44px}
.dsplit .qk,.dsplit .qa{display:none}.dsplit .qw{flex-direction:column;gap:0}.dcards .qgroup:first-child{margin-top:0}}
.queue{display:block;background:var(--card,#fff);border:1px solid var(--line);border-radius:10px;margin:0 0 18px;overflow:hidden}
.qhead{display:flex;gap:10px;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px;color:var(--faint)}
.qhead b{color:var(--ink);font-size:14px}
main a.qrow{display:grid;grid-template-columns:28px minmax(0,1fr) 110px 110px 56px;gap:12px;align-items:center;padding:9px 14px;
border-top:1px solid var(--line2);color:var(--ink2);font-size:13.5px}main a.qrow:hover{background:#fafaf8;text-decoration:none}
.qhead+.qrow{border-top:0}.qw{display:flex;gap:10px;min-width:0}.qw b{color:var(--ink);font-weight:600;white-space:nowrap}
.qw span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--soft)}.qk{font-size:12.5px;color:var(--soft)}
.qa,.qt{text-align:right;font-size:12.5px;color:var(--faint)}.item.dec{scroll-margin-top:16px}.item.dec:target{outline:2px solid var(--accent)}
@media (max-width:860px){main .queue a.qrow{grid-template-columns:28px minmax(0,1fr) auto}.qk,.qa{display:none}.qw{flex-direction:column;gap:0}}.item.dec .act{border-top:1px solid var(--line2);margin-top:14px}
.item.dec .item-body .sa-trust{margin-top:12px}
.ib-who{display:flex;gap:10px;align-items:flex-start;min-width:0}.ib-who>div{min-width:0}
.inbox .sa-cell-s{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:90ch}
.ib-flags{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.cv{background:var(--card,#fff);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:0 0 12px}
.cv-h{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:13.5px}.cv-h b{color:var(--ink)}
.cv-h .sa-faint{margin-left:auto;font-size:12.5px}.cv .msg{max-width:72ch}
.cdec{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--card,#fff);margin-bottom:10px}
.cdec-h{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;color:var(--ink)}
.cdec .act{padding-left:0}.cdec .ballots{padding-left:0}.props{max-width:420px}
.case{grid-template-columns:minmax(0,1fr) minmax(320px,400px)}.cnotes{margin-top:24px}
.sa-tile-v .v{display:inline}.ins .sa-tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:640px){.ins .sa-tiles{grid-template-columns:1fr 1fr}}
main a.sa-rowlink:not(#_),main a.wrow:not(#_),main a.qrow:not(#_){color:inherit}
main a.sa-tab:not(#_){color:var(--soft)}main a.sa-tab.on:not(#_){color:var(--ink)}
main .sa-seg a:not(#_){color:var(--ink2)}main .sa-seg a.on:not(#_){color:var(--ink)}
main a.sa-btn.ghost:not(#_){color:var(--ink)}main a.sa-btn:not(.ghost):not(#_){color:#fff}
.brand small{text-wrap:balance}.tr-done .sa-cell-t{color:var(--soft);font-weight:500}
.hjob.ok .hbadge{display:none}.hjob{padding:10px 16px}.hjob .mute{font-size:13px}
.demo-strip b{white-space:nowrap}.eyebrow b{color:var(--ink)}
@media (min-width:861px){body{background:linear-gradient(to right,var(--side) 244px,var(--line) 244px,var(--line) 245px,var(--bg) 245px)}}.wgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px}
.wrow{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:10px;padding:7px 0;border-top:1px solid var(--line2);
font-size:13px;color:var(--ink2)!important}.wrow:hover{text-decoration:none!important;background:#fafaf8}
.wt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.grid2>.sa-panel{margin:0}
.hstate{display:flex;flex-direction:column;gap:4px;background:var(--card,#fff);border:1px solid var(--line);border-left:3px solid var(--send);
border-radius:10px;padding:14px 16px;margin:0 0 16px;font-size:14px;color:var(--ink)}.hstate.bad{border-left-color:var(--danger)}
.hstate.warn{border-left-color:var(--warn)}.hstate.neutral{border-left-color:var(--line2)}.hstate .sa-faint{font-size:12.5px}
.hjobs{list-style:none;margin:0;padding:0;background:var(--card,#fff);border:1px solid var(--line);border-radius:10px}
.hjob{display:grid;grid-template-columns:28px minmax(0,1fr) 150px;gap:12px;padding:12px 16px;border-top:1px solid var(--line);font-size:13.5px}
.hjob:first-child{border-top:0}.hn{color:var(--faint);font-variant-numeric:tabular-nums;padding-top:1px}
.hh b{color:var(--ink)}.hjob .res{font-size:12px;color:var(--faint);margin-top:2px;word-break:break-word}
.hwhen{text-align:right;font-size:12.5px}.hraw{margin-top:2px}.hraw summary{font-size:12px}
details.hraw:not([open]) pre{display:none}.hjob .res{color:var(--ink2);font-size:12.5px}.hfix{margin-top:8px;background:#fdecec;border-radius:6px;padding:8px 10px;color:var(--ink2)}
@media (max-width:860px){.hjob{grid-template-columns:22px minmax(0,1fr)}.hwhen{grid-column:2;text-align:left}}@media (max-width:860px){.wgrid{grid-template-columns:1fr}}
@media (max-width:860px){.item.dec{padding:14px}.item.dec .act{padding:12px 0 0}.act button{flex:1 1 40%;min-height:44px}
.item.dec .act .faint{flex-basis:100%;order:3}.item.dec .item-foot{padding-top:10px}}
"""


def _page_css() -> str:
    from sayso import brand
    from sayso.brand import app as brand_app
    root = ";".join(f"--{k}:{v}" for k, v in brand.TOKENS.items())
    return (f":root{{{root};--font-text:{brand.TEXT_FONT};--font-mono:{brand.MONO_FONT}}}" + brand_app.css() + SHELL_CSS
            + AUDIT_CSS)
ICONS["swipe"] = '<path d="M7 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM21 9l2 3-2 3M3 9l-2 3 2 3"/>'
ICONS["chart"] = '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'

from sayso.brand import mark_svg as _mark_svg  # noqa: E402  (one mark for every surface)
SAYSO_MARK = _mark_svg("s-mark")
SAYSO_ICON = __import__("urllib.parse").parse.quote(SAYSO_MARK.replace('aria-hidden="true"', ''))
RO_TEXT = "Read-only · cannot send, charge or merge"
CON_TEXT = "Signed in as an operator"
VIEW_TEXT = "Signed in · view only for this product"


def page(title: str, body: str, products: list[str], current: str | None, tab: str = "",
         snap: "Snapshot | None" = None) -> str:
    nav = ""
    if current:
        waiting = len(snap.open_votes()) if snap is not None else 0
        here = NAV_ALIAS.get(tab, tab)
        groups: list[tuple[str, list[str]]] = []
        for name, label, ic in NAV:
            if name in NAV_GROUPS:
                groups.append((NAV_GROUPS[name], []))
            href = f"/p/{e(current)}/{name}" if name else f"/p/{e(current)}/"
            badge = f'<span class=n>{waiting}</span>' if name == "" and waiting else ""
            if name == "health" and snap is not None and any(h["state"] == "stale" for h in snap.health()):
                badge = "<span class=n title='a job is stale' style='color:var(--red)'>●</span>"
            cur = ' aria-current="page"' if here == name else ""
            groups[-1][1].append(f'<a class="{"on" if here == name else ""}" href="{href}"{cur}>{icon(ic)}{label}{badge}</a>')
        nav = "<div><nav aria-label='Pages'>" + "".join(
            f"<div class='navgroup{' admin' if g else ''}'>{f'<div class=navlabel>{e(g)}</div>' if g else ''}{''.join(a)}</div>"
            for g, a in groups) + "</nav></div>"
    prods = "".join(f'<a class="{"on" if p == current else ""}" href="/p/{e(p)}/"><span class=dot></span>{e(p)}</a>'
                    for p in products)
    mode = getattr(snap, "label", None) or ("Dry run" if snap is not None and not snap.config.live else "Live view")
    con = getattr(snap, "console", None)
    stamp = f"<br>{e(when(snap.now.isoformat()))}" if snap is not None and getattr(snap, "label", None) else ""
    demo = snap is not None and not snap.config.live
    strip = ("<div class=demo-strip role=note><b>Dry run</b><span>Fake adapters: nothing is sent to anyone and no money "
             "moves.</span></div>" if demo else "")
    if con:
        foot = (f"<details class=acct><summary>{avatar(con['user'])}<span>{e(con['user'])}<small>Operator · {e(mode)}"
                f"</small></span></summary><div class=menu>Votes count for the exact version shown."
                f"<form method=post action=/logout><input type=hidden name=csrf value='{e(con['csrf'])}'>"
                f"<button class=link>Sign out</button></form></div></details></aside>")
    else:
        foot = f"<div class=side-foot>{icon('lock')}<span><b>Read-only</b>{e(mode)}{stamp}</span></div></aside>"
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><meta name=robots content=noindex>"
            f"<title>{e(title)} · Sayso</title><link rel=icon href='data:image/svg+xml,{SAYSO_ICON}'>"
            f"<style>{CSS}{_page_css()}</style></head><body{' class=has-strip' if strip else ''}>{strip}"
            f"<aside><a class=brand href='/'><span class=logo>{SAYSO_MARK}</span><span>Sayso"
            f"<small>nothing goes out without your say-so</small></span></a>{nav}"
            + (f"<div><div class=navlabel>Products</div><div class=prod>"
               f"<a class='{'on' if current is None else ''}' href='/'>{icon('grid')}All products</a>{prods}</div></div>"
               if len(products) > 1 or current is None else "")
            + foot
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


PERIODS = (7, 14, 30)
WAIT_BUCKETS = (("Under 1 hour", 0, 1), ("1-4 h", 1, 4), ("4-24 h", 4, 24), ("1-3 days", 24, 72),
                ("More than 3 days", 72, None))


def render_overview(s: Snapshot, products: list[str], days: str = "") -> str:
    from sayso.brand import app as A
    slug, ref = s.config.slug, ref_time(s)
    n_days = int(days) if str(days).isdigit() and int(days) in PERIODS else 14
    start = ref - timedelta(days=n_days)
    prev = start - timedelta(days=n_days)
    live = _live_cases(s)
    waiting = s.open_votes()

    def within(at, lo, hi):
        return at is not None and lo < at <= hi

    new_now = sum(1 for _, r in live if within(_at(r.get("created_at")), start, ref))
    new_prev = sum(1 for _, r in live if within(_at(r.get("created_at")), prev, start))
    firsts = first_decisions(s)
    dec_now = [(k, at, a) for k, at, a in firsts if within(at, start, ref)]
    yes_now = sum(1 for *_, a in dec_now if a == "yes")
    opened: dict[str, datetime] = {}
    times = []
    for j in s.journal:
        key, at = str(j.get("key") or ""), _at(j.get("at"))
        if not key or at is None:
            continue
        if j.get("event") == "vote_opened":
            opened.setdefault(key, at)
    for k, at, _a in dec_now:
        if k in opened:
            times.append(max((at - opened[k]).total_seconds() / 3600, 0.0))
    sent = sum(1 for j in s.journal if j.get("event") == "vote_done" and within(_at(j.get("at")), start, ref)
               and (j.get("receipt") or {}).get("status") == "SENT_AND_VERIFIED")
    refunded = sum(int((j.get("receipt") or {}).get("amount") or 0) for j in s.journal
                   if j.get("event") == "vote_done" and str(j.get("key", "")).startswith("money:")
                   and within(_at(j.get("at")), start, ref))
    cur = (s.config.policy.currencies[0] if s.config.policy.currencies else "usd")

    def delta(now, before):
        if not before and not now:
            return "No change on the period before"
        if not before:
            return "None in the period before"
        d = now - before
        return f"{'+' if d >= 0 else ''}{d} on the {n_days} days before"

    tiles = A.tiles([
        A.tile("Waiting on you", f"<div class=v>{len(waiting)}</div>", hot=bool(waiting),
               note="Open decisions right now", source="Open votes in the vote records"),
        A.tile("New cases", e(new_now), note=delta(new_now, new_prev), source="Case records, by date opened"),
        A.tile("Decisions", e(len(dec_now)), note=f"{yes_now} Yes, {len(dec_now) - yes_now} No",
               source="Decision journal: the first answer counts"),
        A.tile("Median time to a decision", e(_hours(_median(times))),
               note=f"{len(times)} with a recorded open time" if times else "No decision with an open time in this period",
               source="Decision journal: vote opened to first answer"),
        A.tile("Replies verified", e(sent), source="Sends found in the Sent folder after they went out"),
        A.tile("Refunded", A.money(refunded, cur), source="Refunds confirmed with the payment provider"),
    ])
    span = [(lbl, n) for lbl, n in daily(s, [_at(r.get("created_at")) for _, r in live], n_days)]
    yes_s = daily(s, [at for _, at, a in firsts if a == "yes"], n_days)
    no_s = daily(s, [at for _, at, a in firsts if a == "no"], n_days)
    ch_new = A.axis_chart(span, label=f"New cases per day, last {n_days} days").replace("class=sa-chart", "class='sa-chart chart'", 1)
    ch_dec = A.axis_chart(yes_s, second=no_s, label=f"Decisions per day, last {n_days} days").replace(
        "class=sa-chart", "class='sa-chart yn'", 1)
    # time waiting on you
    buckets = []
    for lbl, lo, hi in WAIT_BUCKETS:
        n = 0
        for v in waiting:
            at = _at(v.get("opened_at"))
            if at is None:
                continue
            h = (ref - at).total_seconds() / 3600
            if h >= lo and (hi is None or h < hi):
                n += 1
        buckets.append((lbl, n))
    oldest = sorted(waiting, key=lambda v: str(v.get("opened_at", "")))[:5]
    old_rows = "".join(
        f"<a class=wrow href='/p/{e(slug)}/case/{e(v.get('case_key'))}'><span class=wt>{e(v.get('subject'))}</span>"
        f"<span class=sa-faint>{e(_kind_label(v))}</span>{A.reltime(v.get('opened_at'), ref, long=True)}</a>"
        for v in oldest) or "<p class=sa-faint>Nothing is waiting.</p>"
    wait_panel = A.panel(
        "Time waiting on you", f"<div class=wgrid><div>{A.hbars(buckets, total=len(waiting) or 1)}</div>"
        f"<div class=wlist><div class=sa-faint style='font-size:12.5px;margin-bottom:4px'>Oldest first"
        f" ({len(oldest)} of {len(waiting)})</div>{old_rows}</div></div>"
        + A.source("open decisions in the vote records; age from when each vote opened"),
        sub=f"{len(waiting)} open" + (f" \u00b7 oldest {A.long_age((ref - _at(oldest[0].get('opened_at'))).total_seconds())}"
                                     if oldest and _at(oldest[0].get('opened_at')) else ""),
        actions_html=(f"<a class='sa-btn ghost sm' href='/p/{e(slug)}/'>Review decisions</a>" if waiting else ""))
    status = Counter(v.get("status") for v in s.votes.values())
    outcome_rows = [(name, sum(status.get(x, 0) for x in keys)) for name, keys, _c in OUTCOMES]
    flag_counts = Counter()
    for _, r in live:
        fl = [lbl for _t, lbl in flags(s, r)]
        for lbl in fl or ["No flag"]:
            flag_counts["Repeat contact" if lbl.startswith("Wrote ") or lbl.endswith(" cases") else lbl] += 1
    who = Counter(str(v.get("decided_by")) for v in s.votes.values() if v.get("decided_by"))
    stale = [h["job"] for h in s.health() if h["state"] == "stale"]
    alert = (f"<div class=banner style='background:var(--red-bg);color:var(--red)'>{icon('health')}<div><b>Needs a look:"
             f"</b> {e(', '.join(stale))} missed its last runs. <a href='/p/{e(slug)}/health'>Open health →</a>"
             f"</div></div>" if stale else "")
    periods = A.segmented([(f"{d} days", A.qs(f"/p/{slug}/overview", days=d if d != 14 else ""), d == n_days)
                           for d in PERIODS], label="Period")
    body = (f"{_paused_banner(s)}{alert}"
            + A.header("Insights", f"Last {n_days} days to {ref.strftime('%d %b %Y')} (UTC). Every number is read from "
                       "this product's records.", actions_html=periods, eyebrow_html=e(s.config.name))
            + f"<div class=ins>{tiles}</div>" + wait_panel
            + "<div class=grid2 style='margin-top:16px'>"
            + A.panel("New cases", ch_new + A.source("case records, by the day each case opened"), sub=f"Per day, last {n_days} days")
            + A.panel("Decisions made", ch_dec + A.source("decision journal; a decision counts once, at its first answer"),
                      sub=f"Per day, last {n_days} days \u00b7 green Yes, red No")
            + A.panel("Decision outcomes", A.hbars(outcome_rows, total=sum(status.values()) or 1)
                      + A.source("vote records, by current status"), sub=f"All {sum(status.values())} decisions (votes) by where they are now, all time")
            + A.panel("Cases by flag", A.hbars(flag_counts.most_common(), total=len(live) or 1)
                      + A.source("Inbox flags; a case can carry more than one"), sub=f"% of {len(live)} cases")
            + A.panel("Who decided", A.hbars(who.most_common(6)) if who else "<p class=sa-faint>No decision yet.</p>",
                      sub="Every recorded decider, all time")
            + A.panel("Scope", "<p class=sa-faint style='margin:0;font-size:13px'>The period buttons change the KPI "
                      "tiles and the two daily charts. Outcomes, flags and deciders count all time.</p>")
            + "</div>"
            "<p class=notmeasured>Not measured yet, so not shown: AI cost per case, first reply time in business hours "
            "and customer satisfaction. The core does not record them.</p>")
    return page("Insights", body, products, slug, "overview", snap=s)


def _decision_material(s: Snapshot, v: dict, rec: dict) -> str:
    kind, sp = v.get("kind"), (v.get("spec") or {})
    if kind == "reply":
        text = s.draft_text(v)
        if text is None:
            return "<p class=faint>Draft file not readable.</p>"
        return (f"<div class=mail><div class=mh><b>From</b><span>{e(s.config.support_address)}</span><b>To</b>"
                f"<span>{e(rec.get('customer'))}</span><b>Subject</b><span>Re: {e(rec.get('title'))}</span></div>"
                f"<div class=mb>{e(_strip_subject(text))}</div></div>")
    if kind == "money":
        cap = s.config.policy.refund_max_minor
        cur = ", ".join(s.config.policy.currencies).upper()
        ids = "".join(f"<dt>{lbl}</dt><dd><code>{e(sp[k])}</code></dd>" for lbl, k in
                      (("Charge", "charge"), ("Subscription", "subscription"), ("Customer", "customer")) if sp.get(k))
        from sayso.brand import app as A
        amt = (A.money(sp.get("amount"), sp.get("currency")) if sp.get("action") == "refund" and sp.get("amount") is not None
               else e(money_text(sp)))
        return (f"<div class=amount><span class=big>{'Refund ' if sp.get('action') == 'refund' else ''}{amt}</span>"
                + (f"<span class=faint>of a {A.money_plain(cap, (s.config.policy.currencies or ('usd',))[0])} cap per "
                   f"refund</span>" if cap else "") + "</div>"
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
            f"<button class=yes name=answer value=yes>{e(YES_LABEL.get(v.get('kind'), 'Approve'))}</button>"
            f"<button class=no name=answer value=no>Decline</button>"
            f"<span class=faint>Approves only the exact version shown here.</span></form>")


def _console_form(s: Snapshot, action: str, label: str, cls: str = "") -> str:
    return (f"<form class=inline method=post action='/p/{e(s.config.slug)}/{action}'>"
            f"<input type=hidden name=csrf value='{e(s.console['csrf'])}'><button class='{cls}'>{e(label)}</button></form>")


def render_swipe(s: Snapshot, products: list[str]) -> str:
    """One decision at a time: drag right for Yes, left for No. Signed-in operators only (the console)."""
    from sayso.brand import swipe
    slug = s.config.slug
    waiting = s.open_votes()
    if not s.console or not s.console.get("can_vote", True):
        why = (s.console or {}).get("vote_off_reason") or "sign in to the console to vote"
        body = head("Swipe", "Voting from here is switched off", crumb=e(s.config.name)) + \
            f"<div class=empty><b>Nothing to swipe</b>{e(why)}</div>"
        return page("Swipe", body, products, slug, "swipe", snap=s)
    # A card this operator already answered (for this exact version) waits for the next run, not for them.
    mine = _read_json(s.config.state_dir / "web-votes.json", {})
    op = s.console.get("op")
    waiting = [v for v in waiting
               if not (op and (mine.get(v.get("key")) or {}).get(op, {}).get("fingerprint") == v.get("fingerprint"))]
    cards = []
    for v in waiting:
        rec = s.cases.get(v.get("case_key"), {})
        kind = v.get("kind")
        label = KIND_LABEL.get(kind, kind)
        if kind == "money":
            label = "Refund" if (v.get("spec") or {}).get("action") == "refund" else "Money"
        yes = YES_LABEL.get(kind, "Approve")
        no = "Decline"
        cards.append(swipe.card_form(
            action=f"/p/{slug}/vote", csrf=s.console["csrf"], key=str(v.get("key")),
            fingerprint=str(v.get("fingerprint")), question=str(v.get("question") or v.get("subject") or label),
            who=f"{label} · {rec.get('customer') or ''}",
            body_html=_decision_material(s, v, rec), yes_label=yes, no_label=no,
            yes_hint=yes, no_hint=no))
    body = (f"{_paused_banner(s)}" + head("Swipe", "Right is Yes, left is No. Each answer is for the exact version shown.",
                                          crumb=e(s.config.name))
            + f"<style>{swipe.css()}</style>"
            + swipe.deck(cards, script_url="/static/swipe.js",
                         done_html="<b>All done</b><br>Nothing is waiting. New decisions appear here."))
    return page("Swipe", body, products, slug, "swipe", snap=s)


DASH = "\u2014"
NO_VALUE = "<span class=sa-faint>\u2014</span>"

MONEY_WORDS = re.compile(r"\b(refund|charged?|cancel(?:led|ed)?|invoice|billing|money back|chargeback)\b", re.I)
REPEAT_WORDS = re.compile(r"\b(?:(?:second|third|fourth|fifth|\d+(?:st|nd|rd|th))\s+(?:time|request|email|message|attempt)"
                          r"|(?:asking|writing|emailing|following up)\s+again|still\s+(?:waiting|no\b)"
                          r"|no\s+(?:reply|response|answer)\s+(?:yet|from))\b", re.I)
QUEUE_GROUPS = ("Money & billing", "Repeat contacts", "Everything else")
YES_LABEL = {"reply": "Approve & send", "money": "Approve refund", "merge": "Approve merge", "close": "Approve close"}


def customer_messages(s: "Snapshot", rec: dict) -> list[str]:
    return [m for m in s.card_messages(rec.get("card_id", "")) if str(m).startswith("**Customer wrote**")]


def flags(s: "Snapshot", rec: dict, v: dict | None = None) -> list[tuple[str, str]]:
    """Triage hints read from what is recorded: a money vote, a billing topic, a customer who really wrote more
    than once (or another case from the same address), or a subject with a real repeat phrase."""
    out: list[tuple[str, str]] = []
    title = str(rec.get("title") or (v or {}).get("subject") or "")
    if v is not None and v.get("kind") == "money":
        out.append(("warn", "Money"))
    elif MONEY_WORDS.search(title):
        out.append(("neutral", "Billing"))
    n = len(customer_messages(s, rec)) if rec.get("card_id") else 0
    who = str(rec.get("customer") or "").strip().lower()
    others = sum(1 for r in s.cases.values() if who and str(r.get("customer") or "").strip().lower() == who
                 and not r.get("folded_into"))
    if n >= 2:
        out.append(("warn", f"Wrote {n} times"))
    elif others >= 2:
        out.append(("warn", f"{others} cases"))
    elif REPEAT_WORDS.search(title):
        out.append(("warn", "Repeat contact"))
    return out


def queue_group(v: dict, fl: list[tuple[str, str]]) -> int:
    if v.get("kind") == "money" or any(lbl in ("Money", "Billing") for _, lbl in fl):
        return 0
    if any(lbl.startswith("Wrote ") or lbl.endswith(" cases") or lbl == "Repeat contact" for _, lbl in fl):
        return 1
    return 2


def queue(s: "Snapshot", sort: str = "priority") -> list[tuple[dict, list[tuple[str, str]]]]:
    """Open votes in queue order. priority: money/billing, then repeat contacts, then oldest first in each."""
    items = [(v, flags(s, s.cases.get(v.get("case_key"), {}), v)) for v in s.open_votes()]
    if sort == "newest":
        return sorted(items, key=lambda it: str(it[0].get("opened_at", "")), reverse=True)
    if sort == "oldest":
        return sorted(items, key=lambda it: str(it[0].get("opened_at", "")))
    return sorted(items, key=lambda it: (queue_group(*it), str(it[0].get("opened_at", ""))))


def wait_badges(s: "Snapshot", v: dict, ref: datetime) -> str:
    """How long it has waited, and 'Expires in N h' once the policy's vote expiry is near (expiry is real)."""
    from sayso.brand import app as A
    at = _at(v.get("opened_at"))
    if at is None:
        return ""
    secs = (ref - at).total_seconds()
    tone = "warn" if secs >= 4 * 3600 else "neutral"
    out = A.badge(f"Waiting {A.long_age(secs)}", tone, title=f"Opened {when(v.get('opened_at'))}")
    left = s.config.policy.vote_expiry_hours * 3600 - secs
    if 0 < left <= 12 * 3600:
        out += " " + A.badge(f"Expires in {A.long_age(left)}", "bad",
                             title=f"Votes nobody answers expire after {s.config.policy.vote_expiry_hours} h; nothing is done")
    return out


def _kind_label(v: dict) -> str:
    kind = v.get("kind")
    if kind == "money":
        return "Refund" if (v.get("spec") or {}).get("action") == "refund" else "Money"
    return KIND_LABEL.get(kind, str(kind))


def _version_of(v: dict) -> str:
    """What a Yes is bound to, in words: 'reply version 2', 'refund of $29.00 USD on ch_x', 'head abc123'."""
    from sayso.brand import app as A
    key = str(v.get("key") or "")
    sp = v.get("spec") or {}
    if v.get("kind") == "reply" and re.search(r":v\d+$", key):
        return "reply version " + key.rsplit(":v", 1)[-1]
    if v.get("kind") == "merge":
        return "head " + str(sp.get("head", ""))[:12]
    if v.get("kind") == "money" and sp.get("action") == "refund":
        return f"a refund of {A.money_plain(sp.get('amount'), sp.get('currency'))}" + (
            f" on {sp.get('charge')}" if sp.get("charge") else "")
    if v.get("kind") == "money":
        return str(sp.get("action", "this money action")).replace("_", " ")
    return "this exact request"


def render_decisions(s: Snapshot, products: list[str], sort: str = "priority") -> str:
    from sayso.brand import app as A
    slug, ref = s.config.slug, ref_time(s)
    sort = sort if sort in ("priority", "oldest", "newest") else "priority"
    items = queue(s, sort)
    base = f"/p/{slug}/"
    body_items, last_group = "", None
    for v, fl in items:
        rec = s.cases.get(v.get("case_key"), {})
        g = queue_group(v, fl)
        if sort == "priority" and g != last_group:
            n_g = sum(1 for vv, ff in items if queue_group(vv, ff) == g)
            body_items += f"<h2 class=qgroup>{e(QUEUE_GROUPS[g])} <span class=c>{n_g}</span></h2>"
            last_group = g
        chips = " ".join(A.badge(lbl, tone) for tone, lbl in fl)
        sp = v.get("spec") or {}
        amount = (f"<span class=qamt>{A.money(sp.get('amount'), sp.get('currency'))}</span>"
                  if v.get("kind") == "money" and sp.get("action") == "refund" else "")
        body_items += (
            f"<article class='item dec' id='d-{e(v.get('key'))}'><div class=item-head>{avatar(rec.get('customer'))}"
            f"<div class=who><div class=eyebrow><b>{e(_kind_label(v))}</b> · {e(rec.get('customer'))}</div>"
            f"<div class=title>{e(v.get('subject'))}</div><div class=q>{e(v.get('question'))}</div>"
            f"<div class=qflags>{wait_badges(s, v, ref)} {chips}</div></div>"
            f"<div class=meta>{amount}</div></div>"
            f"<div class=item-body>{_decision_material(s, v, rec)}"
            f"{A.trust_line(str(v.get('fingerprint') or ''), version=_version_of(v))}</div>"
            f"{_ballots_html(s, v)}{_vote_form(s, v)}<div class=item-foot>"
            f"<a href='/p/{e(slug)}/case/{e(v.get('case_key'))}'>Open case →</a></div></article>")
    if items:
        qrows = "".join(
            f"<a class=qrow href='#d-{e(v.get('key'))}'>{avatar(s.cases.get(v.get('case_key'), {}).get('customer'))}"
            f"<span class=qw><b>{e(s.cases.get(v.get('case_key'), {}).get('customer') or '')}</b>"
            f"<span>{e(_kind_label(v))} \u00b7 {e(s.cases.get(v.get('case_key'), {}).get('title') or v.get('subject'))}</span></span>"
            f"<span class=qk>{e(_kind_label(v))}</span>"
            f"<span class=qa>{A.money((v.get('spec') or {}).get('amount'), (v.get('spec') or {}).get('currency')) if v.get('kind') == 'money' and (v.get('spec') or {}).get('action') == 'refund' else ''}</span>"
            f"<span class=qt>{A.reltime(v.get('opened_at'), ref)}</span></a>" for v, _ in items)
        body_items = (f"<div class=dsplit><div class=queue role=navigation aria-label='Queue'><div class=qhead><b>Queue</b><span>{len(items)} waiting \u00b7 "
                      f"{'money and billing first, then repeat contacts, then oldest' if sort == 'priority' else sort + ' first'}"
                      f"</span></div>{qrows}</div><div class=dcards>" + body_items + "</div></div>")
    if not body_items:
        body_items = A.empty("Nothing waiting for your say-so",
                             "New reply drafts, refunds, merges and close requests appear here as the loop opens them.")
    n = len(items)
    money_n = sum(1 for v, _ in items if v.get("kind") == "money")
    sub = (f"{n} waiting for your say-so" + (f" · {money_n} refund{'s' if money_n != 1 else ''}" if money_n else "")
           if n else "Nothing waiting")
    actions = A.segmented([(lbl, A.qs(base, sort=key if key != "priority" else ""), sort == key)
                           for key, lbl in (("priority", "Priority"), ("oldest", "Oldest"), ("newest", "Newest"))],
                          label="Sort")
    if s.console and s.console.get("can_vote", True) and n:
        actions = (A.segmented([("List", base, True), ("Review one by one", f"/p/{slug}/swipe", False)], label="Mode")
                   + actions)
    if s.console and s.console.get("can_tick"):
        actions = (f"<form class=tickf method=post action='/p/{e(slug)}/tick'><input type=hidden name=csrf "
                   f"value='{e(s.console['csrf'])}'><button class='sa-btn ghost sm' title='Dry-run products only'>"
                   f"Run the loop once</button></form>" + actions)
    note = ""
    if items:
        note = (f"<div class=note>{icon('lock')}<span>No approve-all: each Yes is its own, for the exact version on its "
                f"card.</span></div>"
                if s.console else
                f"<div class=note>{icon('lock')}<span>Approve or decline on the board poll. This page cannot approve, "
                f"so nothing here can act by mistake.</span></div>")
    body = (f"{_paused_banner(s)}"
            + A.header("Decisions", sub, actions_html=actions, eyebrow_html=e(s.config.name))
            + note
            + body_items)
    return page(s.config.name, body, products, slug, "", snap=s)


CASE_ORDER = (stages.AWAITING, stages.NEW, stages.IN_SUPPORT, stages.IN_DEV, stages.IN_QA, stages.BLOCKED, stages.DONE)


INBOX_VIEWS = (("", "All"), ("waiting", "Waiting on you"), ("open", "In progress"), ("closed", "Closed"))


def _inbox_view(s: Snapshot, rec: dict, n_open: int) -> str:
    st = s.stage(rec["card_id"])
    if n_open:
        return "waiting"
    return "closed" if st == stages.DONE else "open"


def _preview(s: Snapshot, rec: dict) -> str:
    msgs = customer_messages(s, rec)
    if not msgs:
        return ""
    text = re.sub(r"<[^>]{0,200}>", " ", str(msgs[-1]).split("\n\n", 1)[-1])
    flat = " ".join(text.split())
    return flat[:300]


def render_board(s: Snapshot, products: list[str], view: str = "", q: str = "") -> str:
    from sayso.brand import app as A
    slug, ref = s.config.slug, ref_time(s)
    view = view if view in dict(INBOX_VIEWS) else ""
    needle = q.strip().lower()
    open_votes = s.open_votes()
    rows_by_view: dict[str, list] = {k: [] for k, _ in INBOX_VIEWS}
    unread = []
    for key, rec in sorted(s.cases.items(), key=lambda kv: str(kv[1].get("last_inbound") or kv[1].get("created_at", "")),
                           reverse=True):
        if not rec.get("card_id") or rec.get("folded_into"):
            continue
        n_open = sum(1 for v in open_votes if v.get("case_key") == key)
        st = s.stage(rec["card_id"])
        if st == "unreadable":
            unread.append((key, rec))
        hay = " ".join(str(x) for x in (rec.get("title"), rec.get("customer"), rec.get("ticket"), key, _preview(s, rec)))
        if needle and needle not in hay.lower():
            continue
        fl = flags(s, rec)
        if any(v.get("kind") == "money" for v in open_votes if v.get("case_key") == key):
            fl = [("warn", "Money")] + [f for f in fl if f[1] != "Billing"]
        kinds = [_kind_label(v) for v in open_votes if v.get("case_key") == key]
        badges = (A.badge("Needs you \u00b7 " + " + ".join(dict.fromkeys(kinds)), "warn",
                          title=f"{n_open} decision{'s' if n_open != 1 else ''} waiting") if n_open else
                  A.badge("Closed", "neutral") if st == stages.DONE else
                  A.badge(st, "bad") if st in (stages.BLOCKED, "unreadable") else A.badge(st, "neutral")) + "".join(
            " " + A.badge(lbl, tone) for tone, lbl in fl)
        ticket = (" \u00b7 " + e(rec.get("ticket"))) if rec.get("ticket") else ""
        cells = [f"<div class=ib-who>{avatar(rec.get('customer'))}<div><div class=sa-cell-t>{e(rec.get('title'))}</div>"
                 f"<div class=sa-cell-s>{e(rec.get('customer'))}{ticket}"
                 f" \u00b7 {e(_preview(s, rec))}</div></div></div>",
                 f"<div class=ib-flags>{badges}</div>",
                 A.reltime(rec.get("last_inbound") or rec.get("created_at"), ref)]
        row = {"cells": cells, "href": f"/p/{slug}/case/{key}", "cls": "tr-done" if st == stages.DONE else "",
               "rank": (0 if any(v.get("kind") == "money" for v in open_votes if v.get("case_key") == key) else 1) if n_open
               else (3 if st == stages.DONE else 2)}
        rows_by_view[""].append(row)
        rows_by_view[_inbox_view(s, rec, n_open)].append(row)
    base = f"/p/{slug}/board"
    tabs = A.tabs([(lbl, A.qs(base, view=k, q=q), len(rows_by_view[k]), k == view) for k, lbl in INBOX_VIEWS],
                  label="Status")
    finder = A.search(base, q, placeholder="Name, subject or ticket", keep={"view": view}) if s.console else (
        f"<span class=sa-faint>Search: add <code>?q=</code> to the address</span>" if not q else
        f"<span class=sa-faint>Showing matches for \u201c{e(q)}\u201d \u00b7 <a href='{e(A.qs(base, view=view))}'>clear</a></span>")
    rows = sorted(rows_by_view[view], key=lambda r: r["rank"])   # stable: newest email first inside each rank
    empty = A.empty("No cases match" if needle else "No cases here yet",
                    "Try another word or clear the search." if needle else
                    "Each customer email opens one case per problem. They appear here as intake reads the mailbox.")
    table = A.table([("Case", ""), ("Status", ""), ("Last email", "num")], rows, empty_html=empty,
                    caption="Cases", cls="inbox")
    extra = ""
    if unread:
        extra = (f"<p class=sa-src>{len(unread)} case(s) have a stage the board could not read; they show as "
                 f"unreadable, never guessed.</p>")
    total = len(rows_by_view[""])
    body = (f"{_paused_banner(s)}"
            + A.header("Inbox", f"{total} case{'s' if total != 1 else ''} \u00b7 money first, then other cases that need you, then "
                       "newest email", eyebrow_html=e(s.config.name))
            + f"<div class=sa-bar>{tabs}{finder}</div>{table}{extra}")
    return page("Inbox", body, products, slug, "board", snap=s)


def _mail_parts(text: str) -> tuple[dict, str]:
    """A '**Customer wrote**' card message -> (headers, body). Headers are shown as text, never as markup."""
    raw = str(text).split("\n", 1)[-1] if str(text).startswith("**") else str(text)
    head_txt, _, body = raw.partition("\n\n")
    heads = {}
    for line in head_txt.splitlines():
        k, sep, v = line.partition(": ")
        if sep and k in ("From", "To", "Date", "Subject"):
            heads[k] = v
    return (heads, body) if heads else ({}, raw)


def render_case(s: Snapshot, key: str, products: list[str]) -> str | None:
    from sayso.brand import app as A
    rec = s.cases.get(key)
    if rec is None:
        return None
    slug, ref = s.config.slug, ref_time(s)
    msgs = s.card_messages(rec["card_id"])
    convo = ""
    for m in msgs:
        if str(m).startswith("**Customer wrote**"):
            h, body_txt = _mail_parts(m)
            convo += (f"<div class='cv cust'><div class=cv-h>{avatar(rec.get('customer'))}<b>{e(h.get('From') or rec.get('customer'))}"
                      f"</b><span class=sa-faint>{A.reltime(h.get('Date'), ref, long=True, suffix=' ago')}</span></div>"
                      f"<div class='msg'>{e(body_txt.strip())}</div>"
                      + (f"<details><summary>Email headers</summary><dl class=kv>"
                         + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in h.items()) + "</dl></details>" if h else "")
                      + "</div>")
    if not convo:
        convo = "<p class=faint>The customer's email is on the board card.</p>"
    notes = [m for m in msgs[1:] if not str(m).startswith("**Customer wrote**")]
    notes_html = "".join(f"<div class=ev><span class='dot'></span><div class=body><div class=msg>{message_html(m)}</div>"
                         f"</div></div>" for m in notes)
    events = [j for j in s.journal if j.get("case_key") == key or str(j.get("key", "")).find(key) >= 0]
    timeline = "".join(
        f"<div class=ev><span class='dot t-{EVENT_TONE.get(j.get('event'), 'grey')}'></span><div class=body>"
        f"<div>{e(event_text(s, j))}</div><div class=when>{A.reltime(j.get('at'), ref, long=True, suffix=' ago')} \u00b7 "
        f"{e(when(j.get('at')))}</div></div></div>"
        for j in events) or "<p class=faint>No events yet.</p>"
    # decision panel: every open vote on this case, with its material, the lock and the buttons
    panel = ""
    for v in s.open_votes():
        if v.get("case_key") != key:
            continue
        panel += (f"<div class=cdec><div class=cdec-h><b>{e(_kind_label(v))}</b>{wait_badges(s, v, ref)}</div>"
                  f"<div class=q>{e(v.get('question'))}</div>{_decision_material(s, v, rec)}"
                  f"{A.trust_line(str(v.get('fingerprint') or ''), version=_version_of(v))}"
                  f"{_ballots_html(s, v)}{_vote_form(s, v)}</div>")
    if not panel:
        panel = "<p class=faint style='margin:0;font-size:13px'>Nothing waiting on this case.</p>"
    vrows = ""
    for vk, v in sorted(s.votes.items(), key=lambda kv: kv[1].get("opened_at", "")):
        if v.get("case_key") != key or v.get("status") == "open":
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
    for vk, att in s.attempts.items():
        if s.votes.get(vk, {}).get("case_key") == key and s.votes[vk].get("kind") == "money":
            rc = att.get("receipt") or {}
            receipts += (f"<div style='margin-bottom:8px'>{exec_pill(att.get('status'))}<div class=faint "
                         f"style='font-size:12px;margin-top:2px'>Refund {A.money(rc.get('amount'), rc.get('currency'))}"
                         f" · <code>{e(rc.get('provider_id'))}</code></div></div>")
    props = (f"<dl style='margin:0'><div class=prop><dt>Stage</dt><dd>{stage_pill(s.stage(rec['card_id']))}</dd></div>"
             f"<div class=prop><dt>Customer</dt><dd>{e(rec.get('customer'))}</dd></div>"
             f"<div class=prop><dt>Type</dt><dd>{e(rec.get('kind'))}</dd></div>"
             f"<div class=prop><dt>Ticket</dt><dd>{e(rec.get('ticket') or '—')}</dd></div>"
             f"<div class=prop><dt>Opened</dt><dd>{e(when(rec.get('created_at')))}</dd></div>"
             f"<div class=prop><dt>Case key</dt><dd><code>{e(key)}</code></dd></div></dl>")
    none = "<p class=faint style='margin:0;font-size:13px'>None</p>"
    chips = "".join(" " + A.badge(lbl, tone) for tone, lbl in flags(s, rec))
    body = (f"{_paused_banner(s)}"
            + A.header(str(rec.get("title")), f"{rec.get('customer') or ''}",
                       eyebrow_html=f"<a href='/p/{e(slug)}/board'>Inbox</a> / {e(rec.get('ticket') or key)}",
                       meta_html=stage_pill(s.stage(rec["card_id"])) + chips)
            + "<div class=case><div class=thread>"
            f"<h2>Customer wrote</h2>{convo}"
            f"<h2>Timeline</h2>{timeline}"
            f"<details class=cnotes><summary>Board thread <span class=c>{len(notes)}</span></summary>"
            f"{notes_html or '<p class=faint>No further messages.</p>'}</details></div>"
            f"<div class=props><h3 style='margin-top:0'>Decision</h3>{panel}<h3>Receipts</h3>{receipts or none}"
            f"<h3>Properties</h3>{props}<h3>Votes</h3>{vrows or none}<h3>Drafts</h3>{drafts or none}</div></div>")
    return page(str(rec.get("title")), body, products, slug, "case", snap=s)


MONEY_VIEWS = (("", "All"), ("waiting", "Waiting"), ("done", "Refunded"), ("declined", "Declined"))
PROVIDER_CHECK = {"VERIFIED_WITH_PROVIDER": ("Verified with provider", "good"), "UNVERIFIED": ("Not verified", "bad"),
                  "pending": ("Running", "warn"), "PENDING_WITH_PROVIDER": ("Waiting for provider approval", "warn")}


def _money_view(r: dict) -> str:
    if r["vote"] == "open":
        return "waiting"
    if r["vote"] in ("approved", "done"):
        return "done"
    return "declined"


def render_money(s: Snapshot, products: list[str], view: str = "") -> str:
    from sayso.brand import app as A
    slug, ref = s.config.slug, ref_time(s)
    view = view if view in dict(MONEY_VIEWS) else ""
    p = s.config.policy
    cur = (p.currencies[0] if p.currencies else "usd")
    all_rows = s.money_rows()
    sums = {"waiting": 0, "verified": 0, "declined": 0}
    counts = {k: 0 for k, _ in MONEY_VIEWS}
    rows = []
    for r in all_rows:
        sp = r["spec"]
        amt = sp.get("amount", 0) if sp.get("action") == "refund" else 0
        v_view = _money_view(r)
        counts[""] += 1
        counts[v_view] += 1
        if r["vote"] == "open":
            sums["waiting"] += amt
        if r["execution"] == "VERIFIED_WITH_PROVIDER":
            sums["verified"] += amt
        if r["vote"] in ("declined", "void", "expired"):
            sums["declined"] += amt
        if view and v_view != view:
            continue
        rec = s.cases.get(r["case_key"], {})
        vote = s.votes.get(r["key"], {})
        status = {"open": ("Waiting", "warn"), "approved": ("Approved", "neutral"), "done": ("Refunded", "good"),
                  "declined": ("Declined", "neutral"), "void": ("Voided", "neutral"),
                  "expired": ("Expired", "neutral")}.get(r["vote"], (str(r["vote"]), "neutral"))
        check = PROVIDER_CHECK.get(r["execution"])
        check_html = (A.badge(check[0], check[1], title=r["execution"]) if check else
                      "<span class=sa-faint>Runs after approval</span>" if r["vote"] == "open" else
                      "<span class=sa-faint>Not run</span>")
        who = operator(s, r["decided_by"])
        decision = (f"<div>{e(who)}</div><div class=sa-cell-s>{A.reltime(vote.get('decided_at'), ref, long=True, suffix=' ago')}</div>"
                    if who else
                    f"<div>Awaiting a Yes</div><div class=sa-cell-s>opened {A.reltime(vote.get('opened_at'), ref, long=True, suffix=' ago')}</div>"
                    if r["vote"] == "open" else
                    f"<div class=sa-faint>{e(status[0])}</div>")
        if r["execution"] in ("-", None, "") and r["vote"] == "open":
            check_html = A.badge("Pending", "neutral", title="Runs once after a Yes, then is checked with the provider")
        what = ("Refund" if sp.get("action") == "refund" else
                "Cancel subscription" if sp.get("action") == "cancel_subscription" else str(sp.get("action", "")))
        rows.append({"cells": [
            f"<div class=sa-cell-t>{e(rec.get('customer') or DASH)}</div><div class=sa-cell-s>{e(r['subject'])}</div>",
            f"<div>{e(what)}</div><div class=sa-cell-s><code>{e(sp.get('charge') or sp.get('subscription') or '')}</code></div>",
            A.money(amt, sp.get("currency")) if amt else "<span class=sa-faint>\u2014</span>",
            A.badge(*status), decision, check_html], "href": f"/p/{slug}/case/{r['case_key']}"})
    waiting_n = counts["waiting"]
    tiles = A.tiles([
        A.tile("Waiting for a decision", A.money(sums["waiting"], cur), hot=bool(sums["waiting"]),
               note=f"{waiting_n} request{'s' if waiting_n != 1 else ''}",
               link_html=f"<a href='/p/{e(slug)}/'>Review in Decisions</a>" if waiting_n else ""),
        A.tile("Refunded and verified", A.money(sums["verified"], cur), note=f"{counts['done']} refunded",
               source="Read back from the payment provider"),
        A.tile("Declined or not run", A.money(sums["declined"], cur), note=f"{counts['declined']} not run",
               source="Declined, voided or expired votes"),
        A.tile("Refund cap (setting)", A.money(p.refund_max_minor, cur) if p.refund_max_minor else e("None set"),
               note="Per refund; larger requests are refused", source="policy.refund_max_minor in the settings file"),
    ])
    base = f"/p/{slug}/money"
    tabs = A.tabs([(lbl, A.qs(base, view=k), counts[k], k == view) for k, lbl in MONEY_VIEWS], label="Status")
    table = A.table([("Customer", ""), ("Action", ""), ("Amount", "num"), ("Status", ""), ("Decision", ""),
                     ("Provider check", "")], rows, caption="Refunds",
                    empty_html=A.empty("No refunds here", "A refund appears when a draft or an operator asks for one. "
                                       "It runs only after a Yes, once, and is then checked with the payment provider."))
    body = (f"{_paused_banner(s)}"
            + A.header("Refunds", "Every refund waits for a person's Yes, runs once, then is checked with the payment "
                       "provider.", eyebrow_html=e(s.config.name))
            + tiles + f"<div class=sa-bar>{tabs}</div>{table}"
            + f"<p class=sa-src>{counts['']} request{'s' if counts[''] != 1 else ''} \u00b7 from the vote and attempt "
              f"records in this product's state folder.</p>")
    return page("Refunds", body, products, slug, "money", snap=s)


JOB_TITLE = {"intake": "Read new email", "votes": "Count decisions", "release": "Merge approved fixes",
             "money": "Run approved refunds", "sender": "Send approved replies", "dev": "Start fix runs",
             "qa": "Check fixes", "draft": "Write drafts", "close": "Close finished cases",
             "reconcile": "Compare sent with approved", "stall": "Find stalled cases", "health": "Self-check"}
JOB_FIX = {
    "intake": "Check the mailbox and helpdesk settings. New email waits in the mailbox and is read on the next run.",
    "votes": "Nothing is sent while this is stale. Check the board adapter, then run it once by hand.",
    "release": "No merge happens while this is stale. Check the code host token.",
    "money": "A refund is never repeated; check the payment provider before you act by hand.",
    "sender": "Approved replies wait. Check the mailbox/helpdesk send settings.",
    "dev": "Dev cards wait for a run. Check the dev runner.",
    "qa": "Fixes wait for QA. Check the QA runner.",
    "draft": "Cases wait for a draft; nothing is sent without one. Check the drafter.",
    "close": "Quiet cases stay open a little longer. No customer impact.",
    "reconcile": "Drift reports are late. It never repairs anything on its own.",
    "stall": "Stall reminders are late.",
    "health": "Nothing is watching the other jobs. Check the timers.",
}


def job_summary(result: Any) -> str:
    """A heartbeat result in words: '1 opened, 3 open' from "{'opened': 1, 'open': 3, 'held': []}". Zero and empty
    values are left out; nothing left reads 'Nothing to do'. Unparseable text is shown as it is."""
    import ast
    text = str(result or "").strip()
    try:
        data = ast.literal_eval(text) if text.startswith("{") else None
    except (ValueError, SyntaxError):
        data = None
    if not isinstance(data, dict):
        return text or "Nothing recorded"
    parts = []
    for k, v in data.items():
        if isinstance(v, bool):
            if v:
                parts.append(k.replace("_", " "))
        elif isinstance(v, (int, float)) and v:
            parts.append(f"{v:g} {k.replace('_', ' ')}")
        elif isinstance(v, (list, tuple, set)) and v:
            parts.append(f"{k.replace('_', ' ')}: {', '.join(str(x) for x in list(v)[:6])}" + (" \u2026" if len(v) > 6 else ""))
    return ", ".join(parts) if parts else "Nothing to do"


def render_health(s: Snapshot, products: list[str]) -> str:
    from sayso.brand import app as A
    from sayso.jobs import ORDER
    slug, ref = s.config.slug, ref_time(s)
    rows = {h["job"]: h for h in s.health()}
    order = [j for j in ORDER if j in rows] + [j for j in rows if j not in ORDER]
    ran = [rows[j] for j in order if rows[j]["state"] != "never"]
    stale = [h for h in ran if h["state"] == "stale"]
    if s.paused:
        state, tone, line = "Paused", "warn", f"Jobs that act are stopped: {s.pause_reason or 'no reason given'}."
    elif stale:
        state, tone, line = ("Needs attention", "bad",
                             f"{len(stale)} job{'s' if len(stale) != 1 else ''} missed {'their' if len(stale) != 1 else 'its'} "
                             "last runs. What to do is next to each one.")
    elif not ran:
        state, tone, line = "Not started", "neutral", "No job has run yet. Install the timers or run `sayso tick` once."
    else:
        state, tone, line = "Running", "good", f"{len(ran)} jobs ran on time."
    last = max((h["at"] for h in ran), default="")
    jobs = ""
    for i, j in enumerate(order, 1):
        h = rows[j]
        badge = {"ok": A.badge("On time", "good"), "stale": A.badge("Stale", "bad"),
                 "never": A.badge("Not run", "neutral")}.get(h["state"], A.badge(h["state"]))
        every = s.config.jobs.get(j)
        fix = ""
        if h["state"] == "stale":
            fix = (f"<div class=hfix><b>What to do:</b> {e(JOB_FIX.get(j, 'Check this job.'))} Run it once by hand with "
                   f"<code>sayso run {e(j)} --config &lt;file&gt;</code> and read its log with "
                   f"<code>journalctl --user -u sayso-{e(slug)}-{e(j)}</code>.</div>")
        jobs += (f"<li class='hjob {e(h['state'])}'><span class=hn>{i}</span><div class=hmain><div class=hh>"
                 f"<b>{e(JOB_TITLE.get(j, j))}</b> <code class=raw>{e(j)}</code> <span class=hbadge>{badge}</span></div>"
                 f"<div class=mute>{e(JOB_HINT.get(j, ''))}</div>"
                 f"<div class=res>{e(job_summary(h['result'])[:200])}</div>"
                 f"<details class=hraw><summary>Raw result</summary><pre>{e(str(h['result'])[:600])}</pre></details>{fix}</div>"
                 f"<div class=hwhen>{A.reltime(h['at'], ref, long=True, suffix=' ago') if h['at'] else NO_VALUE}"
                 f"<div class=sa-faint>every {e(A.long_age(every) if every else '?')}</div></div></li>")
    alerts = [j for j in s.journal if j.get("event") == "alert"][-20:][::-1]
    arows = [[A.reltime(a.get("at"), ref, long=True, suffix=" ago"), e(a.get("text"))] for a in alerts]
    control = ((_console_form(s, "resume", "Resume the loop", "yes") if s.paused else
                _console_form(s, "pause", "Pause everything that acts", "no")) if s.console else "")
    body = (f"{_paused_banner(s)}"
            + A.header("Health", "Is the loop reading, drafting and sending for this product?",
                       eyebrow_html=e(s.config.name))
            + f"<div class='hstate {tone}'><div>{A.badge(state, tone)} <span>{e(line)}</span></div>"
              f"<div class=sa-faint>Last run {A.reltime(last, ref, long=True, suffix=' ago') if last else 'never'} \u00b7 "
              f"a job is stale after three missed intervals</div></div>"
            + control
            + f"<h2>Jobs <span class=c>in the order they run</span></h2><ol class=hjobs>{jobs}</ol>"
            + f"<h2>Recent alerts <span class=c>{len(alerts)}</span></h2>"
            + A.table([("When", ""), ("Alert", "")], arows,
                      empty_html=A.empty("No alerts", "The loop alerts instead of guessing. Alerts appear here and on "
                                         "the board's alerts channel."))
            + f"<p class=sa-src>Machine-readable: <a href='/api/{e(slug)}.json'>/api/{e(slug)}.json</a></p>")
    return page("Health", body, products, slug, "health", snap=s)


AUDIT_KINDS = (("", "All events"), ("decisions", "Decisions"), ("sends", "Sends & refunds"), ("cases", "Cases"),
               ("alerts", "Alerts & notices"), ("console", "Console"))
AUDIT_OF = {"vote_opened": "decisions", "vote_approved": "decisions", "vote_declined": "decisions",
            "vote_void": "decisions", "vote_expired": "decisions", "vote_policy_approved": "decisions",
            "web_vote": "decisions", "vote_done": "sends", "send_attempt": "sends", "case_opened": "cases",
            "mail_appended": "cases", "alert": "alerts", "notice": "alerts", "paused": "console", "resumed": "console",
            "console_tick": "console"}
AUDIT_CAP = 500


def _audit_case(s: Snapshot, j: dict) -> str | None:
    case = j.get("case_key") or next((c for c in s.cases if c in str(j.get("key", ""))), None)
    return case if case in s.cases else None


def _audit_actor(s: Snapshot, j: dict) -> str:
    who = j.get("decided_by") or j.get("by")
    return str(who) if who else "Sayso"


def _day(j: dict) -> str:
    at = _at(j.get("at"))
    return at.strftime("%a %d %b %Y") if at else "Unknown day"


def audit_rows(s: Snapshot, query: dict | None = None) -> list[dict]:
    """Journal lines, newest first, filtered by ?kind, ?case and ?q (actor, text, key or fingerprint)."""
    q = query or {}
    kind, case, needle = q.get("kind", ""), q.get("case", ""), str(q.get("q", "")).strip().lower()
    out = []
    for j in reversed(s.journal):
        if kind and AUDIT_OF.get(str(j.get("event")), "") != kind:
            continue
        c = _audit_case(s, j)
        if case and c != case:
            continue
        v = s.votes.get(str(j.get("key") or ""), {})
        hay = " ".join([event_text(s, j), _audit_actor(s, j), str(j.get("key", "")), str(v.get("fingerprint", "")),
                        str(s.cases.get(c, {}).get("title", "")) if c else ""]).lower()
        if needle and needle not in hay:
            continue
        out.append({"j": j, "case": c, "vote": v})
    return out


def render_audit(s: Snapshot, products: list[str], query: dict | None = None) -> str:
    from sayso.brand import app as A
    slug = s.config.slug
    q = {k: v for k, v in (query or {}).items() if k in ("kind", "case", "q")}
    if q.get("kind") not in dict(AUDIT_KINDS):
        q["kind"] = ""
    rows = audit_rows(s, q)
    shown = rows[:AUDIT_CAP]
    base = f"/p/{slug}/audit"
    counts = Counter(AUDIT_OF.get(str(j.get("event")), "") for j in s.journal)
    tabs = A.tabs([(lbl, A.qs(base, kind=k, case=q.get("case"), q=q.get("q")),
                    len(s.journal) if not k else counts.get(k, 0), q["kind"] == k) for k, lbl in AUDIT_KINDS],
                  label="Event type")
    finder = (A.search(base, q.get("q", ""), placeholder="Name, text, key or fingerprint",
                       keep={"kind": q.get("kind"), "case": q.get("case")}) if s.console else "")
    filt = ""
    if q.get("case") or q.get("q"):
        bits = []
        if q.get("case"):
            bits.append(f"case \u201c{e(s.cases.get(q['case'], {}).get('title', q['case']))}\u201d")
        if q.get("q"):
            bits.append(f"text \u201c{e(q['q'])}\u201d")
        filt = (f"<p class=sa-src>Filtered by {' and '.join(bits)} \u00b7 <a href='{e(A.qs(base, kind=q['kind']))}'>"
                f"clear</a></p>")
    per_day = Counter(_day(r["j"]) for r in shown)
    parts, cur_day = [], None
    for r in shown:
        j, v, c = r["j"], r["vote"], r["case"]
        day = _day(j)
        if day != cur_day:
            n_day = per_day[day]
            parts.append(f"<div class=aday><b>{e(day)}</b><span>{n_day} event{'s' if n_day != 1 else ''}</span></div>")
            cur_day = day
        at = _at(j.get("at"))
        ev = str(j.get("event"))
        tone = "bad" if ev == "alert" else ("good" if AUDIT_OF.get(ev) == "sends" else "neutral")
        fp = str(v.get("fingerprint") or "")
        lock = (f"<div class=sa-cell-s>{e(_version_of(v))} \u00b7 fingerprint <code title='{e(fp)}'>{e(fp[:12])}"
                f"\u2026</code></div>" if fp and (ev.startswith("vote_") or ev == "web_vote") else "")
        case_link = (f"<a href='{e(A.qs(base, case=c))}' title='Show only this case'>"
                     f"{e(s.cases.get(c, {}).get('title', c))}</a>" if c else "<span class=sa-faint>\u2014</span>")
        fields = {k: val for k, val in j.items() if k not in ("at", "event")}
        parts.append(
            f"<details class=arow><summary><span class=at><time datetime='{e(j.get('at'))}' title='{e(when(j.get('at')))}'>"
            f"{e(at.strftime('%H:%M:%S') if at else j.get('at'))}</time></span>"
            f"<span class=aa>{e(_audit_actor(s, j))}</span>"
            f"<span class=ae>{A.badge(ev.replace('_', ' ').capitalize(), tone)}</span>"
            f"<span class=aw>{e(event_text(s, j))}{lock}</span><span class=ac>{case_link}</span></summary>"
            f"<div class=adetail><dl class=kv><dt>When</dt><dd>{e(when(j.get('at')))}</dd>"
            + (f"<dt>Case</dt><dd><a href='/p/{e(slug)}/case/{e(c)}'>Open case \u2192</a></dd>" if c else "")
            + (f"<dt>Fingerprint</dt><dd><code>{e(fp)}</code></dd>" if fp else "")
            + f"</dl><pre>{e(json.dumps(fields, sort_keys=True, indent=1, default=str)[:2000])}</pre></div></details>")
    listing = (("<div class=alog><div class=ahead><span>Time (UTC)</span><span>Actor</span><span>Event</span>"
                "<span>What happened</span><span>Case</span></div>" + "".join(parts) + "</div>") if shown else
               A.empty("No events match", "Clear a filter to see more. Every email, vote, send and refund the loop "
                       "records lands here."))
    more = (f"<p class=sa-src>Showing the newest {AUDIT_CAP} of {len(rows)}. The CSV export has every matching line.</p>"
            if len(rows) > AUDIT_CAP else
            f"<p class=sa-src>{len(rows)} of {len(s.journal)} events, newest first, from the append-only journal "
            f"(<code>journal.jsonl</code>). Times in UTC.</p>")
    export = (f"<a class='sa-btn ghost sm' href='{e(A.qs(base + '.csv', **q))}' title='Every matching line, no cap'>"
              f"Export {len(rows)} event{'s' if len(rows) != 1 else ''} (CSV)</a>")
    body = (A.header("Audit log", "Every email, decision, send and refund the loop recorded, with the exact version "
                     "each Yes approved.", actions_html=export, eyebrow_html=e(s.config.name))
            + f"<div class=sa-bar>{tabs}{finder}</div>{filt}{listing}{more}")
    return page("Audit log", body, products, slug, "audit", snap=s)


def _csv_cell(value) -> str:
    """Spreadsheet-safe: a cell that starts like a formula gets a leading ' (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def audit_csv(s: Snapshot, query: dict | None = None) -> str:
    """Every matching journal line (no cap), same filters as the page."""
    import csv
    import io
    q = {k: v for k, v in (query or {}).items() if k in ("kind", "case", "q")}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at_utc", "event", "actor", "what", "case_key", "case_title", "vote_key", "fingerprint", "fields_json"])
    for r in audit_rows(s, q):
        j, v, c = r["j"], r["vote"], r["case"]
        fields = {k: val for k, val in j.items() if k not in ("at", "event")}
        w.writerow([_csv_cell(x) for x in (j.get("at"), j.get("event"), _audit_actor(s, j), event_text(s, j), c or "",
                                           s.cases.get(c, {}).get("title", "") if c else "", j.get("key", ""),
                                           v.get("fingerprint", ""), json.dumps(fields, sort_keys=True, default=str))])
    return buf.getvalue()


AUDIT_CSS = """
.alog{background:var(--card,#fff);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.ahead,.arow>summary{display:grid;grid-template-columns:78px 130px 150px minmax(0,1fr) minmax(0,220px);gap:12px;padding:9px 14px;
align-items:start}.ahead{font-size:12.5px;color:var(--soft);background:var(--card2,#f4f4f2);border-bottom:1px solid var(--line)}
.aday{display:flex;justify-content:space-between;padding:7px 14px;background:#fafaf8;border-bottom:1px solid var(--line);
font-size:12.5px;color:var(--soft)}.aday b{color:var(--ink);font-weight:600}
details.arow{margin:0;border-bottom:1px solid var(--line2)}.arow>summary{list-style:none;cursor:pointer;font-size:13.5px;
color:var(--ink2)}.arow>summary::-webkit-details-marker{display:none}.arow>summary:hover{background:#fafaf8}
.at{font-variant-numeric:tabular-nums;color:var(--soft);position:relative;padding-left:14px}
.at:before{content:'▸';position:absolute;left:0;color:var(--faint);font-size:11px}
details[open].arow .at:before{content:'▾'}.aa,.ac{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.adetail{padding:4px 14px 14px 104px}details.arow:not([open]) .adetail{display:none}
@media (max-width:860px){.ahead{display:none}.arow>summary{grid-template-columns:64px minmax(0,1fr);gap:4px 10px}
.arow>summary .aa,.arow>summary .ae,.arow>summary .aw,.arow>summary .ac{grid-column:2}.adetail{padding:4px 14px 14px}}
"""


# -- server ------------------------------------------------------------------------

def route(path: str, snapshot_for: Callable[[str], Snapshot], products: list[str]) -> tuple[int, str, str]:
    """Return (status, content type, body) for one GET path."""
    parts = [unquote(p) for p in urlparse(path).path.split("/") if p]
    query = {k: v[0][:200] for k, v in parse_qs(urlparse(path).query).items() if v}
    if not parts:
        return 200, "text/html", render_home({p: snapshot_for(p) for p in products})
    if parts[0] == "api" and len(parts) == 2 and parts[1].removesuffix(".json") in products:
        return 200, "application/json", json.dumps(snapshot_for(parts[1].removesuffix(".json")).as_json(), default=str)
    if parts[0] != "p" or len(parts) < 2 or parts[1] not in products:
        return 404, "text/html", page("Not found", "<h1>Not found</h1>", products, None)
    s = snapshot_for(parts[1])
    tab = parts[2] if len(parts) > 2 else ""
    if tab == "" and len(parts) == 2:
        return 200, "text/html", render_decisions(s, products, sort=query.get("sort", "priority"))
    if tab == "board" and len(parts) == 3:
        return 200, "text/html", render_board(s, products, view=query.get("view", ""), q=query.get("q", ""))
    if tab == "money" and len(parts) == 3:
        return 200, "text/html", render_money(s, products, view=query.get("view", ""))
    if tab == "overview" and len(parts) == 3:
        return 200, "text/html", render_overview(s, products, days=query.get("days", ""))
    if tab == "audit" and len(parts) == 3:
        return 200, "text/html", render_audit(s, products, query)
    if tab == "audit.csv" and len(parts) == 3:
        return 200, "text/csv", audit_csv(s, query)
    simple = {"board": render_board, "money": render_money, "health": render_health, "audit": render_audit,
              "overview": render_overview}
    if tab in simple and len(parts) == 3:
        return 200, "text/html", simple[tab](s, products)
    if tab == "swipe" and len(parts) == 3:
        return 200, "text/html", render_swipe(s, products)
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
            if ctype == "text/csv":
                self.send_header("Content-Disposition", 'attachment; filename="sayso-audit.csv"')
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
