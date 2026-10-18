"""Help Scout (Inbox API 2.0) as the helpdesk. OFF unless a product sets mode = "live".

Settings (TOML)::

    [helpdesk]
    adapter = "helpscout"
    mailbox_id = 12345                      # the Help Scout inbox Sayso reads and answers
    client_id_env = "ACME_HS_CLIENT_ID"     # an app from Your Profile > My Apps
    client_secret_env = "ACME_HS_CLIENT_SECRET"
    # start_hours = 24                      # first run reads this far back, never the whole history

    [mailbox]
    adapter = "helpscout"                   # same keys as [helpdesk]

Vendor facts this adapter is built on (cited in docs/ADAPTERS.md):
- OAuth2 Client Credentials; a token lives 2 days and expiry shows as HTTP 401.
- ``POST /v2/conversations/{id}/reply`` has NO idempotency key. Duplicate
  prevention is ours: every reply carries a hidden marker (an HTML comment with
  the vote key's hash and the body's sha256), and the conversation's threads are
  read BEFORE every post. A reply that is already there is never posted again.
- A reply only counts as sent when Help Scout shows the thread as ``published``,
  not scheduled, to exactly the one customer. ``draft``, ``review``/
  ``underreview``, ``hidden`` and ``bounced`` are not sent.
- 429 carries ``X-RateLimit-Retry-After`` (seconds). Writes cost 2 requests.
- All times are UTC ``YYYY-MM-DDThh:mm:ssZ``. A naive time is refused.

The mailbox receipt (``HelpScoutMailbox``) reads the SAME system as the
helpdesk: Help Scout sends the email itself and has no separate Sent folder we
can read. So the second receipt only proves the reply thread exists and is
published; it is not independent proof that the email left Help Scout. The
helpdesk documentation says a bounced thread is marked ``bounced``, which the
read-back refuses.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from sayso.adapters.base import InboundMessage, SentRecord
from sayso.adapters.http import Client
from sayso.config import Config, ConfigError, Section

API = "https://api.helpscout.net"
WEB = "https://secure.helpscout.net/conversation/"
MAX_WAIT = 60.0          # never sleep longer than this for one 429
MAX_PAGES = 100          # 100 pages x 25 conversations; more is refused, never silently cut
OVERLAP = timedelta(minutes=5)   # re-read a little before the cursor; intake dedupes by message id
OTHER_CONVERSATIONS = 5  # newest other conversations of the same customer read for last_inbound_at
_MARK = re.compile(r"<!--\s*sayso:([0-9a-f]{16}):([0-9a-f]{64})\s*-->")
_EMAIL = re.compile(r"^[^@\s\"()<>,;:\\]+@[^@\s\"()<>,;:\\]+\.[^@\s\"()<>,;:\\]+$")
NOT_SENT_STATES = ("draft", "review", "underreview", "hidden", "bounced")


class HelpScoutError(RuntimeError):
    """Help Scout answered with something this adapter does not accept. Never holds a secret."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HelpScoutRateLimited(HelpScoutError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None   # a redirect would carry the bearer token to wherever Location points


def no_redirect_transport(method, url, headers, body):
    """Default transport: TLS verified, redirects NOT followed (301 = merged conversation, refused)."""
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _header(headers: dict, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if str(k).lower() == name.lower():
            return str(v)
    return None


def utc_iso(value) -> str:
    """Help Scout time -> ISO 8601 with offset. Naive or unreadable time raises."""
    if not isinstance(value, str) or not value:
        raise HelpScoutError("Help Scout time is missing")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HelpScoutError("Help Scout time is not ISO 8601") from None
    if dt.tzinfo is None:
        raise HelpScoutError("Help Scout time has no timezone")
    return dt.astimezone(timezone.utc).isoformat()


def _hs_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_tag(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]


def marker(idempotency_key: str, body_sha: str) -> str:
    """Hidden in the reply's HTML so the duplicate check and the read-back can find it."""
    return f"<!-- sayso:{key_tag(idempotency_key)}:{body_sha} -->"


def to_html(body: str) -> str:
    """Plain reply text -> the HTML Help Scout stores. ``plain_text`` reverses it exactly."""
    return "<br>".join(html.escape(line, quote=False) for line in body.split("\n"))


def plain_text(value: str) -> str:
    text = _MARK.sub("", str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip("\n")


def web_url(conversation_id) -> str:
    """Link that opens the conversation in Help Scout (the agent must be signed in)."""
    cid = str(conversation_id)
    if not cid.isdigit():
        raise ValueError("Help Scout conversation ids are numbers")
    return WEB + cid


class HelpScoutApi:
    """OAuth2 Client Credentials + JSON calls. One instance per Help Scout account.

    ``token_store`` (optional) is an object with ``get() -> (token, expires_at) | None`` and
    ``put(token, expires_at)``; Cloud keeps the token in its vault. Without it the token lives in
    memory only and is never written anywhere.
    """

    def __init__(self, client_id: str, client_secret: str, transport=None, *, sleep=time.sleep,
                 now=lambda: datetime.now(timezone.utc), token_store=None, max_retries: int = 3):
        if not client_id or not client_secret:
            raise ConfigError("helpscout: client id and secret are required")
        self._id, self._secret = client_id, client_secret
        self.http = Client(API, {"Accept": "application/json"}, transport or no_redirect_transport,
                           sleep=sleep, max_retries=max_retries)
        self.sleep, self.now, self.store, self.max_retries = sleep, now, token_store, max_retries
        self._token: tuple[str, float] | None = None
        self.writes = 0   # POSTs actually sent (tests read it)

    # ------------------------------------------------------------------ token
    def _clean(self, text: str) -> str:
        out = str(text)
        for s in (self._secret, self._token[0] if self._token else None):
            if s:
                out = out.replace(s, "[REDACTED]")
        return out[:300]

    def token(self) -> str:
        now = self.now().timestamp()
        if self._token is None and self.store is not None:
            got = self.store.get()
            if got and got[0]:
                self._token = (str(got[0]), float(got[1]))
        if self._token and self._token[1] - 300 > now:
            return self._token[0]
        return self._fetch_token()

    def _fetch_token(self) -> str:
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._id,
                                       "client_secret": self._secret}).encode()
        status, _, raw = self._raw("POST", "/v2/oauth2/token",
                                   {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200:
            raise HelpScoutError(f"Help Scout refused the app credentials (HTTP {status})", status)
        try:
            data = json.loads(raw)
            token, life = str(data["access_token"]), float(data.get("expires_in") or 0)
        except (ValueError, KeyError, TypeError):
            raise HelpScoutError("Help Scout token answer was not understood") from None
        if not token or life <= 0 or str(data.get("token_type", "bearer")).lower() != "bearer":
            raise HelpScoutError("Help Scout token answer was not understood")
        self._token = (token, self.now().timestamp() + life)
        if self.store is not None:
            self.store.put(*self._token)
        return token

    def forget_token(self) -> None:
        self._token = None
        if self.store is not None:
            self.store.put("", 0.0)

    # ------------------------------------------------------------------ calls
    def _raw(self, method, path, headers, body):
        """One exchange with 429 handling. 429 means nothing was done, so a retry is safe even for POST."""
        url = API + path
        for attempt in range(self.max_retries + 1):
            status, resp_headers, raw = self.http.transport(method, url, dict(self.http.headers, **headers), body)
            if status != 429:
                return status, resp_headers or {}, raw or b""
            if attempt == self.max_retries:
                break
            try:
                wait = float(_header(resp_headers, "X-RateLimit-Retry-After") or 1)
            except ValueError:
                wait = 1.0
            self.sleep(max(0.0, min(wait, MAX_WAIT)))
        raise HelpScoutRateLimited("Help Scout rate limit: gave up after waiting", 429)

    def call(self, method: str, path: str, *, query: dict | None = None, json_body=None):
        """(status, headers, parsed JSON or None). Raises on anything but 2xx."""
        if query:
            path = path + "?" + urllib.parse.urlencode(query)
        body = json.dumps(json_body).encode() if json_body is not None else None
        status, resp_headers, raw = 0, {}, b""
        for again in (True, False):
            headers = {"Authorization": "Bearer " + self.token()}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if method != "GET":
                self.writes += 1
            status, resp_headers, raw = self._raw(method, path, headers, body)
            if status == 401 and again:
                self.forget_token()   # expired: Help Scout signals it only with 401
                continue
            break
        if status in (301, 302, 307, 308):
            raise HelpScoutError("Help Scout moved this conversation (merged); refused", status)
        if status >= 300:
            raise HelpScoutError(f"Help Scout {method} {path.split('?')[0]} failed (HTTP {status}): "
                                 + self._clean(raw.decode("utf-8", "replace")), status)
        try:
            return status, resp_headers, (json.loads(raw) if raw.strip() else None)
        except ValueError:
            raise HelpScoutError("Help Scout answer was not JSON") from None

    def get(self, path: str, **query):
        return self.call("GET", path, query=query or None)[2]

    def pages(self, path: str, key: str, **query) -> list[dict]:
        """Every item of a paginated list. ``page`` numbers are requested 1..totalPages."""
        out, page = [], 1
        while True:
            data = self.get(path, **query, page=page)
            if not isinstance(data, dict):
                raise HelpScoutError("Help Scout list answer was not understood")
            items = (data.get("_embedded") or {}).get(key, [])
            if not isinstance(items, list):
                raise HelpScoutError("Help Scout list answer was not understood")
            out.extend(items)
            total = (data.get("page") or {}).get("totalPages")
            if not isinstance(total, int) or page >= total or not items:
                return out
            page += 1
            if page > MAX_PAGES:
                raise HelpScoutError(f"more than {MAX_PAGES} pages changed at once; refused, not cut short")


def _options(section: Section) -> tuple[int, float]:
    raw = section.options.get("mailbox_id")
    if isinstance(raw, bool) or not (str(raw or "").isascii() and str(raw or "").isdigit()):   # "²".isdigit() is True
        raise ConfigError("[helpdesk] helpscout needs mailbox_id = <the inbox number>")
    hours = section.options.get("start_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 30:
        raise ConfigError("[helpdesk] helpscout start_hours must be 1..720")
    return int(raw), float(hours)


def api_from_section(section: Section, transport=None, **kw) -> HelpScoutApi:
    return HelpScoutApi(section.secret("client_id"), section.secret("client_secret"), transport, **kw)


class HelpScoutHelpdesk:
    """One Help Scout inbox as the helpdesk. Ticket = Help Scout conversation id."""

    def __init__(self, section_or_api, support_address: str, mailbox_id: int | None = None,
                 start_hours: float = 24, transport=None, **kw):
        if isinstance(section_or_api, HelpScoutApi):
            if mailbox_id is None:
                raise ConfigError("helpscout: mailbox_id is required")
            self.api, self.mailbox_id, self.start_hours = section_or_api, int(mailbox_id), float(start_hours)
        else:
            self.mailbox_id, self.start_hours = _options(section_or_api)
            self.api = api_from_section(section_or_api, transport, **kw)
        self.support = str(support_address).strip().lower()
        self._inbox_email: str | None = None

    # ------------------------------------------------------------------ helpers
    def inbox_email(self) -> str:
        if self._inbox_email is None:
            box = self.api.get(f"/v2/mailboxes/{self.mailbox_id}")
            email = str((box or {}).get("email") or "").strip().lower()
            if not _EMAIL.match(email):
                raise HelpScoutError("Help Scout inbox has no readable email address")
            self._inbox_email = email
        return self._inbox_email

    def conversation(self, ticket: str) -> dict:
        cid = str(ticket)
        if not cid.isdigit():
            raise HelpScoutError("not a Help Scout conversation id")
        conv = self.api.get(f"/v2/conversations/{cid}")
        if not isinstance(conv, dict) or str(conv.get("id")) != cid:
            raise HelpScoutError("Help Scout conversation answer was not understood")
        if conv.get("mailboxId") != self.mailbox_id:
            raise HelpScoutError("conversation is not in the connected Help Scout inbox; refused")
        return conv

    def threads(self, ticket: str) -> list[dict]:
        cid = str(ticket)
        if not cid.isdigit():
            raise HelpScoutError("not a Help Scout conversation id")
        rows = self.api.pages(f"/v2/conversations/{cid}/threads", "threads")
        for t in rows:
            if not isinstance(t, dict) or not isinstance(t.get("id"), int) or not t.get("type"):
                raise HelpScoutError("Help Scout thread was not understood")
        return rows

    @staticmethod
    def _is_customer(t: dict) -> bool:
        return t.get("type") == "customer" and t.get("state") == "published"

    @staticmethod
    def _is_reply(t: dict) -> bool:
        return t.get("type") == "message" and (t.get("createdBy") or {}).get("type") == "user"

    @staticmethod
    def _email_of(t: dict) -> str:
        for who in (t.get("customer"), t.get("createdBy")):
            email = str((who or {}).get("email") or "").strip().lower()
            if email:
                return email
        return ""

    # ------------------------------------------------------------------ Helpdesk protocol
    def fetch_inbound(self, cursor):
        """Customer threads created after ``cursor`` in this inbox, oldest first.

        Conversations are listed by modifiedSince (a new customer thread modifies its conversation),
        then each one's threads are read. Only published ``customer`` threads count: notes, agent
        replies, line items, chats and phone threads are never customer mail here."""
        now = self.api.now()
        since = datetime.fromisoformat(utc_iso(cursor)) if cursor else now - timedelta(hours=self.start_hours)
        floor = since - OVERLAP if cursor else since
        convs = self.api.pages("/v2/conversations", "conversations", mailbox=self.mailbox_id, status="all",
                               modifiedSince=_hs_time(floor), sortField="modifiedAt", sortOrder="asc")
        msgs, newest = [], since
        for conv in convs:
            if not isinstance(conv, dict) or not isinstance(conv.get("id"), int):
                raise HelpScoutError("Help Scout conversation was not understood")
            if conv.get("mailboxId") not in (None, self.mailbox_id) or conv.get("type") not in (None, "email"):
                continue
            for t in self.threads(str(conv["id"])):
                if not self._is_customer(t):
                    continue
                at = datetime.fromisoformat(utc_iso(t.get("createdAt")))
                if at <= floor:
                    continue
                sender = self._email_of(t)
                if not _EMAIL.match(sender):
                    raise HelpScoutError("customer thread has no readable email address")
                to = tuple(str(x).strip().lower() for x in (t.get("to") or []) if str(x).strip())
                msgs.append(InboundMessage(
                    message_id=f"helpscout:{conv['id']}:{t['id']}", ticket=str(conv["id"]), sender=sender,
                    recipients=to or (self.support,), subject=str(conv.get("subject") or ""),
                    body=plain_text(str(t.get("body") or "")), received_at=at.isoformat()))
                newest = max(newest, at)
        msgs.sort(key=lambda m: (m.received_at, m.message_id))
        return msgs, newest.isoformat()

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        """Post ONE reply. Reads first: a reply with this key's marker (or this exact text) already in
        the conversation is returned, never posted again. Refuses a conversation in another inbox, with
        another primary customer, or with cc/bcc (the reply would widen past the approved recipient)."""
        want = str(recipient).strip().lower()
        conv = self.conversation(ticket)
        customer = conv.get("primaryCustomer") or {}
        if str(customer.get("email") or "").strip().lower() != want or not isinstance(customer.get("id"), int):
            raise HelpScoutError("the approved recipient is not this conversation's customer; refused")
        if conv.get("cc") or conv.get("bcc"):
            raise HelpScoutError("conversation has cc/bcc; a reply would reach more than the approved "
                                 "recipient; refused")
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        tag = key_tag(idempotency_key)
        for t in self.threads(ticket):
            if not self._is_reply(t):
                continue
            m = _MARK.search(str(t.get("body") or ""))
            # A marker names its key exactly. A reply WITHOUT any marker (Help Scout may strip HTML
            # comments) that has this exact text is treated as ours too: posting again could only duplicate.
            if (m and m.group(1) == tag) or (not m and plain_text(str(t.get("body") or "")) == body):
                return str(t["id"])   # already there: never post twice
        status, headers, _ = self.api.call("POST", f"/v2/conversations/{ticket}/reply", json_body={
            "customer": {"id": customer["id"]},
            "text": to_html(body) + marker(idempotency_key, body_sha)})
        rid = _header(headers, "Resource-ID")
        if status != 201 or not rid:
            raise HelpScoutError(f"Help Scout reply answer was not understood (HTTP {status})", status)
        return str(rid)

    def _record(self, ticket: str, t: dict, body_sha: str) -> SentRecord:
        to = [str(x).strip().lower() for x in (t.get("to") or []) if str(x).strip()]
        if not to:
            to = [self._email_of(t)] if (t.get("customer") or {}).get("email") else []
        everyone = to + [str(x).strip().lower() for x in (t.get("cc") or []) + (t.get("bcc") or [])]
        state = str(t.get("state") or "")
        sent = state == "published" and not t.get("scheduled") and state not in NOT_SENT_STATES
        return SentRecord(ticket=str(ticket), recipient=",".join(everyone), sender=self.inbox_email(),
                          subject="", body_sha=body_sha, message_id=f"helpscout:{ticket}:{t['id']}",
                          status="Sent" if sent else "Not Sent")

    def sent_readback(self, ticket, body_sha):
        out = []
        for t in self.threads(ticket):
            if not self._is_reply(t):
                continue
            raw = str(t.get("body") or "")
            m = _MARK.search(raw)
            if m:
                if m.group(2) != body_sha:
                    continue
            elif hashlib.sha256(plain_text(raw).encode()).hexdigest() != body_sha:
                continue
            out.append(self._record(str(ticket), t, body_sha))
        return out

    def last_sent_at(self, ticket):
        times = [utc_iso(t.get("createdAt")) for t in self.threads(ticket)
                 if self._is_reply(t) and t.get("state") == "published" and not t.get("scheduled")]
        return max(times, key=datetime.fromisoformat) if times else None

    def last_inbound_at(self, ticket, customer):
        """Newest customer thread on this conversation OR from this customer in any other conversation
        of the inbox (the newest few by modification). Unreadable raises; it never reads as quiet."""
        who = str(customer).strip().lower()
        if not _EMAIL.match(who):
            raise HelpScoutError("customer address is not usable in a Help Scout search")
        times = [utc_iso(t.get("createdAt")) for t in self.threads(ticket) if self._is_customer(t)]
        data = self.api.get("/v2/conversations", mailbox=self.mailbox_id, status="all",
                            query=f'(email:"{who}")', sortField="modifiedAt", sortOrder="desc", page=1)
        others = ((data or {}).get("_embedded") or {}).get("conversations")
        if not isinstance(others, list):
            raise HelpScoutError("Help Scout search answer was not understood")
        for conv in [c for c in others if str(c.get("id")) != str(ticket)][:OTHER_CONVERSATIONS]:
            times += [utc_iso(t.get("createdAt")) for t in self.threads(str(conv["id"]))
                      if self._is_customer(t) and self._email_of(t) == who]
        return max(times, key=datetime.fromisoformat) if times else None


class HelpScoutMailbox:
    """The receipt read from Help Scout itself (see the module note: NOT independent of the helpdesk).

    ``sent_copies("helpscout:<conversation>:<thread>")`` is 1 when that reply thread exists, is
    published and not scheduled; else 0. Any other id shape raises."""

    _ID = re.compile(r"^helpscout:(\d+):(\d+)$")

    def __init__(self, helpdesk: HelpScoutHelpdesk):
        self.helpdesk = helpdesk

    def sent_copies(self, message_id):
        m = self._ID.match(str(message_id))
        if not m:
            raise HelpScoutError("not a Help Scout reply id")
        return sum(1 for t in self.helpdesk.threads(m.group(1))
                   if str(t["id"]) == m.group(2) and HelpScoutHelpdesk._is_reply(t)
                   and t.get("state") == "published" and not t.get("scheduled"))


def build(name: str, section: Section, config: Config, transport=None):
    """``reference.build`` hands ``adapter = "helpscout"`` sections here."""
    helpdesk = HelpScoutHelpdesk(section, config.support_address, transport=transport)
    if name == "helpdesk":
        return helpdesk
    if name == "mailbox":
        return HelpScoutMailbox(helpdesk)
    raise ConfigError(f"no helpscout adapter for {name}")
