"""Help Scout adapter against a FAKE Help Scout built from the vendor docs' own examples.

No network, no account. A socket guard fails any connection beyond localhost, and
``SocketGuardTests`` seeds one outside call to prove the guard bites.
Fixture shapes: developer.helpscout.com/mailbox-api (List Conversations, List Threads,
Create Reply Thread, List Inboxes, Authentication, Rate limiting), fetched 16 Oct 2026.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

from sayso import cases, votes
from sayso.adapters import helpscout as hs
from sayso.adapters import reference
from sayso.config import ConfigError, Section, parse
from tests.helpers import LoopTest

# ----------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect


class OutsideCall(AssertionError):
    pass


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
        raise OutsideCall(f"test tried to reach {host!r}")
    return _real_connect(self, address)


def _guarded_getaddrinfo(host, *a, **k):
    if host not in ("127.0.0.1", "::1", "localhost", None):
        raise OutsideCall(f"test tried to resolve {host!r}")
    return _real_getaddrinfo(host, *a, **k)


_real_getaddrinfo = socket.getaddrinfo
ENV = {"HS_ID": "app-id-123", "HS_SECRET": "app-secret-xyz-987"}
_patches = [mock.patch.object(socket.socket, "connect", _guarded_connect),
            mock.patch.object(socket, "getaddrinfo", _guarded_getaddrinfo),
            mock.patch.dict(os.environ, ENV)]


def setUpModule():
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


# ----------------------------------------------------------------------------- fake Help Scout
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
SUPPORT = "support@example.com"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeHelpScout:
    """A tiny stateful Help Scout: one inbox, conversations, threads, OAuth tokens, a 429 knob.

    Every request is recorded in ``calls``. Response bodies copy the docs' example shapes."""

    def __init__(self, now=NOW):
        self.now = now
        self.calls = []
        self.tokens_issued = 0
        self.valid_tokens = set()
        self.rate_limit_next = 0          # answer the next N requests with 429
        self.fail = {}                    # (method, path-prefix) -> status
        self.mailbox = {"id": 13, "name": "Help Scout", "slug": "83ebc5d87292a795", "email": SUPPORT,
                        "createdAt": "2013-06-05T09:17:15Z", "updatedAt": "2016-11-11T12:20:39Z"}
        self.convs = {}
        self.next_thread = 1000
        self.page_size = 25

    # -- seeding (vendor example shapes)
    def add_conversation(self, cid, email="vbear@mywork.com", customer_id=29418, subject="Help",
                         mailbox=13, cc=(), bcc=(), ctype="email"):
        self.convs[cid] = {"conv": {
            "_links": {"web": {"href": "..."}}, "id": cid, "number": cid + 2, "threads": 0, "type": ctype,
            "folderId": 11, "status": "active", "state": "published", "subject": subject, "preview": "Preview",
            "mailboxId": mailbox, "createdBy": {"id": customer_id, "type": "customer", "email": email},
            "createdAt": iso(self.now - timedelta(hours=1)), "cc": list(cc), "bcc": list(bcc),
            "primaryCustomer": {"id": customer_id, "type": "customer", "first": "Vernon", "last": "Bear",
                                "email": email}}, "threads": [], "modified": self.now - timedelta(hours=1)}
        return self.convs[cid]["conv"]

    def customer_writes(self, cid, body, at, email=None, state="published"):
        conv = self.convs[cid]
        email = email or conv["conv"]["primaryCustomer"]["email"]
        self.next_thread += 1
        t = {"id": self.next_thread, "type": "customer", "status": "active", "state": state,
             "action": {"type": "manual-workflow", "text": "", "associatedEntities": {}},
             "body": body, "source": {"type": "email", "via": "customer"},
             "customer": {"id": conv["conv"]["primaryCustomer"]["id"], "first": "Vernon", "last": "Bear",
                          "email": email},
             "createdBy": {"id": conv["conv"]["primaryCustomer"]["id"], "type": "customer", "email": email},
             "to": [SUPPORT], "cc": [], "bcc": [], "createdAt": iso(at), "_embedded": {"attachments": []}}
        conv["threads"].insert(0, t)      # docs: newest first
        conv["modified"] = max(conv["modified"], at)
        return t

    def agent_thread(self, cid, body, at, ttype="message", state="published", by="user", **extra):
        conv = self.convs[cid]
        self.next_thread += 1
        t = {"id": self.next_thread, "type": ttype, "status": "active", "state": state, "body": body,
             "source": {"type": "api", "via": "user"},
             "customer": dict(conv["conv"]["primaryCustomer"]),
             "createdBy": {"id": 4, "type": by, "email": "agent@example.com"},
             "to": [conv["conv"]["primaryCustomer"]["email"]], "cc": [], "bcc": [], "createdAt": iso(at), **extra}
        conv["threads"].insert(0, t)
        conv["modified"] = max(conv["modified"], at)
        return t

    # -- HTTP
    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == "api.helpscout.net", url
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append({"method": method, "path": parts.path, "query": q, "headers": dict(headers),
                           "body": body.decode() if body else None})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return 429, {"X-RateLimit-Retry-After": "7", "X-RateLimit-Remaining-Minute": "0"}, b""
        for (m, prefix), status in self.fail.items():
            if m == method and parts.path.startswith(prefix):
                return status, {}, json.dumps({"message": "nope"}).encode()
        if parts.path == "/v2/oauth2/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form != {"grant_type": "client_credentials", "client_id": ENV["HS_ID"],
                        "client_secret": ENV["HS_SECRET"]}:
                return 401, {}, b'{"error":"invalid_client"}'
            self.tokens_issued += 1
            tok = f"369dbb08be58430086d2f8bd832bc1e{self.tokens_issued}"
            self.valid_tokens.add(tok)
            return 200, {}, json.dumps({"token_type": "bearer", "access_token": tok, "expires_in": 172800}).encode()
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_tokens:
            return 401, {}, b'{"error":"invalid_token"}'
        path = parts.path
        if method == "GET" and path == f"/v2/mailboxes/{self.mailbox['id']}":
            return self._json(self.mailbox)
        if method == "GET" and path == "/v2/conversations":
            return self._list_conversations(q)
        segs = path.split("/")
        if len(segs) >= 4 and segs[2] == "conversations" and segs[3].isdigit():
            cid = int(segs[3])
            if cid not in self.convs:
                return 404, {}, b""
            if method == "GET" and len(segs) == 4:
                c = dict(self.convs[cid]["conv"], threads=len(self.convs[cid]["threads"]))
                return self._json(c)
            if method == "GET" and segs[4:] == ["threads"]:
                rows = self.convs[cid]["threads"]
                return self._page("threads", rows, int(q.get("page", 1)), 50)
            if method == "POST" and segs[4:] == ["reply"]:
                data = json.loads(body)
                if not data.get("text") or "customer" not in data:
                    return 400, {}, b'{"message":"bad"}'
                if len(self.convs[cid]["threads"]) >= 99:
                    return 412, {}, b""
                t = self.agent_thread(cid, data["text"], self.now)
                return 201, {"Resource-ID": str(t["id"])}, b""
        return 404, {}, b""

    def _json(self, obj, status=200):
        return status, {"Content-Type": "application/hal+json"}, json.dumps(obj).encode()

    def _page(self, key, rows, page, size):
        total = max(1, -(-len(rows) // size)) if rows else 0
        chunk = rows[(page - 1) * size: page * size]
        return self._json({"_embedded": {key: chunk}, "_links": {"self": {"href": "..."}},
                           "page": {"number": page, "size": size, "totalElements": len(rows),
                                    "totalPages": total}})

    def _list_conversations(self, q):
        rows = list(self.convs.values())
        if "mailbox" in q:
            rows = [r for r in rows if str(r["conv"]["mailboxId"]) == q["mailbox"]]
        if "modifiedSince" in q:
            since = datetime.fromisoformat(q["modifiedSince"].replace("Z", "+00:00"))
            rows = [r for r in rows if r["modified"] > since]
        if "query" in q:
            email = q["query"].split('"')[1]
            rows = [r for r in rows if r["conv"]["primaryCustomer"]["email"] == email]
        rows.sort(key=lambda r: r["modified"], reverse=q.get("sortOrder") == "desc")
        return self._page("conversations", [r["conv"] for r in rows], int(q.get("page", 1)), self.page_size)


def section(**extra):
    return Section("helpscout", {"mailbox_id": 13, "client_id_env": "HS_ID", "client_secret_env": "HS_SECRET",
                                 **extra})


def desk(fake, **kw):
    waits = kw.pop("waits", [])
    return hs.HelpScoutHelpdesk(section(**kw), SUPPORT, transport=fake, sleep=waits.append, now=lambda: fake.now)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ----------------------------------------------------------------------------- tests
class SocketGuardTests(unittest.TestCase):
    def test_outside_call_is_refused(self):
        """Seeded outside call: the default transport aims at the real Help Scout and must be stopped."""
        with self.assertRaises(OutsideCall):
            hs.no_redirect_transport("GET", "https://api.helpscout.net/v2/users/me", {}, None)


class TokenTests(unittest.TestCase):
    def test_client_credentials_form_and_cache(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        d = desk(f)
        d.threads("1")
        d.threads("1")
        tok = [c for c in f.calls if c["path"] == "/v2/oauth2/token"]
        self.assertEqual(len(tok), 1, "a token is reused until it expires")
        self.assertEqual(tok[0]["headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_expired_token_401_refetches_once(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        d = desk(f)
        d.threads("1")
        f.valid_tokens.clear()            # Help Scout signals expiry only with 401
        d.threads("1")
        self.assertEqual(f.tokens_issued, 2)

    def test_token_near_expiry_is_renewed_before_use(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        d = desk(f)
        d.threads("1")
        f.now = NOW + timedelta(seconds=172800 - 60)
        d.api.now = lambda: f.now
        d.threads("1")
        self.assertEqual(f.tokens_issued, 2)

    def test_bad_credentials_refused_without_secret_in_error(self):
        f = FakeHelpScout()
        with mock.patch.dict(os.environ, {"HS_SECRET": "wrong-secret-555"}):
            d = desk(f)
            with self.assertRaises(hs.HelpScoutError) as cm:
                d.threads("1")
        self.assertNotIn("wrong-secret-555", str(cm.exception))
        self.assertNotIn("app-id-123", str(cm.exception))

    def test_secrets_come_from_env_names_only(self):
        with self.assertRaises(ConfigError):
            hs.HelpScoutHelpdesk(Section("helpscout", {"mailbox_id": 13, "client_id_env": "NOPE_UNSET",
                                                       "client_secret_env": "HS_SECRET"}), SUPPORT)

    def test_mailbox_id_required(self):
        with self.assertRaises(ConfigError):
            hs.HelpScoutHelpdesk(Section("helpscout", {"client_id_env": "HS_ID", "client_secret_env": "HS_SECRET"}),
                                 SUPPORT)

    def test_token_store_used(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        saved = {}

        class Store:
            def get(self):
                return saved.get("t")

            def put(self, tok, exp):
                saved["t"] = (tok, exp)
        d1 = hs.HelpScoutHelpdesk(section(), SUPPORT, transport=f, now=lambda: NOW, token_store=Store())
        d1.threads("1")
        d2 = hs.HelpScoutHelpdesk(section(), SUPPORT, transport=f, now=lambda: NOW, token_store=Store())
        d2.threads("1")
        self.assertEqual(f.tokens_issued, 1, "a stored token is reused by a new adapter")


class RateLimitTests(unittest.TestCase):
    def test_429_waits_retry_after_header_then_succeeds(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        waits = []
        d = desk(f, waits=waits)
        d.threads("1")                      # token first
        f.rate_limit_next = 2
        d.threads("1")
        self.assertEqual(waits, [7.0, 7.0])

    def test_429_gives_up_and_raises(self):
        f = FakeHelpScout()
        f.add_conversation(1)
        d = desk(f)
        d.threads("1")
        f.rate_limit_next = 10
        with self.assertRaises(hs.HelpScoutRateLimited):
            d.threads("1")

    def test_wait_is_capped(self):
        f = FakeHelpScout()
        waits = []
        api = hs.HelpScoutApi("a", "b", lambda *a: (429, {"x-ratelimit-retry-after": "9999"}, b""),
                              sleep=waits.append, max_retries=1)
        with self.assertRaises(hs.HelpScoutRateLimited):
            api._raw("GET", "/v2/x", {}, None)
        self.assertEqual(waits, [hs.MAX_WAIT])
        del f


class FetchInboundTests(unittest.TestCase):
    def setUp(self):
        self.f = FakeHelpScout()
        self.f.add_conversation(1, subject="Export button does nothing")
        self.d = desk(self.f)

    def test_only_published_customer_threads(self):
        f = self.f
        f.customer_writes(1, "It fails.<br>Please help &amp; fast", NOW - timedelta(minutes=30))
        f.agent_thread(1, "internal", NOW - timedelta(minutes=20), ttype="note")
        f.agent_thread(1, "we replied", NOW - timedelta(minutes=10))
        f.agent_thread(1, "", NOW - timedelta(minutes=5), ttype="lineitem", by="user")
        f.customer_writes(1, "draft?", NOW - timedelta(minutes=4), state="draft")
        msgs, cursor = self.d.fetch_inbound(None)
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual((m.ticket, m.sender, m.subject), ("1", "vbear@mywork.com", "Export button does nothing"))
        self.assertEqual(m.body, "It fails.\nPlease help & fast")
        self.assertEqual(m.received_at, "2026-10-16T08:30:00+00:00")
        self.assertTrue(m.message_id.startswith("helpscout:1:"))
        self.assertEqual(cursor, "2026-10-16T08:30:00+00:00")

    def test_query_filters_inbox_all_statuses_modified_since(self):
        self.d.fetch_inbound("2026-10-16T08:00:00+00:00")
        q = [c for c in self.f.calls if c["path"] == "/v2/conversations"][0]["query"]
        self.assertEqual(q["mailbox"], "13")
        self.assertEqual(q["status"], "all")
        self.assertEqual(q["modifiedSince"], "2026-10-16T07:55:00Z")   # 5 min overlap
        self.assertEqual((q["sortField"], q["sortOrder"]), ("modifiedAt", "asc"))

    def test_first_run_reads_only_start_hours_back(self):
        self.f.customer_writes(1, "ancient", NOW - timedelta(days=3))
        self.f.customer_writes(1, "recent", NOW - timedelta(hours=2))
        msgs, _ = self.d.fetch_inbound(None)
        self.assertEqual([m.body for m in msgs], ["recent"])

    def test_cursor_moves_and_overlap_replays_same_ids(self):
        self.f.customer_writes(1, "one", NOW - timedelta(minutes=30))
        msgs, cur = self.d.fetch_inbound(None)
        self.f.customer_writes(1, "two", NOW - timedelta(minutes=28))
        msgs2, cur2 = self.d.fetch_inbound(cur)
        self.assertEqual([m.body for m in msgs2], ["one", "two"])  # overlap re-reads; intake dedupes by id
        self.assertEqual(msgs2[0].message_id, msgs[0].message_id)
        self.assertGreater(cur2, cur)

    def test_no_new_mail_keeps_cursor(self):
        _, cur = self.d.fetch_inbound("2026-10-16T08:00:00+00:00")
        self.assertEqual(cur, "2026-10-16T08:00:00+00:00")

    def test_other_inbox_and_chat_conversations_skipped(self):
        self.f.add_conversation(2, mailbox=99)
        self.f.customer_writes(2, "other inbox", NOW - timedelta(minutes=5))
        self.f.add_conversation(3, ctype="chat")
        self.f.customer_writes(3, "a chat", NOW - timedelta(minutes=5))
        msgs, _ = self.d.fetch_inbound(None)
        self.assertEqual(msgs, [])

    def test_pagination_reads_every_page(self):
        self.f.page_size = 2
        for cid in range(10, 15):
            self.f.add_conversation(cid, email=f"c{cid}@ex.com", customer_id=cid)
            self.f.customer_writes(cid, f"hi {cid}", NOW - timedelta(minutes=60 - cid))
        msgs, _ = self.d.fetch_inbound(None)
        self.assertEqual(len(msgs), 5)
        pages = [c["query"]["page"] for c in self.f.calls if c["path"] == "/v2/conversations"]
        self.assertEqual(pages, ["1", "2", "3"])

    def test_naive_time_refused(self):
        t = self.f.customer_writes(1, "x", NOW - timedelta(minutes=5))
        t["createdAt"] = "2026-10-16T08:55:00"
        with self.assertRaises(hs.HelpScoutError):
            self.d.fetch_inbound(None)

    def test_thread_without_email_refused(self):
        t = self.f.customer_writes(1, "x", NOW - timedelta(minutes=5))
        t["customer"]["email"] = ""
        t["createdBy"]["email"] = ""
        with self.assertRaises(hs.HelpScoutError):
            self.d.fetch_inbound(None)

    def test_unreadable_list_raises(self):
        self.f.fail[("GET", "/v2/conversations")] = 500
        with self.assertRaises(hs.HelpScoutError):
            self.d.fetch_inbound(None)


class SendReplyTests(unittest.TestCase):
    BODY = "Hi Vernon,\n\nThanks <3 - try again now."

    def setUp(self):
        self.f = FakeHelpScout()
        self.f.add_conversation(1)
        self.f.customer_writes(1, "help", NOW - timedelta(minutes=30))
        self.d = desk(self.f)

    def posts(self):
        return [c for c in self.f.calls if c["method"] == "POST" and c["path"].endswith("/reply")]

    def test_reply_shape_marker_and_resource_id(self):
        rid = self.d.send_reply("1", "vbear@mywork.com", "Re: Help", self.BODY, idempotency_key="reply:k1")
        (post,) = self.posts()
        data = json.loads(post["body"])
        self.assertEqual(data["customer"], {"id": 29418})
        self.assertNotIn("draft", data)
        self.assertNotIn("imported", data)
        self.assertIn(hs.marker("reply:k1", sha(self.BODY)), data["text"])
        self.assertIn("Thanks &lt;3", data["text"])
        self.assertEqual(rid, str(self.f.convs[1]["threads"][0]["id"]))
        self.assertEqual(hs.plain_text(data["text"]), self.BODY)

    def test_same_key_never_posts_twice(self):
        a = self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="reply:k1")
        b = self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="reply:k1")
        self.assertEqual(a, b)
        self.assertEqual(len(self.posts()), 1)

    def test_marker_stripped_by_vendor_still_no_duplicate(self):
        self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="reply:k1")
        t = self.f.convs[1]["threads"][0]
        t["body"] = hs.to_html(self.BODY)          # vendor dropped the HTML comment
        self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="reply:k1")
        self.assertEqual(len(self.posts()), 1)

    def test_other_recipient_refused(self):
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1", "someone@else.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.posts(), [])

    def test_conversation_in_other_inbox_refused(self):
        self.f.convs[1]["conv"]["mailboxId"] = 77
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.posts(), [])

    def test_cc_on_conversation_refused(self):
        self.f.convs[1]["conv"]["cc"] = ["boss@mywork.com"]
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.posts(), [])

    def test_unreadable_threads_means_no_post(self):
        self.f.fail[("GET", "/v2/conversations/1/threads")] = 503
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.posts(), [])

    def test_non_numeric_ticket_refused(self):
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1/../../x", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.f.calls, [])

    def test_merged_conversation_301_refused(self):
        self.f.fail[("GET", "/v2/conversations/1")] = 301
        with self.assertRaises(hs.HelpScoutError):
            self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.posts(), [])

    def test_201_without_resource_id_raises(self):
        orig = self.f.__call__

        def no_id(method, url, headers, body):
            st, h, raw = orig(method, url, headers, body)
            return (st, {}, raw) if method == "POST" and url.endswith("/reply") else (st, h, raw)
        d = hs.HelpScoutHelpdesk(section(), SUPPORT, transport=no_id, now=lambda: NOW)
        with self.assertRaises(hs.HelpScoutError):
            d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")

    def test_412_full_conversation_raises(self):
        for i in range(99):
            self.f.agent_thread(1, f"n{i}", NOW, ttype="note")
        with self.assertRaises(hs.HelpScoutError) as cm:
            self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(cm.exception.status, 412)


class ReadbackTests(unittest.TestCase):
    BODY = "Fixed it for you."

    def setUp(self):
        self.f = FakeHelpScout()
        self.f.add_conversation(1)
        self.f.customer_writes(1, "help", NOW - timedelta(minutes=30))
        self.d = desk(self.f)

    def test_published_reply_is_one_sent_record(self):
        self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        (row,) = self.d.sent_readback("1", sha(self.BODY))
        self.assertEqual((row.status, row.recipient, row.sender), ("Sent", "vbear@mywork.com", SUPPORT))
        self.assertTrue(row.message_id.startswith("helpscout:1:"))
        self.assertEqual(hs.HelpScoutMailbox(self.d).sent_copies(row.message_id), 1)

    def test_other_body_not_matched(self):
        self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.d.sent_readback("1", sha("other text")), [])

    def test_not_sent_states(self):
        for state in ("draft", "review", "underreview", "hidden", "bounced"):
            with self.subTest(state=state):
                f = FakeHelpScout()
                f.add_conversation(1)
                f.agent_thread(1, hs.to_html(self.BODY) + hs.marker("k", sha(self.BODY)), NOW, state=state)
                d = desk(f)
                (row,) = d.sent_readback("1", sha(self.BODY))
                self.assertEqual(row.status, "Not Sent")
                self.assertEqual(hs.HelpScoutMailbox(d).sent_copies(row.message_id), 0)

    def test_scheduled_reply_not_sent(self):
        self.f.agent_thread(1, hs.to_html(self.BODY) + hs.marker("k", sha(self.BODY)), NOW,
                            scheduled={"scheduledFor": "2034-06-10T10:20:30Z"})
        (row,) = self.d.sent_readback("1", sha(self.BODY))
        self.assertEqual(row.status, "Not Sent")

    def test_cc_widens_recipient_so_receipt_fails(self):
        self.f.agent_thread(1, hs.to_html(self.BODY) + hs.marker("k", sha(self.BODY)), NOW, cc=["boss@x.com"])
        (row,) = self.d.sent_readback("1", sha(self.BODY))
        self.assertEqual(row.recipient, "vbear@mywork.com,boss@x.com")

    def test_two_copies_both_returned(self):
        for _ in range(2):
            self.f.agent_thread(1, hs.to_html(self.BODY) + hs.marker("k", sha(self.BODY)), NOW)
        self.assertEqual(len(self.d.sent_readback("1", sha(self.BODY))), 2)

    def test_customer_thread_with_same_text_is_not_a_reply(self):
        self.f.customer_writes(1, self.BODY, NOW)
        self.assertEqual(self.d.sent_readback("1", sha(self.BODY)), [])

    def test_mailbox_refuses_foreign_id(self):
        with self.assertRaises(hs.HelpScoutError):
            hs.HelpScoutMailbox(self.d).sent_copies("<abc@mail>")

    def test_last_sent_and_inbound(self):
        self.assertIsNone(self.d.last_sent_at("1"))
        self.d.send_reply("1", "vbear@mywork.com", "s", self.BODY, idempotency_key="k")
        self.assertEqual(self.d.last_sent_at("1"), "2026-10-16T09:00:00+00:00")
        self.assertEqual(self.d.last_inbound_at("1", "vbear@mywork.com"), "2026-10-16T08:30:00+00:00")

    def test_last_inbound_includes_other_conversations_of_customer(self):
        self.f.add_conversation(2, subject="New thread")
        self.f.customer_writes(2, "me again", NOW - timedelta(minutes=2))
        self.assertEqual(self.d.last_inbound_at("1", "vbear@mywork.com"), "2026-10-16T08:58:00+00:00")
        q = [c for c in self.f.calls if c["path"] == "/v2/conversations"][0]["query"]
        self.assertEqual(q["query"], '(email:"vbear@mywork.com")')

    def test_last_inbound_unreadable_raises(self):
        self.f.fail[("GET", "/v2/conversations")] = 500
        with self.assertRaises(hs.HelpScoutError):
            self.d.last_inbound_at("1", "vbear@mywork.com")

    def test_last_inbound_refuses_query_injection(self):
        with self.assertRaises(hs.HelpScoutError):
            self.d.last_inbound_at("1", 'x" OR email:"*')

    def test_web_url(self):
        self.assertEqual(hs.web_url(123), "https://secure.helpscout.net/conversation/123")
        with self.assertRaises(ValueError):
            hs.web_url("1/../evil")


class ConfigTests(unittest.TestCase):
    def raw(self, mode="live"):
        return {"product": {"slug": "acme", "mode": mode, "support_address": SUPPORT},
                "operators": {"1": "a"}, "state": {"dir": "/tmp/x"},
                "helpdesk": {"adapter": "helpscout", "mailbox_id": 13, "client_id_env": "HS_ID",
                             "client_secret_env": "HS_SECRET"},
                "mailbox": {"adapter": "helpscout", "mailbox_id": 13, "client_id_env": "HS_ID",
                            "client_secret_env": "HS_SECRET"}}

    def test_choice_and_build(self):
        from pathlib import Path
        cfg = parse(self.raw(), Path("/tmp/acme.toml"))
        helpdesk = reference.build("helpdesk", "helpscout", cfg.section("helpdesk"), cfg)
        mailbox = reference.build("mailbox", "helpscout", cfg.section("mailbox"), cfg)
        self.assertIsInstance(helpdesk, hs.HelpScoutHelpdesk)
        self.assertIsInstance(mailbox, hs.HelpScoutMailbox)

    def test_needs_live_mode(self):
        from pathlib import Path
        with self.assertRaises(ConfigError):
            parse(self.raw("dry-run"), Path("/tmp/acme.toml"))

    def test_secret_in_file_refused(self):
        from pathlib import Path
        raw = self.raw()
        raw["helpdesk"]["client_secret_env"] = "abc-real-secret"
        with self.assertRaises(ConfigError):
            parse(raw, Path("/tmp/acme.toml"))


class LoopEndToEndTests(LoopTest):
    """The real intake -> draft -> vote -> sender jobs over Help Scout adapters and the fake server."""

    def setUp(self):
        super().setUp()
        self.f = FakeHelpScout()
        self.f.add_conversation(7, email="pat@customer.test", customer_id=55, subject="Export button does nothing")
        d = hs.HelpScoutHelpdesk(section(), self.ctx.config.support_address, transport=self.f,
                                 sleep=lambda s: None, now=lambda: self.f.now)
        self.ctx.helpdesk, self.ctx.mailbox = d, hs.HelpScoutMailbox(d)

    def test_one_approved_reply_sent_once_and_verified(self):
        self.f.customer_writes(7, "It fails.", NOW - timedelta(minutes=3))
        self.tick("intake")
        key, rec = self.only_case()
        self.assertEqual(rec["ticket"], "7")
        self.tick("draft")
        v = self.vote("reply")
        self.tick("votes")
        out = self.tick("sender")["sender"]
        self.assertEqual(out[v["key"]], "SENT_AND_VERIFIED")
        posts = [c for c in self.f.calls if c["method"] == "POST" and c["path"].endswith("/reply")]
        self.assertEqual(len(posts), 1)
        self.tick("sender")
        posts = [c for c in self.f.calls if c["method"] == "POST" and c["path"].endswith("/reply")]
        self.assertEqual(len(posts), 1, "a second tick never sends again")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)

    def test_post_lost_in_flight_is_not_resent(self):
        """The POST reached Help Scout but the answer was lost: the sender re-reads, finds it, never re-posts."""
        self.f.customer_writes(7, "It fails.", NOW - timedelta(minutes=3))
        self.tick("intake")
        self.tick("draft")
        v = self.vote("reply")
        self.tick("votes")
        orig = self.f.__call__

        def lose_answer(method, url, headers, body):
            st, h, raw = orig(method, url, headers, body)
            if method == "POST" and url.endswith("/reply"):
                raise TimeoutError("answer lost")
            return st, h, raw
        self.ctx.helpdesk.api.http.transport = lose_answer
        out = self.tick("sender")["sender"]
        self.assertEqual(out[v["key"]], "SENT_AND_VERIFIED")
        posts = [c for c in self.f.calls if c["method"] == "POST" and c["path"].endswith("/reply")]
        self.assertEqual(len(posts), 1)

    def test_customer_writes_again_reopens_same_case(self):
        self.f.customer_writes(7, "It fails.", NOW - timedelta(minutes=3))
        self.tick("intake")
        self.f.now = NOW + timedelta(minutes=10)
        self.f.customer_writes(7, "Still broken", NOW + timedelta(minutes=9))
        out = self.tick("intake")["intake"]
        self.assertEqual(out["appended"], 1)
        self.assertEqual(len(cases.load(self.ctx)), 1)


if __name__ == "__main__":
    unittest.main()
