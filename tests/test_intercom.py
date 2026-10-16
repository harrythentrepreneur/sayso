"""Intercom adapter against a RECORDING transport. No network, no account.

Fixtures are Intercom's own examples from the official OpenAPI description
(github.com/intercom/Intercom-OpenAPI, descriptions/2.14/api.intercom.io.yaml, commit efbf677):
search `successful`, retrieve `conversation found`, reply `User reply`, `/me`, trimmed to the
fields the adapter reads. Where a test needs a customer part or a second page, it copies the
example's own field shapes (ids, unix times, author objects).

A socket guard is installed for this whole module: any connection that is not to localhost
fails the test, and `SocketGuardTests` proves the guard bites with one seeded outside call.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import unittest
from unittest import mock

from sayso.adapters import intercom, reference
from sayso.adapters.http import HttpError
from sayso.config import ConfigError, Section, parse
from pathlib import Path

# ---------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect
_real_create = socket.create_connection


class OutsideCall(AssertionError):
    pass


def _local(addr) -> bool:
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    return host in ("127.0.0.1", "::1", "localhost") or isinstance(addr, (str, bytes))


def _guard_connect(self, addr):
    if not _local(addr):
        raise OutsideCall(f"test tried to reach {addr!r}")
    return _real_connect(self, addr)


def _guard_create(addr, *a, **k):
    if not _local(addr):
        raise OutsideCall(f"test tried to reach {addr!r}")
    return _real_create(addr, *a, **k)


def setUpModule():
    socket.socket.connect = _guard_connect
    socket.create_connection = _guard_create


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


# ---------------------------------------------------------------- fixtures (Intercom 2.14 examples)
NOW = 1734540000
ADMIN = "991267694"                 # reply example's admin id
CONTACT = "6762f1571bb69f9f2193bbbb"  # reply example's contact id
SUPPORT = "help@acme.test"

SEARCH_EXAMPLE = {  # POST /conversations/search -> 200 `successful`
    "type": "conversation.list",
    "pages": {"type": "pages", "page": 1, "per_page": 5, "total_pages": 1},
    "total_count": 1,
    "conversations": [{"type": "conversation", "id": "524", "created_at": 1734537559, "updated_at": 1734537561,
                       "waiting_since": 1734537561, "open": True, "state": "open"}],
}

CONVERSATION = {  # GET /conversations/{id} -> `conversation found` / reply `User reply`, customer-opened
    "type": "conversation", "id": "524", "created_at": 1734537559, "updated_at": 1734537561,
    "title": None,
    "source": {"type": "conversation", "id": "403918349", "delivered_as": "customer_initiated", "subject": "",
               "body": "Hi, my invoice is wrong.",
               "author": {"type": "user", "id": CONTACT, "name": "Joe Bloggs", "email": "joe@bloggs.com"},
               "attachments": [], "url": None, "redacted": False},
    "contacts": {"type": "contact.list", "contacts": [{"type": "contact", "id": CONTACT, "external_id": "70"}]},
    "conversation_parts": {"type": "conversation_part.list", "total_count": 1, "conversation_parts": [
        {"type": "conversation_part", "id": "132", "part_type": "comment", "body": "Thanks again :)",
         "created_at": 1734537561, "updated_at": 1734537561, "notified_at": 1734537561,
         "author": {"id": CONTACT, "type": "user", "name": "Joe Bloggs", "email": "joe@bloggs.com"},
         "attachments": [], "external_id": None, "redacted": False}]},
}

CONTACT_EXAMPLE = {"type": "contact", "id": CONTACT, "role": "user", "email": "joe@bloggs.com", "name": "Joe Bloggs"}

ME_EXAMPLE = {"type": "admin", "id": ADMIN, "name": "Ciaran212 Lee", "email": "admin212@email.com",
              "email_verified": True, "app": {"type": "app", "id_code": "xyz789", "name": "ACME", "region": "US",
                                              "timezone": "America/Los_Angeles"}}


def admin_part(pid, body, *, admin=ADMIN, part_type="comment", t=1734537600, redacted=False):
    return {"type": "conversation_part", "id": pid, "part_type": part_type, "body": body, "created_at": t,
            "updated_at": t, "notified_at": t, "redacted": redacted,
            "author": {"id": admin, "type": "admin", "name": "Ciaran212 Lee", "email": "admin212@email.com"}}


def with_parts(*parts, conv=None):
    c = copy.deepcopy(conv or CONVERSATION)
    c["conversation_parts"]["conversation_parts"] += list(parts)
    return c


class Fake:
    """Routes (method, path) to queued answers; records every request."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path.split("?", 1)[0]
        self.calls.append({"method": method, "url": url, "path": path, "headers": headers,
                           "body": json.loads(body) if body else None})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        status, payload = item if isinstance(item, tuple) else (200, item)
        headers_out = {}
        if isinstance(payload, dict) and "__headers__" in payload:
            payload = dict(payload)
            headers_out = payload.pop("__headers__")
        return status, headers_out, json.dumps(payload).encode()

    def writes(self):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("/reply")]


def desk(routes, **kw):
    fake = Fake(routes)
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("sleep", lambda s: None)
    h = intercom.IntercomHelpdesk(token="tok_test", admin_id=ADMIN, support_address=SUPPORT, transport=fake, **kw)
    return h, fake


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- tests
class SocketGuardTests(unittest.TestCase):
    def test_seeded_outside_call_is_blocked(self):
        """The one deliberate outside call: the real urllib transport to Intercom must be stopped."""
        with self.assertRaises(OutsideCall):
            intercom.IntercomHelpdesk(token="t", admin_id="1", support_address=SUPPORT).http.request("GET", "/me")


class HeaderAndConfigTests(unittest.TestCase):
    def test_every_request_pins_the_version_and_bearer(self):
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION})
        h.last_sent_at("524")
        hdr = fake.calls[0]["headers"]
        self.assertEqual(hdr["Intercom-Version"], "2.14")
        self.assertEqual(hdr["Authorization"], "Bearer tok_test")
        self.assertTrue(fake.calls[0]["url"].startswith("https://api.intercom.io/"))

    def test_region_picks_the_documented_host_only(self):
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION}, region="eu")
        h.last_sent_at("524")
        self.assertTrue(fake.calls[0]["url"].startswith("https://api.eu.intercom.io/"))
        with self.assertRaises(ConfigError):
            desk({}, region="https://evil.example")

    def test_admin_id_must_be_numeric_and_token_present(self):
        with self.assertRaises(ConfigError):
            intercom.IntercomHelpdesk(token="t", admin_id="me", support_address=SUPPORT)
        with self.assertRaises(ConfigError):
            intercom.IntercomHelpdesk(token="", admin_id="1", support_address=SUPPORT)

    @mock.patch.dict(os.environ, {"IC_TOKEN": "tok_env"})
    def test_build_reads_the_secret_from_the_env_var_named_in_settings(self):
        raw = {"product": {"slug": "acme", "mode": "live", "support_address": SUPPORT},
               "operators": {"1": "Ann"}, "state": {"dir": "/tmp/x"},
               "helpdesk": {"adapter": "intercom", "token_env": "IC_TOKEN", "admin_id": ADMIN, "region": "au"},
               "mailbox": {"adapter": "intercom"}}
        cfg = parse(raw, Path("/tmp/sayso.toml"))
        h = reference.build("helpdesk", "intercom", cfg.section("helpdesk"), cfg)
        self.assertIsInstance(h, intercom.IntercomHelpdesk)
        self.assertEqual(h.http.headers["Authorization"], "Bearer tok_env")
        self.assertEqual(h.http.base, "https://api.au.intercom.io")
        box = reference.build("mailbox", "intercom", cfg.section("mailbox"), cfg)
        self.assertIsInstance(box, intercom.IntercomMailbox)

    def test_secret_in_settings_file_refused(self):
        raw = {"product": {"slug": "acme", "mode": "live", "support_address": SUPPORT},
               "operators": {"1": "Ann"}, "state": {"dir": "/tmp/x"},
               "helpdesk": {"adapter": "intercom", "token_env": "dG9rOjBiM...", "admin_id": ADMIN}}
        with self.assertRaises(ConfigError):
            parse(raw, Path("/tmp/sayso.toml"))

    def test_unknown_option_refused(self):
        with self.assertRaises(ConfigError):
            intercom.IntercomHelpdesk.from_section(Section("intercom", {"token_env": "X", "admin_id": "1",
                                                                         "base_url": "https://x"}), SUPPORT)

    def test_mailbox_intercom_needs_helpdesk_intercom(self):
        raw = {"product": {"slug": "acme", "mode": "live", "support_address": SUPPORT},
               "operators": {"1": "Ann"}, "state": {"dir": "/tmp/x"}, "mailbox": {"adapter": "intercom"}}
        cfg = parse(raw, Path("/tmp/sayso.toml"))
        with self.assertRaises(ConfigError):
            reference.build("mailbox", "intercom", cfg.section("mailbox"), cfg)


class FetchInboundTests(unittest.TestCase):
    def test_search_since_cursor_and_customer_parts_become_inbound(self):
        h, fake = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE,
                        ("GET", "/conversations/524"): with_parts(admin_part("140", "We will look."))})
        msgs, cursor = h.fetch_inbound("1734537000")
        q = fake.calls[0]["body"]
        self.assertEqual(q["query"], {"field": "updated_at", "operator": ">", "value": 1734537000})
        self.assertEqual(q["pagination"]["per_page"], 150)
        self.assertEqual(fake.calls[1]["url"].split("?")[1], "display_as=plaintext")
        self.assertEqual([m.message_id for m in msgs], ["intercom:524:source:403918349", "intercom:524:132"])
        self.assertTrue(all(m.sender == "joe@bloggs.com" and m.ticket == "524" for m in msgs))
        self.assertEqual(msgs[0].body, "Hi, my invoice is wrong.")
        self.assertEqual(msgs[1].received_at, "2024-12-18T15:59:21+00:00")
        self.assertEqual(cursor, "1734537561")
        self.assertNotIn("We will look.", [m.body for m in msgs], "an admin part is never inbound")

    def test_first_run_reads_only_the_backfill_window(self):
        h, fake = desk({("POST", "/conversations/search"): dict(SEARCH_EXAMPLE, conversations=[])},
                       backfill_hours=24)
        msgs, cursor = h.fetch_inbound(None)
        self.assertEqual(fake.calls[0]["body"]["query"]["value"], NOW - 86400)
        self.assertEqual((msgs, cursor), ([], str(NOW - 86400)))

    def test_old_parts_behind_the_cursor_are_skipped(self):
        h, _ = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE, ("GET", "/conversations/524"): CONVERSATION})
        msgs, _ = h.fetch_inbound(str(1734537561 + intercom.OVERLAP))
        self.assertEqual([m.message_id for m in msgs], ["intercom:524:132"])

    def test_every_page_is_read_before_the_cursor_moves(self):
        p1 = dict(SEARCH_EXAMPLE, pages={"type": "pages", "page": 1, "per_page": 1, "total_pages": 2,
                                         "next": {"per_page": 1, "starting_after": "WzE3MzQ1Mzc1NjEsNTI0XQ=="}})
        conv2 = copy.deepcopy(CONVERSATION)
        conv2["id"] = "525"
        p2 = dict(SEARCH_EXAMPLE, conversations=[{"type": "conversation", "id": "525", "updated_at": 1734537999}])
        h, fake = desk({("POST", "/conversations/search"): [p1, p2],
                        ("GET", "/conversations/524"): CONVERSATION, ("GET", "/conversations/525"): conv2})
        msgs, cursor = h.fetch_inbound("1734537000")
        searches = [c["body"] for c in fake.calls if c["path"] == "/conversations/search"]
        self.assertEqual(searches[1]["pagination"]["starting_after"], "WzE3MzQ1Mzc1NjEsNTI0XQ==")
        self.assertEqual(cursor, "1734537999")
        self.assertEqual({m.ticket for m in msgs}, {"524", "525"})

    def test_contact_without_email_gets_a_stable_non_routable_address(self):
        conv = copy.deepcopy(CONVERSATION)
        conv["conversation_parts"]["conversation_parts"][0]["author"]["email"] = None
        conv["source"]["author"]["email"] = ""
        h, _ = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE, ("GET", "/conversations/524"): conv})
        msgs, _ = h.fetch_inbound("1734537000")
        self.assertEqual({m.sender for m in msgs}, {f"{CONTACT}@contacts.intercom.invalid"})

    def test_not_understood_answers_raise(self):
        bad_search = {"type": "conversation.list"}
        h, _ = desk({("POST", "/conversations/search"): bad_search})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("1")
        wrong = copy.deepcopy(CONVERSATION)
        wrong["id"] = "999"
        h, _ = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE, ("GET", "/conversations/524"): wrong})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("1")
        iso_time = with_parts()
        iso_time["conversation_parts"]["conversation_parts"][0]["created_at"] = "2024-12-18T16:12:41Z"
        h, _ = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE, ("GET", "/conversations/524"): iso_time})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("1")
        h, _ = desk({})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("2024-12-18")

    def test_unknown_customer_part_type_raises(self):
        odd = with_parts()
        odd["conversation_parts"]["conversation_parts"][0]["part_type"] = "brand_new_kind"
        h, _ = desk({("POST", "/conversations/search"): SEARCH_EXAMPLE, ("GET", "/conversations/524"): odd})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("1")

    def test_runaway_pagination_raises_instead_of_moving_the_cursor(self):
        loop = dict(SEARCH_EXAMPLE, pages={"next": {"starting_after": "same"}})
        h, _ = desk({("POST", "/conversations/search"): loop, ("GET", "/conversations/524"): CONVERSATION})
        with self.assertRaises(intercom.IntercomError):
            h.fetch_inbound("1")


class SendReplyTests(unittest.TestCase):
    BODY = "Hi Joe,\n\nWe fixed the invoice.\nThanks"

    def answer_with(self, body_html, pid="200"):
        return with_parts(admin_part(pid, intercom.plain(body_html)))

    def test_reply_is_an_admin_comment_with_marker(self):
        mark = intercom.marker("vote:k1", sha(self.BODY))
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION, ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE,
                        ("POST", "/conversations/524/reply"): self.answer_with(f"We fixed it {mark}")})
        mid = h.send_reply("524", "Joe@Bloggs.com", "Re: invoice", self.BODY, "vote:k1")
        self.assertEqual(mid, "intercom:524:200")
        sent = fake.writes()[0]["body"]
        self.assertEqual({k: sent[k] for k in ("message_type", "type", "admin_id")},
                         {"message_type": "comment", "type": "admin", "admin_id": ADMIN})
        self.assertEqual(sent["body"], "<p>Hi Joe,</p><p>We fixed the invoice.<br>Thanks</p><p>" + mark + "</p>")

    def test_reply_body_is_html_escaped(self):
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION, ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE,
                        ("POST", "/conversations/524/reply"): CONVERSATION})
        h.send_reply("524", "joe@bloggs.com", "s", "<script>x</script> & co", "k")
        self.assertIn("&lt;script&gt;x&lt;/script&gt; &amp; co", fake.writes()[0]["body"]["body"])

    def test_retry_finds_the_earlier_reply_and_does_not_post_again(self):
        mark = intercom.marker("vote:k1", sha(self.BODY))
        earlier = with_parts(admin_part("200", "We fixed it\n\n" + mark))
        h, fake = desk({("GET", "/conversations/524"): earlier, ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE,
                        ("POST", "/conversations/524/reply"): earlier})
        self.assertEqual(h.send_reply("524", "joe@bloggs.com", "s", self.BODY, "vote:k1"), "intercom:524:200")
        self.assertEqual(fake.writes(), [], "a second POST would message the customer twice")

    def test_wrong_recipient_refused_before_any_write(self):
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION, ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE})
        with self.assertRaises(intercom.IntercomError):
            h.send_reply("524", "someone@else.test", "s", self.BODY, "k")
        self.assertEqual(fake.writes(), [])

    def test_group_conversation_refused(self):
        group = copy.deepcopy(CONVERSATION)
        group["contacts"]["contacts"].append({"type": "contact", "id": "6762f1621bb69f9f2193bbbe"})
        h, fake = desk({("GET", "/conversations/524"): group})
        with self.assertRaises(intercom.IntercomError):
            h.send_reply("524", "joe@bloggs.com", "s", self.BODY, "k")
        self.assertEqual(fake.writes(), [])

    def test_non_numeric_ticket_refused_without_a_request(self):
        h, fake = desk({})
        for bad in ("524/../admins", "last", "", "524?x=1"):
            with self.assertRaises(intercom.IntercomError):
                h.send_reply(bad, "joe@bloggs.com", "s", self.BODY, "k")
        self.assertEqual(fake.calls, [])

    def test_contact_without_email_matches_its_stand_in_address(self):
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION,
                        ("GET", f"/contacts/{CONTACT}"): dict(CONTACT_EXAMPLE, email=None),
                        ("POST", "/conversations/524/reply"): CONVERSATION})
        h.send_reply("524", f"{CONTACT}@contacts.intercom.invalid", "s", self.BODY, "k")
        self.assertEqual(len(fake.writes()), 1)

    def test_api_error_is_raised_not_swallowed(self):
        h, _ = desk({("GET", "/conversations/524"): CONVERSATION, ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE,
                     ("POST", "/conversations/524/reply"): (401, {"type": "error.list", "errors": [
                         {"code": "unauthorized", "message": "Access Token Invalid"}]})})
        with self.assertRaises(HttpError):
            h.send_reply("524", "joe@bloggs.com", "s", self.BODY, "k")


class ReadbackTests(unittest.TestCase):
    BODY = "We fixed the invoice."

    def setUp(self):
        self.mark = intercom.marker("vote:k1", sha(self.BODY))

    def readback(self, *parts, contact=CONTACT_EXAMPLE):
        h, _ = desk({("GET", "/conversations/524"): with_parts(*parts), ("GET", f"/contacts/{CONTACT}"): contact})
        return h.sent_readback("524", sha(self.BODY))

    def test_our_admin_comment_is_sent_to_the_customer(self):
        rows = self.readback(admin_part("200", self.BODY + "\n\n" + self.mark))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].status, rows[0].sender, rows[0].recipient, rows[0].message_id),
                         ("Sent", SUPPORT, "joe@bloggs.com", "intercom:524:200"))

    def test_a_note_never_counts_as_sent(self):
        rows = self.readback(admin_part("200", self.BODY + " " + self.mark, part_type="note"))
        self.assertEqual([r.status for r in rows], ["Not Sent"])

    def test_another_admin_is_not_our_sender(self):
        rows = self.readback(admin_part("200", self.BODY + " " + self.mark, admin="5"))
        self.assertEqual([(r.status, r.sender) for r in rows], [("Not Sent", "admin212@email.com")])

    def test_redacted_part_is_not_sent(self):
        rows = self.readback(admin_part("200", self.BODY + " " + self.mark, redacted=True))
        self.assertEqual([r.status for r in rows], ["Not Sent"])

    def test_two_copies_are_both_reported_so_the_sender_refuses(self):
        rows = self.readback(admin_part("200", self.mark), admin_part("201", self.mark))
        self.assertEqual(len(rows), 2)

    def test_other_body_is_ignored(self):
        other = intercom.marker("vote:k1", sha("something else"))
        self.assertEqual(self.readback(admin_part("200", other)), [])

    def test_mailbox_rereads_the_part_by_id(self):
        conv = with_parts(admin_part("200", self.mark), admin_part("201", "x", part_type="note"))
        h, fake = desk({("GET", "/conversations/524"): conv})
        box = intercom.IntercomMailbox(h)
        self.assertEqual(box.sent_copies("intercom:524:200"), 1)
        self.assertEqual(box.sent_copies("intercom:524:201"), 0, "a note is not a sent copy")
        self.assertEqual(box.sent_copies("intercom:524:999"), 0)
        with self.assertRaises(intercom.IntercomError):
            box.sent_copies("<abc@mail>")


class TimesTests(unittest.TestCase):
    def test_last_sent_at_is_the_newest_admin_comment(self):
        conv = with_parts(admin_part("200", "a", t=1734537600), admin_part("201", "n", part_type="note", t=1734539999))
        h, _ = desk({("GET", "/conversations/524"): conv})
        self.assertEqual(h.last_sent_at("524"), "2024-12-18T16:00:00+00:00")

    def test_last_sent_at_none_without_admin_reply(self):
        h, _ = desk({("GET", "/conversations/524"): CONVERSATION})
        self.assertIsNone(h.last_sent_at("524"))

    def test_last_inbound_at_includes_another_conversation_of_the_same_customer(self):
        other = copy.deepcopy(CONVERSATION)
        other["id"] = "777"
        other["conversation_parts"]["conversation_parts"][0]["created_at"] = 1734539000
        h, fake = desk({("GET", "/conversations/524"): CONVERSATION, ("GET", "/conversations/777"): other,
                        ("POST", "/contacts/search"): {"type": "list", "data": [CONTACT_EXAMPLE],
                                                       "total_count": 1, "pages": {"type": "pages", "page": 1}},
                        ("POST", "/conversations/search"): dict(SEARCH_EXAMPLE, conversations=[
                            {"type": "conversation", "id": "524"}, {"type": "conversation", "id": "777"}])})
        self.assertEqual(h.last_inbound_at("524", "Joe@Bloggs.com"), "2024-12-18T16:23:20+00:00")
        searches = [c["body"]["query"] for c in fake.calls if c["method"] == "POST"]
        self.assertEqual(searches[0], {"field": "email", "operator": "=", "value": "joe@bloggs.com"})
        self.assertEqual(searches[1], {"field": "contact_ids", "operator": "IN", "value": [CONTACT]})

    def test_last_inbound_at_this_ticket_only_when_customer_has_no_other(self):
        h, _ = desk({("GET", "/conversations/524"): CONVERSATION,
                     ("POST", "/contacts/search"): {"type": "list", "data": [], "pages": {}}})
        self.assertEqual(h.last_inbound_at("524", "joe@bloggs.com"), "2024-12-18T15:59:21+00:00")

    def test_unreadable_search_raises_never_none(self):
        h, _ = desk({("GET", "/conversations/524"): CONVERSATION, ("POST", "/contacts/search"): (500, {"e": 1})})
        with self.assertRaises(HttpError):
            h.last_inbound_at("524", "joe@bloggs.com")


class RateLimitTests(unittest.TestCase):
    def test_429_waits_until_the_reset_header_then_retries(self):
        waits = []
        h, fake = desk({("GET", "/conversations/524"): [
            (429, {"type": "error.list", "__headers__": {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0",
                                                          "X-RateLimit-Reset": str(NOW + 4)}}),
            (200, CONVERSATION)]}, sleep=waits.append)
        self.assertIsNone(h.last_sent_at("524"))
        self.assertEqual(waits, [4.0])
        self.assertEqual(len(fake.calls), 2)

    def test_wait_is_bounded(self):
        waits = []
        h, _ = desk({("GET", "/conversations/524"): [
            (429, {"__headers__": {"X-RateLimit-Reset": str(NOW + 3600)}}), (200, CONVERSATION)]}, sleep=waits.append)
        h.last_sent_at("524")
        self.assertEqual(waits, [10.0])

    def test_persistent_429_raises(self):
        h, _ = desk({("GET", "/conversations/524"): (429, {"__headers__": {"X-RateLimit-Reset": "x"}})})
        with self.assertRaises(HttpError):
            h.last_sent_at("524")


class LoopIntegrationTests(unittest.TestCase):
    """The core sender's own receipt rule, run against the Intercom adapter."""

    def test_sender_receipt_rule_accepts_one_admin_comment(self):
        from types import SimpleNamespace
        from sayso.jobs import sender
        body = "We fixed the invoice."
        mark = intercom.marker("vote:k1", sha(body))
        h, _ = desk({("GET", "/conversations/524"): with_parts(admin_part("200", body + "\n\n" + mark)),
                     ("GET", f"/contacts/{CONTACT}"): CONTACT_EXAMPLE})
        ctx = SimpleNamespace(helpdesk=h, mailbox=intercom.IntercomMailbox(h),
                              config=SimpleNamespace(support_address=SUPPORT))
        self.assertEqual(sender.check_receipt(ctx, "524", "joe@bloggs.com", sha(body)),
                         {"message_id": "intercom:524:200", "status": sender.VERIFIED})
        self.assertIsNone(sender.check_receipt(ctx, "524", "other@x.test", sha(body)))


if __name__ == "__main__":
    unittest.main()
