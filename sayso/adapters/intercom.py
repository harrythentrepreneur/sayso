"""Intercom helpdesk adapter: conversations in, admin replies out, proof read back.

Satisfies ``Helpdesk`` and ``Mailbox`` in ``adapters/base.py`` (see docs/ADAPTERS.md, "Intercom").
Every request carries a pinned ``Intercom-Version`` header, so a version change in the
Intercom Developer Hub cannot change what this code reads.

Mapping:
- ticket       = the Intercom conversation id (digits only; anything else is refused).
- inbound      = the conversation's opening message and every later part written by the
                 customer (author type user/lead/contact), from ``POST /conversations/search``
                 on ``updated_at`` and ``GET /conversations/{id}?display_as=plaintext``.
- reply        = ``POST /conversations/{id}/reply`` with ``type: admin``, ``message_type:
                 comment`` and the configured admin as author. Never a note (a note does
                 not reach the customer), never a bot or Operator reply (Intercom does not
                 email those to the customer by default).
- marker       = the reply ends with ``Ref: sayso-<key>.<body>``. Intercom has NO idempotency
                 key on replies, so the marker is how a retry finds an earlier reply (read
                 before write) and how ``sent_readback`` finds THIS body again.
- receipt      = the conversation part itself, read back by id. Intercom has no separate
                 Sent folder; ``IntercomMailbox`` re-reads the part, which proves it exists
                 and is a customer-visible admin comment, but it is the same system.

Quirks (Intercom docs, API 2.14):
- Search ``>`` on a date means "greater OR EQUAL", so a boundary conversation comes back
  again. Intake drops a message id it has seen, so that is harmless.
- Conversation search has no sort order: every page is read before the cursor moves.
- A retrieved conversation holds at most the 500 newest parts.
- 429 carries ``X-RateLimit-Reset`` (unix seconds) in a header, not a JSON ``retry_after``;
  ``rate_limit_transport`` converts it so ``http.Client`` waits (1-10 s, the limit window).
- A part id is a string in the schema but an integer in one doc example: ``str()`` always.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

from sayso.adapters.base import InboundMessage, SentRecord
from sayso.adapters.http import Client
from sayso.config import ConfigError, Section

VERSION = "2.14"
HOSTS = {"us": "https://api.intercom.io", "eu": "https://api.eu.intercom.io", "au": "https://api.au.intercom.io"}
CUSTOMER_AUTHORS = frozenset({"user", "lead", "contact"})
INBOUND_PARTS = frozenset({"comment", "open", "quick_reply"})
NO_EMAIL_DOMAIN = "contacts.intercom.invalid"   # a contact with no email gets <contact id>@this
PER_PAGE = 150                                   # Intercom's documented maximum
MAX_PAGES = 50
OVERLAP = 600                                    # seconds re-read behind the cursor; intake drops seen ids
OPTIONS = frozenset({"token_env", "admin_id", "region", "backfill_hours"})
_ID = re.compile(r"^[0-9]{1,20}$")
_CONTACT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MARK = re.compile(r"Ref: sayso-([0-9a-f]{10})\.([0-9a-f]{16})")


class IntercomError(RuntimeError):
    """Intercom answered with something this adapter does not understand. Nothing is guessed."""


def rate_limit_transport(inner, now=time.time):
    """Wrap a transport so a 429's ``X-RateLimit-Reset`` header becomes the ``retry_after``
    body field ``http.Client`` already honours. The wait is bounded to 1-10 s."""
    def send(method, url, headers, body):
        status, resp_headers, raw = inner(method, url, headers, body)
        if status == 429:
            reset = {k.lower(): v for k, v in (resp_headers or {}).items()}.get("x-ratelimit-reset")
            try:
                wait = float(reset) - now()
            except (TypeError, ValueError):
                wait = 10.0
            raw = json.dumps({"retry_after": max(1.0, min(wait, 10.0))}).encode()
        return status, resp_headers, raw
    return send


def _iso(ts) -> str:
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        raise IntercomError(f"Intercom time is not unix seconds: {ts!r}")
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def plain(body) -> str:
    """The text of a part. ``display_as=plaintext`` is requested, so tags are a fallback only."""
    text = str(body or "")
    if "<" in text and ">" in text:
        p = _Text()
        p.feed(text)
        text = "".join(p.out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_html(text: str) -> str:
    """Plain reply text as Intercom comment HTML: paragraphs and line breaks kept, nothing else."""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p.strip()).replace("\n", "<br>") + "</p>" for p in paras)


def marker(idempotency_key: str, body_sha: str) -> str:
    return f"Ref: sayso-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]}.{body_sha[:16]}"


class IntercomHelpdesk:
    """One Intercom workspace, replying as one admin."""

    def __init__(self, *, token: str, admin_id: str, support_address: str, region: str = "us",
                 transport=None, clock=time.time, backfill_hours: int = 24, sleep=time.sleep):
        if not token:
            raise ConfigError("intercom: the access token is empty")
        if region not in HOSTS:
            raise ConfigError(f"intercom: region must be one of {sorted(HOSTS)}")
        if not _ID.match(str(admin_id or "")):
            raise ConfigError("intercom: admin_id must be the numeric id of the admin who replies")
        if not isinstance(backfill_hours, int) or isinstance(backfill_hours, bool) or not 0 <= backfill_hours <= 720:
            raise ConfigError("intercom: backfill_hours must be a whole number from 0 to 720")
        self.admin_id = str(admin_id)
        self.support = support_address.strip().lower()
        self.region = region
        self.clock = clock
        self.backfill = backfill_hours * 3600
        from sayso.adapters.http import urllib_transport
        self.http = Client(HOSTS[region], {"Authorization": "Bearer " + token, "Accept": "application/json",
                                           "Intercom-Version": VERSION},
                           rate_limit_transport(transport or urllib_transport, clock), sleep=sleep)
        self._emails: dict[str, str] = {}

    @classmethod
    def from_section(cls, section: Section, support_address: str, transport=None, **kw) -> "IntercomHelpdesk":
        unknown = set(section.options) - OPTIONS
        if unknown:
            raise ConfigError(f"[helpdesk] intercom: unknown keys {sorted(unknown)}")
        return cls(token=section.secret("token"), admin_id=str(section.options.get("admin_id") or ""),
                   support_address=support_address, region=str(section.options.get("region", "us")),
                   backfill_hours=section.options.get("backfill_hours", 24), transport=transport, **kw)

    # ------------------------------------------------------------------ reads
    def _conversation(self, ticket: str) -> dict:
        if not _ID.match(str(ticket)):
            raise IntercomError("ticket is not an Intercom conversation id")
        conv = self.http.request("GET", f"/conversations/{ticket}", query={"display_as": "plaintext"})
        if not isinstance(conv, dict) or conv.get("type") != "conversation" or str(conv.get("id")) != str(ticket):
            raise IntercomError("Intercom did not return this conversation")
        parts = (conv.get("conversation_parts") or {}).get("conversation_parts")
        if not isinstance(parts, list):
            raise IntercomError("conversation has no readable parts list")
        return conv

    @staticmethod
    def _parts(conv: dict) -> list[dict]:
        out = []
        for p in conv["conversation_parts"]["conversation_parts"]:
            if not isinstance(p, dict) or p.get("type") != "conversation_part" or p.get("id") in (None, ""):
                raise IntercomError("conversation part is not understood")
            out.append(p)
        return out

    def _search(self, path: str, query: dict) -> list[dict]:
        """Every page of a search. Raises if the answer is not a list or runs past MAX_PAGES."""
        key = "conversations" if path.startswith("/conversations") else "data"
        rows, after = [], None
        for _ in range(MAX_PAGES):
            body = {"query": query, "pagination": {"per_page": PER_PAGE, **({"starting_after": after} if after else {})}}
            page = self.http.request("POST", path, json_body=body)
            got = page.get(key) if isinstance(page, dict) else None
            if not isinstance(got, list):
                raise IntercomError(f"search answer has no {key} list")
            rows += got
            nxt = ((page.get("pages") or {}).get("next") or {}).get("starting_after")
            if not nxt or not got:
                return rows
            after = str(nxt)
        raise IntercomError(f"search ran past {MAX_PAGES} pages; the cursor was not moved")

    def _customer_of(self, author: dict) -> str:
        email = str(author.get("email") or "").strip().lower()
        cid = str(author.get("id") or "")
        if email:
            return email
        if not _CONTACT_ID.match(cid):
            raise IntercomError("customer part has no email and no usable contact id")
        return f"{cid}@{NO_EMAIL_DOMAIN}".lower()

    def fetch_inbound(self, cursor):
        now = int(self.clock())
        if cursor in (None, ""):
            since = now - self.backfill
        elif str(cursor).isdigit():
            since = int(cursor)
        else:
            raise IntercomError("cursor is not unix seconds")
        convs = self._search("/conversations/search", {"field": "updated_at", "operator": ">", "value": since})
        msgs, newest = [], since
        for row in convs:
            if not isinstance(row, dict) or row.get("type") != "conversation":
                raise IntercomError("search returned something that is not a conversation")
            updated = row.get("updated_at")
            _iso(updated)
            newest = max(newest, updated)
            conv = self._conversation(str(row.get("id")))
            msgs += self._inbound_of(conv, since)
        msgs.sort(key=lambda m: m.received_at)
        return msgs, str(newest)

    def _inbound_of(self, conv: dict, since: int) -> list[InboundMessage]:
        cid = str(conv["id"])
        subject = str(conv.get("title") or (conv.get("source") or {}).get("subject") or "").strip()
        subject = plain(subject) or "Intercom conversation"
        out = []
        since -= OVERLAP                    # a part can be indexed after its own created_at
        src = conv.get("source") or {}
        author = src.get("author") or {}
        if author.get("type") in CUSTOMER_AUTHORS and plain(src.get("body")):
            when = _iso(conv.get("created_at"))
            if conv["created_at"] >= since:
                out.append(InboundMessage(message_id=f"intercom:{cid}:source:{src.get('id')}", ticket=cid,
                                          sender=self._customer_of(author), recipients=(self.support,),
                                          subject=subject, body=plain(src.get("body")), received_at=when))
        for p in self._parts(conv):
            a = p.get("author") or {}
            if a.get("type") not in CUSTOMER_AUTHORS or not plain(p.get("body")) or p.get("redacted"):
                continue
            if p.get("part_type") not in INBOUND_PARTS:
                raise IntercomError(f"customer part of type {p.get('part_type')!r} is not understood")
            created = p.get("created_at")
            when = _iso(created)
            if created < since:
                continue
            out.append(InboundMessage(message_id=f"intercom:{cid}:{p['id']}", ticket=cid, sender=self._customer_of(a),
                                      recipients=(self.support,), subject=subject, body=plain(p.get("body")),
                                      received_at=when))
        return out

    def _contact_email(self, conv: dict) -> str:
        """The ONE customer on this conversation. A group conversation (several contacts) is refused:
        a reply there reaches people nobody approved."""
        contacts = (conv.get("contacts") or {}).get("contacts")
        if not isinstance(contacts, list) or len(contacts) != 1:
            raise IntercomError("conversation does not have exactly one customer")
        cid = str(contacts[0].get("id") or "")
        if not _CONTACT_ID.match(cid):
            raise IntercomError("conversation customer has no usable id")
        if cid not in self._emails:
            contact = self.http.request("GET", f"/contacts/{cid}")
            if not isinstance(contact, dict) or contact.get("type") != "contact" or str(contact.get("id")) != cid:
                raise IntercomError("Intercom did not return this contact")
            email = str(contact.get("email") or "").strip().lower()
            self._emails[cid] = email or f"{cid}@{NO_EMAIL_DOMAIN}".lower()
        return self._emails[cid]

    def _ours(self, part: dict) -> bool:
        """A customer-visible reply written by OUR admin: an admin comment, not redacted."""
        a = part.get("author") or {}
        return (a.get("type") == "admin" and str(a.get("id")) == self.admin_id and part.get("part_type") == "comment"
                and not part.get("redacted"))

    # ------------------------------------------------------------------ writes
    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        conv = self._conversation(str(ticket))
        to = self._contact_email(conv)
        if to != str(recipient or "").strip().lower():
            raise IntercomError("recipient is not the customer on this conversation; nothing sent")
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        mark = marker(idempotency_key, body_sha)
        key_part = mark.split(".")[0]
        for p in self._parts(conv):        # read before write: Intercom has no idempotency key
            if (p.get("author") or {}).get("type") == "admin" and key_part in plain(p.get("body")):
                return f"intercom:{conv['id']}:{p['id']}"
        out = self.http.request("POST", f"/conversations/{conv['id']}/reply", json_body={
            "message_type": "comment", "type": "admin", "admin_id": self.admin_id,
            "body": to_html(body) + "<p>" + mark + "</p>"})
        if not isinstance(out, dict) or out.get("type") != "conversation":
            raise IntercomError("reply answer is not a conversation")
        for p in reversed(self._parts(out)):
            if mark in plain(p.get("body")):
                return f"intercom:{conv['id']}:{p['id']}"
        return ""                           # the read-back decides; the write call is never proof

    # ------------------------------------------------------------------ proof
    def sent_readback(self, ticket, body_sha):
        conv = self._conversation(str(ticket))
        to = ""
        try:
            to = self._contact_email(conv)
        except IntercomError:
            to = ""                         # unknown recipient: the sender's recipient match then fails
        out = []
        for p in self._parts(conv):
            m = _MARK.search(plain(p.get("body")))
            if not m or m.group(2) != body_sha[:16]:
                continue
            ours = self._ours(p)
            a = p.get("author") or {}
            out.append(SentRecord(ticket=str(conv["id"]), recipient=to,
                                  sender=self.support if ours else str(a.get("email") or a.get("id") or ""),
                                  subject="", body_sha=body_sha, message_id=f"intercom:{conv['id']}:{p['id']}",
                                  status="Sent" if ours else "Not Sent"))
        return out

    def part(self, message_id: str) -> dict | None:
        m = re.fullmatch(r"intercom:([0-9]{1,20}):([A-Za-z0-9_-]{1,40})", str(message_id or "").strip())
        if not m:
            raise IntercomError("not an Intercom part id")
        found = [p for p in self._parts(self._conversation(m.group(1))) if str(p.get("id")) == m.group(2)]
        return found[0] if len(found) == 1 else None

    def last_sent_at(self, ticket):
        conv = self._conversation(str(ticket))
        times = [p.get("created_at") for p in self._parts(conv)
                 if (p.get("author") or {}).get("type") == "admin" and p.get("part_type") == "comment"
                 and not p.get("redacted")]
        for t in times:
            _iso(t)                         # a time we cannot read is an error, never skipped
        return _iso(max(times)) if times else None

    def _newest_from_customer(self, conv: dict) -> int | None:
        times = [p.get("created_at") for p in self._parts(conv)
                 if (p.get("author") or {}).get("type") in CUSTOMER_AUTHORS]
        if ((conv.get("source") or {}).get("author") or {}).get("type") in CUSTOMER_AUTHORS:
            times.append(conv.get("created_at"))
        for t in times:
            _iso(t)
        return max(times) if times else None

    def last_inbound_at(self, ticket, customer):
        """Newest customer message on this conversation OR on ANY conversation of this customer."""
        best = self._newest_from_customer(self._conversation(str(ticket)))
        who = str(customer or "").strip().lower()
        if who.endswith("@" + NO_EMAIL_DOMAIN):
            ids = [who.split("@")[0]]
        elif who:
            ids = [str(c.get("id")) for c in self._search("/contacts/search",
                                                          {"field": "email", "operator": "=", "value": who})
                   if isinstance(c, dict) and c.get("id")]
        else:
            ids = []
        if ids:
            for row in self._search("/conversations/search", {"field": "contact_ids", "operator": "IN", "value": ids}):
                if not isinstance(row, dict) or str(row.get("id")) == str(ticket):
                    continue
                t = self._newest_from_customer(self._conversation(str(row.get("id"))))
                if t is not None and (best is None or t > best):
                    best = t
        return _iso(best) if best is not None else None


class IntercomMailbox:
    """The receipt check. Intercom has no Sent folder, so this re-reads the reply part by id and
    counts it only when it is a customer-visible comment by our admin (see module notes)."""

    def __init__(self, helpdesk: IntercomHelpdesk):
        self.helpdesk = helpdesk

    def sent_copies(self, message_id):
        p = self.helpdesk.part(message_id)
        return 1 if p is not None and self.helpdesk._ours(p) else 0
