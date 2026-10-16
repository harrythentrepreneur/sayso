"""Crisp helpdesk adapter against recorded doc fixtures. No network, no account.

Every response body below is built from the request/response examples published in the Crisp REST API
reference (tests/fixtures/crisp_docs.json, copied verbatim from https://docs.crisp.chat/references/rest-api/v1/).
A socket guard fails the run on any connection that is not to localhost.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from sayso.adapters import crisp, reference
from sayso.adapters.http import HttpError
from sayso.config import ConfigError, Section

DOCS = json.loads((Path(__file__).parent / "fixtures" / "crisp_docs.json").read_text(encoding="utf-8"))
WEBSITE = DOCS["list-conversations"]["responses"]["200 OK"]["data"][0]["website_id"]
SESSION = DOCS["list-conversations"]["responses"]["200 OK"]["data"][0]["session_id"]
OTHER = DOCS["list-people-conversations"]["responses"]["200 OK"]["data"][0]
EMAIL = DOCS["list-conversations"]["responses"]["200 OK"]["data"][0]["meta"]["email"]
ENV = {"CR_ID": "ident-123", "CR_KEY": "key-456"}
SUPPORT = "support@acme.example"
T0 = 1_792_000_000_000  # epoch ms, Oct 2026


# --------------------------------------------------------------------------- socket guard
_LOCAL = ("127.0.0.1", "::1", "localhost")
_real_connect = socket.socket.connect
_real_create = socket.create_connection
_guard_hits: list = []


def _host(addr):
    return addr[0] if isinstance(addr, tuple) else str(addr)


def _guarded_connect(self, addr):
    if self.family in (socket.AF_INET, socket.AF_INET6) and _host(addr) not in _LOCAL:
        _guard_hits.append(addr)
        raise AssertionError(f"socket guard: outside connection to {addr!r} refused in tests")
    return _real_connect(self, addr)


def _guarded_create(addr, *a, **k):
    if _host(addr) not in _LOCAL:
        _guard_hits.append(addr)
        raise AssertionError(f"socket guard: outside connection to {addr!r} refused in tests")
    return _real_create(addr, *a, **k)


def setUpModule():
    socket.socket.connect = _guarded_connect
    socket.create_connection = _guarded_create


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


# --------------------------------------------------------------------------- fake Crisp from doc examples
def _doc(name, status):
    return copy.deepcopy(DOCS[name]["responses"][status])


def _msg(session, frm, content, ts, fp, typ="text", stamped=True):
    m = copy.deepcopy(DOCS["get-messages"]["responses"]["200 OK"]["data"][0])
    m.update({"session_id": session, "website_id": WEBSITE, "from": frm, "content": content,
              "timestamp": ts, "fingerprint": fp, "type": typ, "stamped": stamped})
    return m


class FakeCrisp:
    """A transport that answers like api.crisp.chat, using the docs' example envelopes."""

    def __init__(self):
        self.convs: dict[str, dict] = {}
        self.people: dict[str, list[str]] = {}
        self.calls: list[dict] = []
        self.fail: list[tuple[int, dict]] = []   # queued (status, body) answers for the next calls
        self.batch = 50                          # messages per page (Crisp returns "the last batch")
        self.post_lands = True                   # False = the POST errors AFTER storing (lost reply)

    def conv(self, session, email, subject="Refund please", updated=T0):
        c = copy.deepcopy(DOCS["list-conversations"]["responses"]["200 OK"]["data"][0])
        c.update({"session_id": session, "website_id": WEBSITE, "updated_at": updated})
        c["meta"]["email"], c["meta"]["subject"] = email, subject
        self.convs[session] = {"row": c, "messages": []}
        self.people.setdefault(email, []).append(session)
        return c

    def add(self, session, frm, content, ts, fp, **kw):
        self.convs[session]["messages"].append(_msg(session, frm, content, ts, fp, **kw))
        self.convs[session]["row"]["updated_at"] = max(self.convs[session]["row"]["updated_at"], ts)

    def _ok(self, data, reason="listed", status=200):
        body = {"error": False, "reason": reason, "data": data}
        return status, {}, json.dumps(body).encode()

    def _err(self, name, status):
        return int(status.split()[0]), {}, json.dumps(_doc(name, status)).encode()

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append({"method": method, "url": url, "path": parsed.path, "query": q, "headers": headers,
                           "body": json.loads(body) if body else None})
        if self.fail:
            status, payload = self.fail.pop(0)
            return status, {}, json.dumps(payload).encode()
        assert parsed.scheme == "https" and parsed.hostname == "api.crisp.chat", url
        parts = parsed.path.split("/")[1:]   # v1 website <id> ...
        assert parts[:3] == ["v1", "website", WEBSITE], parsed.path
        rest = parts[3:]
        if not rest and method == "GET":
            data = _doc("get-website-information", "200 OK")["data"]
            data["website_id"] = WEBSITE
            return self._ok(data, "resolved")
        if rest[:1] == ["conversations"] and method == "GET":
            page, per = int(rest[1]), int(q.get("per_page", 20))
            assert 20 <= per <= 50, "docs: per_page between 20 and 50"
            since = q.get("filter_date_start")
            rows = [c["row"] for c in self.convs.values()]
            if since:
                from datetime import datetime
                ms = int(datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() * 1000)
                rows = [r for r in rows if r["updated_at"] >= ms]
            rows = sorted(rows, key=lambda r: -r["updated_at"])[(page - 1) * per: page * per]
            return self._ok(rows)
        if rest[:1] == ["conversation"]:
            session = rest[1]
            if session not in self.convs:
                return self._err("get-messages", "404 Not Found")
            c = self.convs[session]
            if rest[2:] == ["meta"] and method == "GET":
                data = _doc("get-metas", "200 OK")["data"]
                data.update({"email": c["row"]["meta"]["email"], "subject": c["row"]["meta"].get("subject")})
                return self._ok(data, "resolved")
            if rest[2:] == ["messages"] and method == "GET":
                msgs = sorted(c["messages"], key=lambda m: m["timestamp"])
                if "timestamp_before" in q:
                    msgs = [m for m in msgs if m["timestamp"] < int(q["timestamp_before"])]
                return self._ok(msgs[-self.batch:])
            if rest[2] == "message" and len(rest) == 4 and method == "GET":
                fp = int(rest[3])
                hit = [m for m in c["messages"] if m["fingerprint"] == fp]
                if not hit:
                    body = _doc("get-a-message", "404 Not Found")
                    body["reason"] = "message_not_found"
                    return 404, {}, json.dumps(body).encode()
                return self._ok(hit[0], "resolved")
            if rest[2:] == ["message"] and method == "POST":
                req = json.loads(body)
                assert set(req) <= {"type", "from", "origin", "content", "fingerprint"}, req
                fp = req.get("fingerprint") or 777
                self.add(session, req["from"], req["content"], T0 + 10_000 + len(c["messages"]), fp)
                if not self.post_lands:
                    return 502, {}, b"bad gateway"
                answer = _doc("send-text-message", "202 Accepted")
                answer["data"]["fingerprint"] = fp
                return 202, {}, json.dumps(answer).encode()
        if rest[:2] == ["people", "conversations"] and method == "GET":
            who, page = urllib.parse.unquote(rest[2]), int(rest[4])
            if who not in self.people:
                body = _doc("list-people-conversations", "404 Not Found")
                body["reason"] = "people_not_found"
                return 404, {}, json.dumps(body).encode()
            return self._ok(self.people[who][(page - 1) * 20: page * 20])
        raise AssertionError(f"unexpected call {method} {parsed.path}")


def section(**over):
    opts = {"website_id": WEBSITE, "identifier_env": "CR_ID", "key_env": "CR_KEY"}
    opts.update(over)
    return Section("crisp", opts)


def helpdesk(fake, **over):
    return crisp.CrispHelpdesk(section(**over), SUPPORT, transport=fake, sleep=lambda s: None,
                               now=lambda: (T0 + 3_600_000) / 1000)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- tests
class SocketGuardTests(unittest.TestCase):
    def test_guard_bites_on_an_outside_connection(self):
        """Seeded outside call: the real urllib transport to api.crisp.chat must be stopped by the guard."""
        before = len(_guard_hits)
        with mock.patch.dict(os.environ, ENV):
            h = crisp.CrispHelpdesk(section(), SUPPORT)   # no transport: the REAL urllib one
            with self.assertRaises(Exception):
                h.api.website_info()
        self.assertGreater(len(_guard_hits), before, "socket guard did not see the outside connection")

    def test_localhost_still_allowed(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            c = socket.create_connection(srv.getsockname(), timeout=2)
            c.close()
        finally:
            srv.close()


@mock.patch.dict(os.environ, ENV)
class ConfigTests(unittest.TestCase):
    def test_auth_headers_plugin_tier(self):
        fake = FakeCrisp()
        h = helpdesk(fake)
        h.api.website_info()
        hdr = fake.calls[0]["headers"]
        self.assertEqual(hdr["Authorization"], "Basic " + base64.b64encode(b"ident-123:key-456").decode())
        self.assertEqual(hdr["X-Crisp-Tier"], "plugin")
        self.assertTrue(fake.calls[0]["url"].startswith("https://api.crisp.chat/v1/website/" + WEBSITE))

    def test_website_tier(self):
        fake = FakeCrisp()
        helpdesk(fake, tier="website").api.website_info()
        self.assertEqual(fake.calls[0]["headers"]["X-Crisp-Tier"], "website")

    def test_bad_settings_refused(self):
        for over in ({"tier": "user"}, {"website_id": "../x"}, {"website_id": ""}, {"start_days": 0},
                     {"start_days": "x"}):
            with self.assertRaises(ConfigError, msg=over):
                helpdesk(FakeCrisp(), **over)

    def test_missing_secret_refused(self):
        with mock.patch.dict(os.environ, {"CR_KEY": ""}):
            with self.assertRaises(ConfigError):
                helpdesk(FakeCrisp())

    def test_choice_and_build(self):
        from sayso import config
        self.assertIn("crisp", config.ADAPTER_CHOICES["helpdesk"])
        self.assertIn("crisp", config.ADAPTER_CHOICES["mailbox"])
        cfg = mock.Mock(support_address=SUPPORT)
        with mock.patch.object(crisp, "CrispHelpdesk") as hd, mock.patch.object(crisp, "CrispMailbox") as mb:
            reference.build("helpdesk", "crisp", section(), cfg)
            reference.build("mailbox", "crisp", section(), cfg)
        hd.assert_called_once()
        mb.assert_called_once()
        with self.assertRaises(ConfigError):
            crisp.build("payments", section(), cfg)

    def test_live_settings_file_parses(self):
        from sayso import config
        with tempfile.TemporaryDirectory() as d:
            raw = {"product": {"slug": "acme", "mode": "live", "support_address": SUPPORT},
                   "operators": {"1": "Ann"}, "state": {"dir": d},
                   "helpdesk": {"adapter": "crisp", "website_id": WEBSITE, "identifier_env": "CR_ID",
                                "key_env": "CR_KEY"},
                   "mailbox": {"adapter": "crisp", "website_id": WEBSITE, "identifier_env": "CR_ID",
                               "key_env": "CR_KEY"}}
            cfg = config.parse(raw, Path(d) / "x.toml")
            self.assertEqual(cfg.section("helpdesk").adapter, "crisp")
            raw["helpdesk"]["key_env"] = "not a var name"
            with self.assertRaises(ConfigError):
                config.parse(raw, Path(d) / "x.toml")


@mock.patch.dict(os.environ, ENV)
class InboundTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCrisp()
        self.fake.conv(SESSION, EMAIL)
        self.fake.add(SESSION, "user", "Hey, how are you doing?", T0 + 1000, 5719231201)
        self.fake.add(SESSION, "operator", "👍", T0 + 2000, 5720387819)
        self.fake.add(SESSION, "user", "Can I get a refund?", T0 + 3000, 9663657330)
        self.fake.add(SESSION, "user", "internal", T0 + 3500, 111, typ="note")

    def test_user_messages_only_with_iso_times(self):
        msgs, cur = helpdesk(self.fake).fetch_inbound(None)
        self.assertEqual([m.body for m in msgs], ["Hey, how are you doing?", "Can I get a refund?"])
        m = msgs[0]
        self.assertEqual((m.ticket, m.sender, m.subject), (SESSION, EMAIL, "Refund please"))
        self.assertEqual(m.message_id, f"crisp:{SESSION}:5719231201")
        self.assertTrue(m.received_at.endswith("+00:00"))
        self.assertEqual(m.recipients, (SUPPORT,))
        self.assertEqual(cur, str(T0 + 3500))
        listing = self.fake.calls[0]
        self.assertEqual(listing["query"]["order_date_updated"], "1")
        self.assertEqual(listing["query"]["per_page"], "50")

    def test_cursor_moves_and_rereads_same_ms(self):
        h = helpdesk(self.fake)
        _, cur = h.fetch_inbound(None)
        self.fake.add(SESSION, "user", "One more thing", T0 + 9000, 42)
        msgs, cur2 = h.fetch_inbound(cur)
        self.assertEqual([m.body for m in msgs], ["One more thing"])
        self.assertEqual(cur2, str(T0 + 9000))
        again, _ = h.fetch_inbound(cur2)
        self.assertEqual([m.message_id for m in again], [f"crisp:{SESSION}:42"])  # same id: intake dedupes

    def test_conversation_without_email_skipped(self):
        self.fake.conv("session_" + "1" * 8 + "-1111-1111-1111-" + "1" * 12, "")
        sid = list(self.fake.convs)[-1]
        self.fake.add(sid, "user", "anon", T0 + 5000, 5)
        h = helpdesk(self.fake)
        msgs, _ = h.fetch_inbound(None)
        self.assertNotIn("anon", [m.body for m in msgs])
        self.assertEqual(h.skipped_no_email, 1)

    def test_file_message_named_not_dropped(self):
        self.fake.add(SESSION, "user", {"name": "invoice.pdf", "url": "https://storage.crisp.chat/x",
                                        "type": "application/pdf"}, T0 + 4000, 77, typ="file")
        msgs, _ = helpdesk(self.fake).fetch_inbound(None)
        self.assertIn("[file: invoice.pdf]", [m.body for m in msgs])

    def test_unknown_type_raises(self):
        self.fake.add(SESSION, "user", "x", T0 + 4000, 78, typ="hologram")
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound(None)

    def test_unknown_sender_raises(self):
        self.fake.add(SESSION, "robot", "x", T0 + 4000, 79)
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound(None)

    def test_seconds_timestamp_raises(self):
        self.fake.add(SESSION, "user", "x", 1_792_000_100, 80)   # seconds, not ms
        self.fake.convs[SESSION]["row"]["updated_at"] = T0 + 5000
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound(None)

    def test_error_envelope_raises(self):
        self.fake.fail = [(200, {"error": True, "reason": "listed", "data": []})]
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound(None)

    def test_bad_cursor_raises(self):
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound("2026-10-01")

    def test_pages_back_for_older_messages(self):
        self.fake.batch = 2
        msgs, _ = helpdesk(self.fake).fetch_inbound(None)
        self.assertEqual([m.body for m in msgs], ["Hey, how are you doing?", "Can I get a refund?"])
        self.assertTrue(any("timestamp_before" in c["query"] for c in self.fake.calls))

    def test_page_cap_raises_never_truncates(self):
        for i in range(crisp.MAX_PAGES * 2 + 2):
            self.fake.add(SESSION, "user", f"m{i}", T0 + 10_000 + i, 1000 + i)
        self.fake.batch = 1
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).fetch_inbound(str(T0))

    def test_docs_error_bodies_raise(self):
        for status in ("402 Payment Required", "403 Forbidden", "404 Not Found"):
            self.fake.fail = [(int(status.split()[0]), _doc("list-conversations", status))]
            with self.assertRaises(HttpError, msg=status):
                helpdesk(self.fake).fetch_inbound(None)


@mock.patch.dict(os.environ, ENV)
class SendTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCrisp()
        self.fake.conv(SESSION, EMAIL)
        self.fake.add(SESSION, "user", "Can I get a refund?", T0 + 1000, 5719231201)

    def posts(self):
        return [c for c in self.fake.calls if c["method"] == "POST"]

    def test_send_body_and_fingerprint(self):
        h = helpdesk(self.fake)
        mid = h.send_reply(SESSION, EMAIL, "Re: x", "Refund approved.", "reply:k1")
        fp = crisp.crisp_fingerprint("reply:k1")
        self.assertEqual(mid, f"crisp:{SESSION}:{fp}")
        body = self.posts()[0]["body"]
        self.assertEqual(body, {"type": "text", "from": "operator", "origin": "chat",
                                "content": "Refund approved.", "fingerprint": fp})
        self.assertLess(fp, 2 ** 53)
        # The docs' own send example uses exactly these keys plus our fingerprint.
        self.assertEqual(set(DOCS["send-text-message"]["request"]) | {"fingerprint"}, set(body))

    def test_retry_with_same_key_never_posts_twice(self):
        h = helpdesk(self.fake)
        h.send_reply(SESSION, EMAIL, "s", "Refund approved.", "reply:k1")
        h.send_reply(SESSION, EMAIL, "s", "Refund approved.", "reply:k1")
        self.assertEqual(len(self.posts()), 1)

    def test_lost_answer_then_retry_does_not_duplicate(self):
        self.fake.post_lands = False
        h = helpdesk(self.fake)
        with self.assertRaises(HttpError):
            h.send_reply(SESSION, EMAIL, "s", "Refund approved.", "reply:k2")
        self.fake.post_lands = True
        h.send_reply(SESSION, EMAIL, "s", "Refund approved.", "reply:k2")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(h.sent_readback(SESSION, sha("Refund approved."))), 1)

    def test_fingerprint_held_by_other_text_raises(self):
        fp = crisp.crisp_fingerprint("reply:k3")
        self.fake.add(SESSION, "operator", "something else", T0 + 2000, fp)
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).send_reply(SESSION, EMAIL, "s", "Refund approved.", "reply:k3")
        self.assertEqual(self.posts(), [])

    def test_wrong_recipient_refused_before_write(self):
        with self.assertRaises(crisp.CrispRefused):
            helpdesk(self.fake).send_reply(SESSION, "someone@else.example", "s", "hi", "reply:k4")
        self.assertEqual(self.posts(), [])

    def test_bad_ticket_refused_before_any_call(self):
        for bad in ("../../website", "session_x", "", "8c842203-7ed8-4e29-a608-7cf78a7d2fcc"):
            with self.assertRaises(crisp.CrispRefused):
                helpdesk(self.fake).send_reply(bad, EMAIL, "s", "hi", "reply:k5")
        self.assertEqual(self.fake.calls, [])

    def test_empty_body_refused(self):
        with self.assertRaises(crisp.CrispRefused):
            helpdesk(self.fake).send_reply(SESSION, EMAIL, "s", "  ", "reply:k6")
        self.assertEqual(self.posts(), [])

    def test_unconfirmed_fingerprint_raises(self):
        h = helpdesk(self.fake)
        meta = _doc("get-metas", "200 OK")
        meta["data"]["email"] = EMAIL
        nf = _doc("get-a-message", "404 Not Found")
        nf["reason"] = "message_not_found"
        self.fake.fail = [(200, meta), (404, nf), (202, _doc("send-text-message", "202 Accepted"))]
        with self.assertRaises(crisp.CrispError):   # docs example fingerprint differs from ours
            h.send_reply(SESSION, EMAIL, "s", "hi", "reply:k7")

    def test_docs_send_refusals_raise(self):
        for status in ("400 Bad Request", "402 Payment Required", "406 Not Acceptable", "409 Conflict"):
            h = helpdesk(self.fake)
            meta = _doc("get-metas", "200 OK")
            meta["data"]["email"] = EMAIL
            nf = _doc("get-a-message", "404 Not Found")
            nf["reason"] = "message_not_found"
            self.fake.fail = [(200, meta), (404, nf), (int(status[:3]), _doc("send-text-message", status))]
            with self.assertRaises(HttpError, msg=status):
                h.send_reply(SESSION, EMAIL, "s", "hi", "reply:" + status)

    def test_message_read_other_404_raises(self):
        """Only Crisp's own 'message_not_found' means 'not there yet'; a missing session is an error."""
        h = helpdesk(self.fake)
        with self.assertRaises(HttpError):
            h.api.message("session_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, 5)


@mock.patch.dict(os.environ, ENV)
class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCrisp()
        self.fake.conv(SESSION, EMAIL)
        self.fake.add(SESSION, "user", "Can I get a refund?", T0 + 1000, 1)

    def test_exact_sha_only(self):
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2000, 2)
        self.fake.add(SESSION, "operator", "Refund approved. ", T0 + 2100, 3)
        rows = helpdesk(self.fake).sent_readback(SESSION, sha("Refund approved."))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.status, r.recipient, r.sender, r.message_id), ("Sent", EMAIL, SUPPORT, f"crisp:{SESSION}:2"))

    def test_two_copies_are_two_rows(self):
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2000, 2)
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2100, 3)
        self.assertEqual(len(helpdesk(self.fake).sent_readback(SESSION, sha("Refund approved."))), 2)

    def test_unstamped_is_not_sent(self):
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2000, 2, stamped=False)
        rows = helpdesk(self.fake).sent_readback(SESSION, sha("Refund approved."))
        self.assertEqual([r.status for r in rows], ["Not Sent"])

    def test_customer_text_never_counts_as_our_reply(self):
        self.fake.add(SESSION, "user", "Refund approved.", T0 + 2000, 2)
        self.assertEqual(helpdesk(self.fake).sent_readback(SESSION, sha("Refund approved.")), [])

    def test_note_never_counts(self):
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2000, 2, typ="note")
        self.assertEqual(helpdesk(self.fake).sent_readback(SESSION, sha("Refund approved.")), [])

    def test_mailbox_receipt(self):
        self.fake.add(SESSION, "operator", "Refund approved.", T0 + 2000, 2)
        mb = crisp.CrispMailbox(section(), transport=self.fake, sleep=lambda s: None)
        self.assertEqual(mb.sent_copies(f"crisp:{SESSION}:2"), 1)
        self.assertEqual(mb.sent_copies(f"crisp:{SESSION}:3"), 0)
        with self.assertRaises(crisp.CrispError):
            mb.sent_copies(f"crisp:{SESSION}:1")         # the customer's message is not our copy
        with self.assertRaises(crisp.CrispError):
            mb.sent_copies("<abc@mail.example>")


@mock.patch.dict(os.environ, ENV)
class TimesTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCrisp()
        self.fake.conv(SESSION, EMAIL)
        self.fake.add(SESSION, "user", "first", T0 + 1000, 1)
        self.fake.add(SESSION, "operator", "answer", T0 + 2000, 2)

    def test_last_sent_and_inbound_on_ticket(self):
        h = helpdesk(self.fake)
        self.assertEqual(h.last_sent_at(SESSION), crisp.ms_to_iso(T0 + 2000))
        self.assertEqual(h.last_inbound_at(SESSION, EMAIL), crisp.ms_to_iso(T0 + 1000))

    def test_last_inbound_includes_other_sessions_by_email(self):
        self.fake.conv(OTHER, EMAIL)
        self.fake.add(OTHER, "user", "new thread", T0 + 5000, 9)
        h = helpdesk(self.fake)
        self.assertEqual(h.last_inbound_at(SESSION, EMAIL), crisp.ms_to_iso(T0 + 5000))
        people = [c for c in self.fake.calls if "/people/conversations/" in c["path"]]
        self.assertTrue(people and urllib.parse.unquote(people[0]["path"]).endswith(f"/{EMAIL}/list/1"))

    def test_unknown_person_reads_ticket_only(self):
        h = helpdesk(self.fake)
        self.assertEqual(h.last_inbound_at(SESSION, "nobody@x.example"), crisp.ms_to_iso(T0 + 1000))

    def test_people_list_error_raises(self):
        h = helpdesk(self.fake)
        own = {"error": False, "reason": "listed", "data": self.fake.convs[SESSION]["messages"]}
        self.fake.fail = [(200, own), (403, _doc("list-people-conversations", "403 Forbidden"))]
        with self.assertRaises(HttpError):
            h.last_inbound_at(SESSION, EMAIL)

    def test_message_from_another_conversation_raises(self):
        self.fake.fail = [(200, _doc("get-messages", "200 OK"))]   # docs example: a different session id
        with self.assertRaises(crisp.CrispError):
            helpdesk(self.fake).last_sent_at(SESSION)

    def test_people_page_cap_raises(self):
        h = helpdesk(self.fake)
        with mock.patch.object(h.api, "call", side_effect=lambda m, p, **k: [SESSION] if "/people/" in p
                               else self.fake_call(h, m, p, **k)):
            with self.assertRaises(crisp.CrispError):
                h._people_sessions(EMAIL)

    def fake_call(self, h, m, p, **k):
        raise AssertionError("not reached")

    def test_no_messages_is_none(self):
        self.fake.conv(OTHER, "other@x.example")
        h = helpdesk(self.fake)
        self.assertIsNone(h.last_sent_at(OTHER))


@mock.patch.dict(os.environ, ENV)
class RateLimitTests(unittest.TestCase):
    def test_429_and_420_retried_bounded(self):
        fake = FakeCrisp()
        waits = []
        h = crisp.CrispHelpdesk(section(), SUPPORT, transport=fake, sleep=waits.append)
        fake.fail = [(429, {"error": True, "reason": "rate_limited", "data": {}}),
                     (420, {"error": True, "reason": "rate_limited", "data": {}})]
        h.api.website_info()
        self.assertEqual(len(waits), 2)

    def test_retries_stop(self):
        fake = FakeCrisp()
        h = crisp.CrispHelpdesk(section(), SUPPORT, transport=fake, sleep=lambda s: None)
        fake.fail = [(420, {"error": True, "data": {}})] * 10
        with self.assertRaises(HttpError):
            h.api.website_info()
        self.assertLessEqual(len(fake.calls), 4)

    def test_daily_quota_raises_at_once(self):
        fake = FakeCrisp()
        waits = []
        h = crisp.CrispHelpdesk(section(), SUPPORT, transport=fake, sleep=waits.append)
        fake.fail = [(423, _doc("list-conversations", "423 Locked"))]
        with self.assertRaises(HttpError) as cm:
            h.fetch_inbound(None)
        self.assertEqual((cm.exception.status, waits), (423, []))


@mock.patch.dict(os.environ, ENV)
class SenderJobTests(unittest.TestCase):
    """The real sender job over the Crisp adapter: one send, proven, never repeated."""

    def test_sender_verifies_and_never_resends(self):
        from sayso import cases, runtime, votes
        from sayso.config import parse
        from sayso.jobs import sender
        fake = FakeCrisp()
        fake.conv(SESSION, EMAIL)
        fake.add(SESSION, "user", "Can I get a refund?", T0 + 1000, 1)
        with tempfile.TemporaryDirectory() as d:
            cfg = parse({"product": {"slug": "acme", "support_address": SUPPORT},
                         "operators": {"1": "Ann"}, "state": {"dir": d}}, Path(d) / "x.toml")
            ctx = runtime.build(cfg)
            ctx.helpdesk = crisp.CrispHelpdesk(section(), SUPPORT, transport=fake, sleep=lambda s: None,
                                               now=lambda: (T0 + 3_600_000) / 1000)
            ctx.mailbox = crisp.CrispMailbox(section(), transport=fake, sleep=lambda s: None)
            msgs, _ = ctx.helpdesk.fetch_inbound(None)
            key, rec, _ = cases.ensure_case(ctx, customer=EMAIL, kind=cases.CORRESPONDENCE, title="Refund",
                                            first_message=cases.email_card(msgs[0]), ticket=SESSION)
            body = "Your refund is on its way."
            draft = Path(d) / "draft.txt"
            draft.write_text(body, encoding="utf-8")
            from sayso import stages
            cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
            cases.move(ctx, rec["card_id"], stages.AWAITING)
            vkey = "reply:" + key + ":1"
            votes.open_vote(ctx, key=vkey, kind="reply", case_key=key, card_id=rec["card_id"],
                            subject="refund case", question="send?", identity={"recipient": EMAIL},
                            material={"sha256": sha(body)}, spec={"path": str(draft), "sha256": sha(body)})
            votes.set_status(ctx, vkey, votes.APPROVED)
            out = sender.send_one(ctx, votes.load(ctx)[vkey], sleep=lambda s: None)
            self.assertEqual(out, sender.VERIFIED)
            posts = [c for c in fake.calls if c["method"] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertNotEqual(sender.send_one(ctx, votes.load(ctx)[vkey], sleep=lambda s: None), sender.VERIFIED)
            # Worst case: the local attempt record is lost and the card is back in Awaiting approval.
            # The adapter's read-before-write must still stop a second message to the customer.
            ctx.paths.attempts.unlink()
            votes.set_status(ctx, vkey, votes.APPROVED)
            cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
            cases.move(ctx, rec["card_id"], stages.AWAITING)
            sender.send_one(ctx, votes.load(ctx)[vkey], sleep=lambda s: None)
            self.assertEqual(len([c for c in fake.calls if c["method"] == "POST"]), 1)
            self.assertEqual(len(ctx.helpdesk.sent_readback(SESSION, sha(body))), 1)


if __name__ == "__main__":
    unittest.main()
