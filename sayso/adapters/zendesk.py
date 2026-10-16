"""Zendesk Support as the helpdesk (tickets + public comments). Stdlib only.

Calls (Zendesk Ticketing API v2, docs cited in docs/ADAPTERS.md):
  inbound    GET  /api/v2/incremental/ticket_events?include=comment_events   (ADMIN token; 10 req/min)
  reply      PUT  /api/v2/tickets/{id}  {"ticket": {"comment": {"html_body", "public": true}, "metadata"}}
  read-back  GET  /api/v2/tickets/{id}/audits  +  GET /api/v2/tickets/{id}/comments
  customer   GET  /api/v2/users/search  +  GET /api/v2/users/{id}/tickets/requested

Rules this adapter keeps (each one has a test and a mutation):
- Inbound is PUBLIC comments written by END USERS only. Agent replies, internal notes and
  automatic messages never become customer mail.
- A reply goes only to the ticket's requester, and only when the requester IS the approved
  recipient and nobody is CC'd: a public comment is emailed to every CC, which nobody approved.
- Zendesk documents ``Idempotency-Key`` for ticket CREATION and keeps keys for two hours. The
  header is sent on the update too, but never trusted: before every write the ticket's audits are
  read for our own key in the audit metadata, so a retry after a crash (or after two hours) finds
  the first write instead of commenting twice.
- Text that Zendesk would silently change is refused before the write: ``{{placeholders}}`` are
  expanded or stripped, and bodies over 64 kB are cut without an error.
- Read-back proves the comment exists, is public, and its text (whitespace-normalised, because
  Zendesk re-renders whitespace) matches the approved text.
- 429: the ``Retry-After`` HEADER is honoured (Zendesk does not put it in the JSON body). A wait
  longer than a minute ends the run; the next tick tries again.
- Anything not understood (a naive time, an unknown role, a missing field) raises.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any

from sayso.adapters.base import InboundMessage, SentRecord
from sayso.adapters.http import Client, HttpError
from sayso.config import ConfigError, Section

SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_BODY_BYTES = 60_000          # Zendesk cuts comment bodies at 64 kB with no error
MAX_RETRY_AFTER = 60.0           # longer waits end the run instead of blocking a tick
INBOUND_PAGES = 3                # incremental export allows 10 requests a minute
OTHER_TICKETS = 20               # customer's other tickets checked for last_inbound_at
AUDIT_PAGES = 10
END_USER = "end-user"
STAFF_ROLES = ("agent", "admin")
MARKER_KEY, MARKER_SHA, MARKER_NSHA = "sayso_key", "sayso_sha", "sayso_nsha"


class ZendeskError(RuntimeError):
    """Zendesk answered something this adapter does not understand. Nothing is guessed."""


class ZendeskRefused(ZendeskError):
    """A guard refused the write. Nothing was sent."""


class RateLimited(HttpError):
    def __init__(self, wait: float):
        super().__init__(429, f"rate limited; Zendesk asked to wait {wait:.0f}s")
        self.wait = wait


def check_subdomain(value: Any) -> str:
    sub = str(value or "").strip().lower()
    if not SUBDOMAIN.fullmatch(sub):
        raise ConfigError("zendesk: subdomain must be the part before .zendesk.com (letters, digits, dashes)")
    return sub


def base_url(subdomain: str) -> str:
    return f"https://{check_subdomain(subdomain)}.zendesk.com"


def authorization(auth: str, secret: str, email: str = "") -> str:
    """The Authorization header. ``token``: Basic {email}/token:{api_token}. ``oauth``: Bearer."""
    import base64
    if not secret:
        raise ConfigError("zendesk: the token is empty")
    if auth == "oauth":
        return "Bearer " + secret
    if auth != "token":
        raise ConfigError("zendesk: auth must be 'token' or 'oauth'")
    if not re.fullmatch(r"[^@\s/:]+@[^@\s/:]+\.[^@\s/:]+", email or ""):
        raise ConfigError("zendesk: auth = 'token' needs the agent's email address")
    return "Basic " + base64.b64encode(f"{email}/token:{secret}".encode()).decode()


def normalise_text(text: str) -> str:
    """Whitespace-insensitive form used to compare an approved body with what Zendesk stored."""
    return " ".join(html.unescape(str(text or "")).split())


def text_sha(text: str) -> str:
    return hashlib.sha256(normalise_text(text).encode()).hexdigest()


def to_html(body: str) -> str:
    """Plain text -> html_body: escaped, one <p> per paragraph, <br> per line. No Markdown, no
    whitespace collapsing, so the customer reads exactly the approved words."""
    paras = re.split(r"\n\s*\n", body.strip())
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paras)


def iso(value: Any) -> str:
    """Zendesk times are ISO 8601 UTC with Z. Anything without a zone is refused."""
    text = str(value or "")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ZendeskError(f"zendesk: unreadable time {text[:40]!r}") from None
    if dt.tzinfo is None:
        raise ZendeskError("zendesk: a time without a timezone was refused")
    return dt.isoformat()


class ZendeskClient(Client):
    """core Client + Zendesk's Retry-After header."""

    def request(self, method, path, *, json_body=None, form=None, query=None, extra_headers=None):
        url = self.base + path + (("?" + urllib.parse.urlencode(query, doseq=True)) if query else "")
        headers = dict(self.headers, **(extra_headers or {}))
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            status, resp_headers, raw = self.transport(method, url, headers, body)
            if status == 429:
                lower = {str(k).lower(): v for k, v in (resp_headers or {}).items()}
                try:
                    wait = float(lower.get("retry-after", 60))
                except (TypeError, ValueError):
                    wait = 60.0
                if wait > MAX_RETRY_AFTER or attempt >= self.max_retries:
                    raise RateLimited(wait)
                self.sleep(max(wait, 1.0))
                continue
            if status >= 400:
                raise HttpError(status, raw.decode("utf-8", "replace"))
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                raise ZendeskError("zendesk: the answer was not JSON") from None
        raise RateLimited(60.0)


def _ids(rows) -> list[int]:
    out = []
    for r in rows or []:
        v = r.get("id") if isinstance(r, dict) else r
        if isinstance(v, int) and not isinstance(v, bool):
            out.append(v)
    return out


class ZendeskHelpdesk:
    """One Zendesk account. ``support_address`` is the address the customer sees replies from."""

    def __init__(self, subdomain: str, auth_header: str, support_address: str, transport=None, *,
                 sleep=time.sleep, now=time.time, backfill_hours: float = 24.0):
        self.subdomain = check_subdomain(subdomain)
        self.support = str(support_address or "").strip().lower()
        self.http = ZendeskClient(base_url(self.subdomain), {"Authorization": auth_header,
                                                              "Accept": "application/json"},
                                  transport, sleep=sleep)
        self.now = now
        self.backfill = float(backfill_hours)
        self._users: dict[int, dict] = {}

    @classmethod
    def from_section(cls, section: Section, support_address: str, transport=None, **kw):
        o = section.options
        header = authorization(str(o.get("auth", "token")), section.secret("token"), str(o.get("email", "")))
        hours = o.get("backfill_hours", 24)
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0 <= hours <= 24 * 30:
            raise ConfigError("zendesk: backfill_hours must be a number from 0 to 720")
        return cls(str(o.get("subdomain", "")), header, support_address, transport, backfill_hours=hours, **kw)

    def __repr__(self) -> str:
        return f"ZendeskHelpdesk({self.subdomain})"

    def ticket_url(self, ticket: str) -> str:
        return f"https://{self.subdomain}.zendesk.com/agent/tickets/{_ticket_id(ticket)}"

    # ------------------------------------------------------------------ reads
    def check(self) -> dict:
        """Safe connection test: who am I. Reads only. Refuses a token that is not an agent/admin."""
        me = (self.http.request("GET", "/api/v2/users/me") or {}).get("user") or {}
        role = me.get("role")
        if role not in STAFF_ROLES:
            raise ZendeskRefused("zendesk: this token does not belong to an agent or admin")
        return {"email": str(me.get("email") or ""), "role": role, "id": me.get("id"),
                "can_export": role == "admin"}

    def support_addresses(self) -> list[dict]:
        rows = (self.http.request("GET", "/api/v2/recipient_addresses") or {}).get("recipient_addresses")
        if not isinstance(rows, list):
            raise ZendeskError("zendesk: recipient_addresses missing")
        return [{"email": str(r.get("email") or "").lower(), "default": bool(r.get("default"))} for r in rows]

    def _ticket(self, ticket: str) -> dict:
        t = (self.http.request("GET", f"/api/v2/tickets/{_ticket_id(ticket)}") or {}).get("ticket")
        if not isinstance(t, dict) or not isinstance(t.get("requester_id"), int):
            raise ZendeskError("zendesk: ticket has no requester")
        return t

    def _load_users(self, ids) -> None:
        want = sorted({i for i in ids if isinstance(i, int) and i not in self._users})
        for i in range(0, len(want), 100):
            chunk = want[i:i + 100]
            rows = (self.http.request("GET", "/api/v2/users/show_many",
                                      query={"ids": ",".join(map(str, chunk))}) or {}).get("users")
            if not isinstance(rows, list):
                raise ZendeskError("zendesk: users/show_many missing users")
            for u in rows:
                self._remember(u)
        missing = [i for i in want if i not in self._users]
        if missing:
            raise ZendeskError(f"zendesk: {len(missing)} comment author(s) could not be read")

    def _remember(self, u: dict) -> None:
        if isinstance(u, dict) and isinstance(u.get("id"), int):
            role = u.get("role")
            if role not in (END_USER,) + STAFF_ROLES:
                raise ZendeskError(f"zendesk: unknown user role {str(role)[:20]!r}")
            self._users[u["id"]] = {"email": str(u.get("email") or "").strip().lower(), "role": role}

    def _user(self, uid: int) -> dict:
        self._load_users([uid])
        return self._users[uid]

    def _comments(self, ticket: str, newest_first: bool = True, pages: int = 5) -> list[dict]:
        """Public and private comments (authors' roles loaded), newest first by default."""
        out: list[dict] = []
        query: dict = {"page[size]": 100, "sort": "-created_at" if newest_first else "created_at",
                       "include": "users"}
        path = f"/api/v2/tickets/{_ticket_id(ticket)}/comments"
        for _ in range(pages):
            page = self.http.request("GET", path, query=query) or {}
            rows = page.get("comments")
            if not isinstance(rows, list):
                raise ZendeskError("zendesk: comments missing")
            for u in page.get("users") or []:
                self._remember(u)
            out.extend(rows)
            meta = page.get("meta") or {}
            if not meta.get("has_more"):
                return out
            query = dict(query, **{"page[after]": meta.get("after_cursor")})
        raise ZendeskError("zendesk: too many comments to read")

    def _audits(self, ticket: str) -> list[dict]:
        out: list[dict] = []
        query: dict = {"page[size]": 100}
        path = f"/api/v2/tickets/{_ticket_id(ticket)}/audits"
        for _ in range(AUDIT_PAGES):
            page = self.http.request("GET", path, query=query) or {}
            rows = page.get("audits")
            if not isinstance(rows, list):
                raise ZendeskError("zendesk: audits missing")
            out.extend(rows)
            meta = page.get("meta") or {}
            if not meta.get("has_more"):
                return out
            query = {"page[size]": 100, "page[after]": meta.get("after_cursor")}
        raise ZendeskError("zendesk: too many audits to read")

    # ------------------------------------------------------------------ Helpdesk protocol
    def fetch_inbound(self, cursor):
        if cursor is None:
            start = int(self.now() - self.backfill * 3600)
        else:
            if not str(cursor).isdigit():
                raise ZendeskError("zendesk: the stored cursor is not a Zendesk time")
            start = int(cursor)
        start = min(start, int(self.now()) - 61)   # the export refuses the most recent minute
        events: list[dict] = []
        next_start = start
        for _ in range(INBOUND_PAGES):
            page = self.http.request("GET", "/api/v2/incremental/ticket_events",
                                     query={"start_time": next_start, "include": "comment_events"}) or {}
            rows = page.get("ticket_events")
            end_time = page.get("end_time")
            if not isinstance(rows, list) or not isinstance(end_time, int):
                raise ZendeskError("zendesk: ticket_events export missing events or end_time")
            events.extend(rows)
            next_start = end_time
            if page.get("end_of_stream") is not False:
                break
        comments = []
        for ev in events:
            for child in ev.get("child_events") or []:
                if child.get("event_type", child.get("type")) != "Comment" or child.get("public") is not True:
                    continue
                comments.append((ev, child))
        tickets = {ev.get("ticket_id") for ev, _ in comments}
        if any(not isinstance(t, int) for t in tickets):
            raise ZendeskError("zendesk: a comment event has no ticket id")
        self._load_users([c.get("author_id") for _, c in comments])
        subjects = self._subjects(tickets)
        msgs = []
        for ev, c in comments:
            author = self._users.get(c.get("author_id"))
            if author is None:
                raise ZendeskError("zendesk: a comment has no readable author")
            if author["role"] != END_USER or not author["email"]:
                continue                       # our agents' replies are never customer mail
            if not isinstance(c.get("id"), int):
                raise ZendeskError("zendesk: a comment has no id")
            tid = ev["ticket_id"]
            msgs.append(InboundMessage(
                message_id=f"zendesk:{tid}:{c['id']}", ticket=str(tid), sender=author["email"],
                recipients=(subjects[tid]["recipient"],) if subjects[tid]["recipient"] else (),
                subject=subjects[tid]["subject"], body=str(c.get("body") or ""),
                received_at=iso(c.get("created_at") or ev.get("created_at"))))
        msgs.sort(key=lambda m: (m.received_at, m.message_id))
        return msgs, str(next_start)

    def _subjects(self, tickets) -> dict:
        out = {}
        ids = sorted(tickets)
        for i in range(0, len(ids), 100):
            rows = (self.http.request("GET", "/api/v2/tickets/show_many",
                                      query={"ids": ",".join(map(str, ids[i:i + 100]))}) or {}).get("tickets")
            if not isinstance(rows, list):
                raise ZendeskError("zendesk: tickets/show_many missing tickets")
            for t in rows:
                out[t.get("id")] = {"subject": str(t.get("subject") or t.get("raw_subject") or ""),
                                    "recipient": str(t.get("recipient") or "").lower()}
        missing = [t for t in ids if t not in out]
        if missing:
            raise ZendeskError(f"zendesk: {len(missing)} ticket(s) could not be read")
        return out

    def _find_marked(self, ticket: str, key: str, field: str = MARKER_KEY) -> list[dict]:
        return [a for a in self._audits(ticket)
                if str(((a.get("metadata") or {}).get("custom") or {}).get(field) or "") == key]

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        tid = _ticket_id(ticket)
        if not isinstance(body, str) or not body.strip():
            raise ZendeskRefused("zendesk: empty reply refused")
        if "{{" in body:
            raise ZendeskRefused("zendesk: '{{' would be read as a Zendesk placeholder and change the text")
        html_body = to_html(body)
        if len(html_body.encode()) > MAX_BODY_BYTES:
            raise ZendeskRefused("zendesk: reply too long; Zendesk would cut it without an error")
        t = self._ticket(tid)
        if t.get("status") == "closed":
            raise ZendeskRefused("zendesk: the ticket is closed; a reply cannot be added")
        requester = self._user(t["requester_id"])
        if requester["email"] != str(recipient or "").strip().lower():
            raise ZendeskRefused("zendesk: the ticket's requester is not the approved recipient")
        if _ids(t.get("email_cc_ids")) or _ids(t.get("collaborator_ids")):
            raise ZendeskRefused("zendesk: the ticket has CCs; a public reply would also go to them")
        key = str(idempotency_key)
        earlier = self._find_marked(tid, key)
        if earlier:                             # written by an earlier attempt: never comment twice
            return self._comment_of(earlier[0]) or ""
        out = self.http.request("PUT", f"/api/v2/tickets/{tid}", json_body={"ticket": {
            "comment": {"html_body": html_body, "public": True},
            "metadata": {MARKER_KEY: key, MARKER_SHA: hashlib.sha256(body.encode()).hexdigest(),
                         MARKER_NSHA: text_sha(body)}}},
            extra_headers={"Idempotency-Key": hashlib.sha256(key.encode()).hexdigest()})
        return self._comment_of((out or {}).get("audit") or {}) or ""

    @staticmethod
    def _comment_of(audit: dict) -> str:
        for e in audit.get("events") or []:
            if e.get("type") == "Comment" and isinstance(e.get("id"), int):
                return str(e["id"])
        return ""

    def sent_readback(self, ticket, body_sha):
        tid = _ticket_id(ticket)
        audits = self._find_marked(tid, body_sha, MARKER_SHA)
        if not audits:
            return []
        t = self._ticket(tid)
        requester = self._user(t["requester_id"])
        cc = _ids(t.get("email_cc_ids")) or _ids(t.get("collaborator_ids"))
        self._load_users(cc)
        recipients = ",".join([requester["email"]] + [self._users[i]["email"] for i in cc])
        sender = str(t.get("recipient") or "").lower() or self._default_address()
        comments = {c.get("id"): c for c in self._comments(tid)}
        out = []
        for a in audits:
            cid = self._comment_of(a)
            c = comments.get(int(cid)) if cid else None
            nsha = str(((a.get("metadata") or {}).get("custom") or {}).get(MARKER_NSHA) or "")
            stored = str((c or {}).get("plain_body") or (c or {}).get("body") or "")
            ok = bool(c) and c.get("public") is True and text_sha(stored) == nsha
            out.append(SentRecord(ticket=tid, recipient=recipients, sender=sender, subject=str(t.get("subject") or ""),
                                  body_sha=body_sha, message_id=f"zendesk:{tid}:{a.get('id')}:{cid}",
                                  status="Sent" if ok else "Not Sent"))
        return out

    def _default_address(self) -> str:
        rows = self.support_addresses()
        for r in rows:
            if r["default"]:
                return r["email"]
        return self.support

    def last_sent_at(self, ticket):
        return self._last_public_by(ticket, lambda _i, u: u["role"] in STAFF_ROLES)

    def _last_public_by(self, ticket, author_ok) -> str | None:
        for c in self._comments(ticket):
            if c.get("public") is not True:
                continue
            uid = c.get("author_id")
            if not isinstance(uid, int):
                raise ZendeskError("zendesk: a comment has no author")
            if uid not in self._users:
                self._load_users([uid])
            if author_ok(uid, self._users[uid]):
                return iso(c.get("created_at"))
        return None

    def last_inbound_at(self, ticket, customer):
        best = self._last_public_by(ticket, lambda _i, u: u["role"] == END_USER)
        email = str(customer or "").strip().lower()
        if not email:
            return best
        users = (self.http.request("GET", "/api/v2/users/search", query={"query": f"email:{email}"}) or {}).get("users")
        if not isinstance(users, list):
            raise ZendeskError("zendesk: users/search missing users")
        mine = [u for u in users if str(u.get("email") or "").lower() == email and isinstance(u.get("id"), int)]
        for u in mine:
            self._remember(u)
            rows = (self.http.request("GET", f"/api/v2/users/{u['id']}/tickets/requested",
                                      query={"sort_by": "updated_at", "sort_order": "desc", "per_page": 100})
                    or {}).get("tickets")
            if not isinstance(rows, list):
                raise ZendeskError("zendesk: requested tickets missing")
            checked = 0
            for t in rows:
                if str(t.get("id")) == _ticket_id(ticket):
                    continue
                updated = iso(t.get("updated_at"))
                if best and updated <= best:
                    break                       # sorted newest first: nothing older can be newer
                if checked >= OTHER_TICKETS:
                    raise ZendeskError("zendesk: too many recent tickets from this customer to check")
                checked += 1
                got = self._last_public_by(str(t["id"]), lambda i, _u, uid=u["id"]: i == uid)
                if got and (best is None or got > best):
                    best = got
        return best


class ZendeskMailbox:
    """The independent receipt for a Zendesk reply: Zendesk's own email notification.

    Zendesk sends the requester's email itself (a trigger), and records it as a Notification event in
    the SAME audit as our comment. ``sent_copies`` counts those notifications to the requester. This
    is the same system as the helpdesk, which is weaker than an IMAP Sent folder: see docs/ADAPTERS.md.
    """

    def __init__(self, helpdesk: ZendeskHelpdesk):
        self.zd = helpdesk

    @classmethod
    def from_section(cls, section: Section, transport=None, **kw):
        return cls(ZendeskHelpdesk.from_section(section, "", transport, **kw))

    def sent_copies(self, message_id):
        m = re.fullmatch(r"zendesk:([0-9]+):([0-9]+):([0-9]+)", str(message_id or ""))
        if not m:
            raise ZendeskError("zendesk: not a Zendesk reply id")
        tid, audit_id = m.group(1), m.group(2)
        audit = (self.zd.http.request("GET", f"/api/v2/tickets/{tid}/audits/{audit_id}") or {}).get("audit")
        if not isinstance(audit, dict):
            raise ZendeskError("zendesk: audit missing")
        requester = self.zd._ticket(tid)["requester_id"]
        return sum(1 for e in audit.get("events") or []
                   if e.get("type") in ("Notification", "NotificationWithCcs")
                   and requester in _ids(e.get("recipients")))


def _ticket_id(ticket) -> str:
    tid = str(ticket or "").strip()
    if not tid.isdigit():
        raise ZendeskRefused("zendesk: ticket id must be a number")
    return tid


def build(name: str, section: Section, config) -> Any:
    """reference.build() branch. The mailbox receipt reads the SAME Zendesk account as the helpdesk."""
    if name == "helpdesk":
        return ZendeskHelpdesk.from_section(section, config.support_address)
    if name == "mailbox":
        helpdesk = config.section("helpdesk")
        if helpdesk.adapter != "zendesk":
            raise ConfigError("[mailbox] zendesk needs [helpdesk] adapter = \"zendesk\"")
        return ZendeskMailbox.from_section(helpdesk)
    raise ConfigError(f"no zendesk adapter for {name}")
