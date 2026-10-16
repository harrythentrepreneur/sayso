"""Crisp (REST API v1) as the helpdesk. OFF unless a product sets ``adapter = "crisp"`` and live mode.

One Crisp conversation (``session_id``) is one ticket. The customer is the
conversation's ``meta.email``. Sources and verbatim quotes for every call are in
the lane research (docs/ADAPTERS.md, "Crisp").

Rules this adapter keeps (docs/ADAPTERS.md, "Rules for every adapter"):
- Auth is a token keypair: ``Authorization: Basic base64(identifier:key)`` plus
  ``X-Crisp-Tier: plugin`` (or ``website`` for a website token). Both halves are
  read only through ``section.secret()``.
- Crisp has no idempotency header. The send carries a numeric message
  ``fingerprint`` derived from the idempotency key, and the adapter READS BEFORE
  IT WRITES: if a message with that fingerprint already exists, it never posts
  again. Nothing is added to the customer's text.
- A reply goes only to a conversation whose own email is the approved recipient.
- Read-backs read Crisp's own message list. A reply counts as Sent only when
  Crisp marks it ``stamped``.
- Anything not understood (an unknown message type or sender, an error
  envelope, a timestamp that is not epoch milliseconds, a list longer than the
  page cap) raises. The loop turns that into UNVERIFIED plus an alert.
- 429 and 420 ("Enhance Your Calm") are retried a bounded number of times; 423
  (daily quota used up) raises at once.

The mailbox half (``CrispMailbox``) reads the same Crisp message by fingerprint.
It is NOT independent of the helpdesk: Crisp is both the system of record and
the only receipt. docs/ADAPTERS.md says what that weakens.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sayso.adapters.base import InboundMessage, SentRecord
from sayso.adapters.http import Client, HttpError
from sayso.config import Config, ConfigError, Section

API = "https://api.crisp.chat/v1"
TIERS = ("plugin", "website")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
WEBSITE_ID = re.compile(rf"^{_UUID}$")
SESSION_ID = re.compile(rf"^session_{_UUID}$")
_EMAIL = re.compile(r"^[^@\s/?#]+@[^@\s/?#]+\.[^@\s/?#]+$")
# Epoch milliseconds between 2001-09-09 and 2100-01-01. Seconds or a string are refused.
_MS_MIN, _MS_MAX = 1_000_000_000_000, 4_102_444_800_000
# Message types that carry what the customer said, and widget/system types we skip on purpose.
CONTENT_TYPES = ("text", "file", "animation", "audio")
SKIPPED_TYPES = ("note", "picker", "field", "carousel", "event")
SENDERS = ("user", "operator")
PER_PAGE = 50
MAX_PAGES = 10
READBACK_DAYS = 90  # how far back sent_readback looks for copies of one reply


class CrispError(RuntimeError):
    """Crisp answered with something this adapter does not understand. Never guessed around."""


class CrispRefused(CrispError):
    """A guard refused the action before any write."""


def crisp_fingerprint(idempotency_key: str) -> int:
    """A stable message fingerprint for one idempotency key (15 digits at most, below 2**53)."""
    n = int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:13], 16)
    return n or 1


def ms_to_iso(value: Any) -> str:
    """Crisp message/conversation time (epoch ms) to ISO 8601 UTC. Anything else raises."""
    if isinstance(value, bool) or not isinstance(value, int) or not _MS_MIN <= value <= _MS_MAX:
        raise CrispError("Crisp timestamp is not epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        raise CrispError("naive time refused")
    return int(dt.timestamp() * 1000)


def _norm(address: Any) -> str:
    return str(address or "").strip().lower()


def _auth_headers(identifier: str, key: str, tier: str) -> dict[str, str]:
    token = base64.b64encode(f"{identifier}:{key}".encode()).decode()
    return {"Authorization": "Basic " + token, "X-Crisp-Tier": tier, "Accept": "application/json"}


def _calm_transport(inner):
    """Crisp may answer 420 Enhance Your Calm for a rate limit. The shared client retries 429 only,
    so 420 is reported to it as 429 (same bounded retry). Nothing else is changed."""
    from sayso.adapters.http import urllib_transport
    inner = inner or urllib_transport

    def send(method, url, headers, body):
        status, resp_headers, raw = inner(method, url, headers, body)
        return (429 if status == 420 else status), resp_headers, raw
    return send


class _CrispApi:
    """The HTTP half shared by the helpdesk and the mailbox. Holds no customer data."""

    def __init__(self, section: Section, transport=None, sleep=time.sleep):
        self.website = str(section.options.get("website_id") or "").strip().lower()
        if not WEBSITE_ID.match(self.website):
            raise ConfigError("[crisp] website_id must be the workspace's website id (a UUID)")
        tier = str(section.options.get("tier") or "plugin")
        if tier not in TIERS:
            raise ConfigError(f"[crisp] tier must be one of {TIERS}")
        headers = _auth_headers(section.secret("identifier"), section.secret("key"), tier)
        self.http = Client(API, headers, _calm_transport(transport), sleep=sleep)

    def call(self, method: str, path: str, **kw) -> Any:
        """One API call. Returns the envelope's ``data``. An error envelope raises."""
        out = self.http.request(method, path, **kw)
        if not isinstance(out, dict) or out.get("error") is not False or "data" not in out:
            raise CrispError(f"Crisp answered {method} with an error envelope")
        return out["data"]

    @staticmethod
    def reason(exc: HttpError) -> str:
        """Crisp's ``reason`` word from an error body, or ''."""
        text = str(exc)
        try:
            return str(json.loads(text.split(": ", 1)[1]).get("reason") or "")
        except (IndexError, ValueError, AttributeError):
            return ""

    def site(self, rest: str = "") -> str:
        return f"/website/{self.website}{rest}"

    @staticmethod
    def session(ticket: str) -> str:
        if not SESSION_ID.match(str(ticket)):
            raise CrispRefused("not a Crisp conversation id")
        return str(ticket)

    def website_info(self) -> dict:
        """The safe connection test: ``GET /website/{id}``. Reads only."""
        data = self.call("GET", self.site())
        if not isinstance(data, dict) or str(data.get("website_id", "")).lower() != self.website:
            raise CrispError("Crisp returned a different website")
        return data

    def message(self, session: str, fingerprint: int) -> dict | None:
        """One message by fingerprint, or None when Crisp says it does not exist."""
        try:
            data = self.call("GET", self.site(f"/conversation/{self.session(session)}/message/{int(fingerprint)}"))
        except HttpError as exc:
            if exc.status == 404 and self.reason(exc) == "message_not_found":
                return None
            raise
        if not isinstance(data, dict):
            raise CrispError("message read returned no object")
        return data

    def messages(self, session: str, before: int | None = None) -> list[dict]:
        query = {"timestamp_before": str(before)} if before is not None else None
        rows = self.call("GET", self.site(f"/conversation/{self.session(session)}/messages"), query=query)
        if not isinstance(rows, list):
            raise CrispError("message list is not a list")
        for m in rows:
            if not isinstance(m, dict) or m.get("from") not in SENDERS \
                    or m.get("type") not in CONTENT_TYPES + SKIPPED_TYPES:
                raise CrispError("Crisp message with an unknown sender or type")
            ms_to_iso(m.get("timestamp"))
            if str(m.get("session_id", session)) != session:
                raise CrispError("Crisp returned a message from another conversation")
        return sorted(rows, key=lambda m: m["timestamp"])

    def messages_since(self, session: str, since_ms: int) -> list[dict]:
        """Every message at or after ``since_ms``, oldest first (pages back with timestamp_before)."""
        out: dict[int, dict] = {}
        before = None
        for _ in range(MAX_PAGES):
            batch = self.messages(session, before)
            for m in batch:
                if m["timestamp"] >= since_ms:
                    out[int(m.get("fingerprint") or 0) or id(m)] = m
            if not batch or batch[0]["timestamp"] < since_ms or (before is not None and batch[0]["timestamp"] >= before):
                return sorted(out.values(), key=lambda m: m["timestamp"])
            before = batch[0]["timestamp"]
        raise CrispError("conversation has more messages than the page cap; raise it or read by hand")

    def newest(self, session: str, sender: str, kinds=("text", "file", "animation", "audio")) -> int | None:
        """Newest timestamp of a ``sender`` message in one conversation, paging back if needed."""
        before = None
        for _ in range(MAX_PAGES):
            batch = self.messages(session, before)
            hits = [m["timestamp"] for m in batch if m["from"] == sender and m["type"] in kinds]
            if hits:
                return max(hits)
            if not batch or (before is not None and batch[0]["timestamp"] >= before):
                return None
            before = batch[0]["timestamp"]
        raise CrispError("no answer within the page cap")

    def meta(self, session: str) -> dict:
        data = self.call("GET", self.site(f"/conversation/{self.session(session)}/meta"))
        if not isinstance(data, dict):
            raise CrispError("conversation meta is not an object")
        return data


def _body_of(m: dict) -> str:
    if m["type"] == "text":
        if not isinstance(m.get("content"), str):
            raise CrispError("text message without text")
        return m["content"]
    content = m.get("content") if isinstance(m.get("content"), dict) else {}
    return f"[{m['type']}: {content.get('name') or content.get('url') or 'no name'}]"


class CrispHelpdesk:
    """Helpdesk over Crisp conversations. Options: ``website_id``, ``tier`` (plugin|website),
    ``identifier_env``, ``key_env``, ``start_days`` (first-run look-back, default 7)."""

    def __init__(self, section: Section, support_address: str, transport=None, sleep=time.sleep, now=time.time):
        self.api = _CrispApi(section, transport, sleep)
        self.support = _norm(support_address)
        try:
            self.start_days = int(section.options.get("start_days", 7))
        except (TypeError, ValueError):
            raise ConfigError("[crisp] start_days must be a whole number") from None
        if not 1 <= self.start_days <= 90:
            raise ConfigError("[crisp] start_days must be 1..90")
        self.now = now
        self.skipped_no_email = 0

    # ------------------------------------------------------------------ inbound
    def _changed_conversations(self, since_ms: int) -> list[dict]:
        since = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = []
        for page in range(1, MAX_PAGES + 1):
            rows = self.api.call("GET", self.api.site(f"/conversations/{page}"),
                                 query={"per_page": str(PER_PAGE), "filter_date_start": since,
                                        "order_date_updated": "1"})
            if not isinstance(rows, list):
                raise CrispError("conversation list is not a list")
            out.extend(rows)
            if len(rows) < PER_PAGE:
                return out
        raise CrispError("more changed conversations than the page cap since the last run")

    def fetch_inbound(self, cursor):
        """Customer (``from: user``) messages at or after the cursor (epoch ms), oldest first.

        ``>=`` on purpose: two messages can share a millisecond. Intake drops message ids it has
        already recorded, so a re-read is harmless; a skipped message would not be."""
        if cursor is None:
            since = int((self.now() - self.start_days * 86400) * 1000)
        else:
            if not str(cursor).isdigit():
                raise CrispError("Crisp cursor is not epoch milliseconds")
            since = int(cursor)
        msgs: list[InboundMessage] = []
        newest = since
        for conv in self._changed_conversations(since):
            conv = conv if isinstance(conv, dict) else {}
            session = str(conv.get("session_id") or "")
            self.api.session(session)
            meta = conv.get("meta") if isinstance(conv.get("meta"), dict) else {}
            email = _norm(meta.get("email"))
            if not _EMAIL.match(email):
                self.skipped_no_email += 1  # no address: nothing can be approved for it
                continue
            for m in self.api.messages_since(session, since):
                newest = max(newest, m["timestamp"])
                if m["from"] != "user" or m["type"] not in CONTENT_TYPES:
                    continue
                fp = m.get("fingerprint")
                if isinstance(fp, bool) or not isinstance(fp, int):
                    raise CrispError("Crisp message without a numeric fingerprint")
                msgs.append(InboundMessage(
                    message_id=f"crisp:{session}:{fp}", ticket=session, sender=email,
                    recipients=(self.support,), subject=str(meta.get("subject") or ""),
                    body=_body_of(m), received_at=ms_to_iso(m["timestamp"])))
        msgs.sort(key=lambda x: x.received_at)
        return msgs, str(newest)

    # ------------------------------------------------------------------ send
    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        session = self.api.session(ticket)
        if not isinstance(body, str) or not body.strip():
            raise CrispRefused("empty reply refused")
        email = _norm(self.api.meta(session).get("email"))
        if not email or email != _norm(recipient):
            raise CrispRefused("the conversation's email is not the approved recipient")
        fp = crisp_fingerprint(idempotency_key)
        existing = self.api.message(session, fp)
        if existing is not None:
            # Read before write: an earlier attempt already landed. Never post twice.
            if existing.get("from") != "operator" or existing.get("content") != body:
                raise CrispError("a different message already holds this fingerprint")
            return f"crisp:{session}:{fp}"
        data = self.api.call("POST", self.api.site(f"/conversation/{session}/message"),
                             json_body={"type": "text", "from": "operator", "origin": "chat",
                                        "content": body, "fingerprint": fp})
        if not isinstance(data, dict) or data.get("fingerprint") != fp:
            raise CrispError("Crisp did not confirm the message fingerprint we sent")
        return f"crisp:{session}:{fp}"

    def sent_readback(self, ticket, body_sha):
        session = self.api.session(ticket)
        email = _norm(self.api.meta(session).get("email"))
        out = []
        since = int((self.now() - READBACK_DAYS * 86400) * 1000)
        for m in self.api.messages_since(session, since):
            if m["from"] != "operator" or m["type"] != "text" or not isinstance(m.get("content"), str):
                continue
            if hashlib.sha256(m["content"].encode()).hexdigest() != body_sha:
                continue
            # Crisp has no From address for an operator message; the sender is our support address by
            # construction (the token's workspace). The recipient is read from the conversation itself.
            out.append(SentRecord(ticket=session, recipient=email, sender=self.support, subject="",
                                  body_sha=body_sha, message_id=f"crisp:{session}:{m.get('fingerprint')}",
                                  status="Sent" if m.get("stamped") is True else "Not Sent"))
        return out

    # ------------------------------------------------------------------ times
    def last_sent_at(self, ticket):
        ms = self.api.newest(self.api.session(ticket), "operator", kinds=("text", "file", "animation", "audio"))
        return ms_to_iso(ms) if ms is not None else None

    def _people_sessions(self, email: str) -> list[str]:
        """Every conversation of this person (Crisp accepts the email in place of people_id)."""
        out: list[str] = []
        who = urllib.parse.quote(email, safe="@")
        for page in range(1, MAX_PAGES + 1):
            try:
                rows = self.api.call("GET", self.api.site(f"/people/conversations/{who}/list/{page}"))
            except HttpError as exc:
                if exc.status == 404 and self.api.reason(exc) == "people_not_found":
                    return out
                raise
            if not isinstance(rows, list) or not all(isinstance(r, str) for r in rows):
                raise CrispError("people conversation list is not a list of ids")
            if not rows:
                return out
            out.extend(rows)
        raise CrispError("person has more conversations than the page cap")

    def last_inbound_at(self, ticket, customer):
        session = self.api.session(ticket)
        times = [self.api.newest(session, "user")]
        email = _norm(customer)
        if _EMAIL.match(email):
            for other in self._people_sessions(email):
                if other != session:
                    times.append(self.api.newest(self.api.session(other), "user"))
        best = max((t for t in times if t is not None), default=None)
        return ms_to_iso(best) if best is not None else None


class CrispMailbox:
    """The send receipt read from Crisp itself (same system as the helpdesk; see module doc)."""

    def __init__(self, section: Section, transport=None, sleep=time.sleep):
        self.api = _CrispApi(section, transport, sleep)

    def sent_copies(self, message_id):
        m = re.fullmatch(r"crisp:(session_[0-9a-f-]{36}):([1-9][0-9]{0,15})", str(message_id or ""))
        if not m:
            raise CrispError("not a Crisp message id")
        got = self.api.message(m.group(1), int(m.group(2)))
        if got is None:
            return 0
        if got.get("from") != "operator":
            raise CrispError("the message with this fingerprint is not ours")
        return 1


def build(name: str, section: Section, config: Config):
    if name == "helpdesk":
        return CrispHelpdesk(section, config.support_address)
    if name == "mailbox":
        return CrispMailbox(section)
    raise ConfigError(f"no crisp adapter for {name}")
