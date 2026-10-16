"""Zendesk helpdesk adapter against a fake Zendesk built from the vendor docs' own example JSON.

No network, no account. A socket guard fails any connection that is not localhost.
Fixture sources (Zendesk developer docs, fetched 16 Oct 2026):
  ticket_events + comment_events  api-reference/ticketing/ticket-management/incremental_exports/
  ticket / show_many               api-reference/ticketing/tickets/tickets/
  comments                         api-reference/ticketing/tickets/ticket_comments/
  audits (metadata.custom, Notification event)  api-reference/ticketing/tickets/ticket_audits/
  users                            api-reference/ticketing/users/users/
"""
from __future__ import annotations

import base64
import copy
import json
import os
import socket
import unittest
import urllib.parse
from unittest import mock

from sayso.adapters import zendesk
from sayso.adapters.http import HttpError
from sayso.config import ConfigError, Section

# ----------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
        raise AssertionError(f"test tried to reach the network: {host!r}")
    return _real_connect(self, address)


def setUpModule():
    socket.socket.connect = _guarded_connect


def tearDownModule():
    socket.socket.connect = _real_connect


class SocketGuardTests(unittest.TestCase):
    def test_guard_refuses_an_outside_connection(self):
        """Seeded outside call: the real urllib transport aimed at Zendesk must be stopped by the guard."""
        from sayso.adapters.http import urllib_transport
        with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                             ("93.184.216.34", 443))]):
            with self.assertRaises(AssertionError) as cm:
                urllib_transport("GET", "https://example.zendesk.com/api/v2/users/me", {}, None)
        self.assertIn("network", str(cm.exception))


# ----------------------------------------------------------------------------- fixtures (docs shapes)
NOW = 1_760_000_000            # fixed clock
REQUESTER, AGENT, OTHER = 20978392, 235323, 35334


def users():
    return {
        REQUESTER: {"id": REQUESTER, "name": "Johnny End User", "email": "johnny@example.com", "role": "end-user",
                    "created_at": "2009-07-20T22:55:29Z"},
        AGENT: {"id": AGENT, "name": "Agent", "email": "agent@acme.example", "role": "agent"},
        OTHER: {"id": OTHER, "name": "Other", "email": "other@example.com", "role": "end-user"},
    }


def ticket(tid=35436, **kw):
    t = {"id": tid, "subject": "Help, my printer is on fire!", "raw_subject": "{{dc.printer_on_fire}}",
         "description": "The fire is very colorful.", "status": "open", "requester_id": REQUESTER,
         "submitter_id": REQUESTER, "assignee_id": AGENT, "recipient": "support@acme.example",
         "collaborator_ids": [], "email_cc_ids": [], "follower_ids": [], "created_at": "2009-07-20T22:55:29Z",
         "updated_at": "2011-05-05T10:38:52Z", "url": f"https://acme.zendesk.com/api/v2/tickets/{tid}",
         "via": {"channel": "web"}}
    t.update(kw)
    return t


def comment_event(cid, author, body, public=True, at="2025-10-09T08:00:00Z"):
    return {"id": cid, "via": {"channel": "api", "source": {"from": {}, "to": {}, "rel": None}},
            "via_reference_id": None, "type": "Comment", "author_id": author, "body": body,
            "html_body": f"<div class=\"zd-comment\"><p dir=\"auto\">{body}</p></div>", "public": public,
            "attachments": [], "audit_id": cid + 1, "created_at": at, "event_type": "Comment"}


class FakeZendesk:
    """Routes (method, path) to the docs' response shapes, records every call and applies writes."""

    def __init__(self):
        self.calls = []
        self.users = users()
        self.tickets = {35436: ticket()}
        self.comments = {35436: [
            {"id": 1274, "type": "Comment", "author_id": REQUESTER, "body": "Thanks for your help!",
             "plain_body": "Thanks for your help!", "public": True, "created_at": "2025-10-09T08:00:00Z",
             "audit_id": 432567, "attachments": []}]}
        self.audits = {35436: [{"id": 432567, "ticket_id": 35436, "author_id": REQUESTER,
                                "created_at": "2025-10-09T08:00:00Z", "metadata": {"custom": {}, "system": {}},
                                "events": [{"id": 1274, "type": "Comment", "body": "Thanks for your help!",
                                            "public": True, "attachments": []}], "via": {"channel": "web"}}]}
        self.events_pages = [{"count": 1, "end_of_stream": True, "end_time": NOW - 100, "ticket_events": [
            {"id": 926256957613, "ticket_id": 35436, "created_at": "2025-10-09T08:00:00Z", "updater_id": REQUESTER,
             "event_type": "Audit", "child_events": [comment_event(1274, REQUESTER, "Thanks for your help!")]}]}]
        self.requested = {REQUESTER: []}
        self.next_id = 5000
        self.notify = True            # Zendesk's default trigger emails the requester
        self.store_body = None        # override what Zendesk stores (to prove the text check)
        self.fail_put_after_write = False
        self.ratelimit = []           # queued Retry-After values for the next calls

    def __call__(self, method, url, headers, body):
        p = urllib.parse.urlsplit(url)
        assert p.scheme == "https" and p.hostname == "acme.zendesk.com", url
        q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        data = json.loads(body) if body else None
        self.calls.append({"method": method, "path": p.path, "query": q, "headers": headers, "body": data})
        if self.ratelimit:
            return 429, {"Retry-After": str(self.ratelimit.pop(0))}, b'{"error":"APIRateLimitExceeded"}'
        status, out = self.route(method, p.path, q, data)
        return status, {}, json.dumps(out).encode()

    def route(self, method, path, q, data):
        parts = path.strip("/").split("/")[2:]   # after api/v2
        if parts == ["users", "me"]:
            return 200, {"user": self.users[AGENT] | {"role": "admin"}}
        if parts == ["recipient_addresses"]:
            return 200, {"recipient_addresses": [{"email": "support@acme.example", "default": True}]}
        if parts == ["incremental", "ticket_events"]:
            return 200, self.events_pages.pop(0)
        if parts == ["users", "show_many"]:
            ids = [int(i) for i in q["ids"].split(",")]
            return 200, {"users": [self.users[i] for i in ids if i in self.users]}
        if parts == ["users", "search"]:
            email = q["query"].split(":", 1)[1]
            return 200, {"users": [u for u in self.users.values() if u["email"] == email]}
        if len(parts) == 4 and parts[0] == "users" and parts[2:] == ["tickets", "requested"]:
            ids = self.requested.get(int(parts[1]), [])
            return 200, {"tickets": [self.tickets[i] for i in ids]}
        if parts == ["tickets", "show_many"]:
            return 200, {"tickets": [self.tickets[int(i)] for i in q["ids"].split(",") if int(i) in self.tickets]}
        tid = int(parts[1])
        if len(parts) == 2 and method == "GET":
            return 200, {"ticket": self.tickets[tid]}
        if len(parts) == 2 and method == "PUT":
            return self.update(tid, data["ticket"])
        if parts[2:] == ["comments"]:
            rows = sorted(self.comments[tid], key=lambda c: c["created_at"], reverse=q.get("sort") == "-created_at")
            return 200, {"comments": rows, "users": [self.users[c["author_id"]] for c in rows],
                         "meta": {"has_more": False, "after_cursor": None}}
        if parts[2:] == ["audits"]:
            return 200, {"audits": self.audits[tid], "meta": {"has_more": False}}
        if len(parts) == 4 and parts[2] == "audits":
            return 200, {"audit": next(a for a in self.audits[tid] if a["id"] == int(parts[3]))}
        raise AssertionError(f"unexpected call {method} {path}")

    def update(self, tid, t):
        self.next_id += 10
        cid, aid = self.next_id, self.next_id + 1
        stored_html = t["comment"]["html_body"]
        stored = self.store_body if self.store_body is not None else \
            " ".join(stored_html.replace("<br>", "\n").replace("</p><p>", "\n\n").replace("<p>", "")
                     .replace("</p>", "").split())
        import html as _h
        stored = _h.unescape(stored)
        self.comments[tid].append({"id": cid, "type": "Comment", "author_id": AGENT, "body": stored,
                                   "plain_body": stored, "html_body": stored_html,
                                   "public": t["comment"]["public"], "created_at": "2025-10-10T09:00:00Z",
                                   "audit_id": aid, "attachments": []})
        events = [{"id": cid, "type": "Comment", "body": stored, "public": t["comment"]["public"], "attachments": []}]
        if self.notify:
            events.append({"id": cid + 2, "type": "Notification", "subject": "Your ticket has been updated",
                           "body": "Ticket #35436 has been updated", "recipients": [REQUESTER],
                           "via": {"channel": "system", "source": {"type": "rule", "id": 61}}})
        audit = {"id": aid, "ticket_id": tid, "author_id": AGENT, "created_at": "2025-10-10T09:00:00Z",
                 "metadata": {"custom": t.get("metadata", {}), "system": {}}, "events": events,
                 "via": {"channel": "api"}}
        self.audits[tid].append(audit)
        if self.fail_put_after_write:
            return 504, {"error": "timeout"}
        return 200, {"ticket": self.tickets[tid], "audit": audit}


ENV = {"ZD_TOKEN": "6wiIBWbGkBMo1mRDMuVwkw1EPsNkeUj95PIz2akv"}
OPTS = {"subdomain": "acme", "email": "agent@acme.example", "token_env": "ZD_TOKEN"}


class ZendeskTestBase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.zd = FakeZendesk()
        self.waits = []
        self.h = zendesk.ZendeskHelpdesk.from_section(Section("zendesk", dict(OPTS)), "support@acme.example",
                                                      transport=self.zd, sleep=self.waits.append, now=lambda: NOW)


class AuthTests(ZendeskTestBase):
    def test_api_token_is_basic_email_slash_token(self):
        self.h.check()
        auth = self.zd.calls[0]["headers"]["Authorization"]
        self.assertEqual(base64.b64decode(auth.split()[1]).decode(),
                         "agent@acme.example/token:" + ENV["ZD_TOKEN"])

    def test_oauth_is_bearer(self):
        self.assertEqual(zendesk.authorization("oauth", "abc"), "Bearer abc")

    def test_token_needs_email(self):
        with self.assertRaises(ConfigError):
            zendesk.authorization("token", "abc", "")

    def test_bad_subdomain_refused(self):
        for bad in ("acme.evil.com", "", "-x", "a/b", "acme.zendesk.com", "x" * 70, "ACME@", "127.0.0.1"):
            with self.assertRaises(ConfigError, msg=bad):
                zendesk.check_subdomain(bad)
        self.assertEqual(zendesk.base_url("Acme"), "https://acme.zendesk.com")

    def test_missing_secret_refused(self):
        with mock.patch.dict(os.environ, {"ZD_TOKEN": ""}):
            with self.assertRaises(ConfigError):
                zendesk.ZendeskHelpdesk.from_section(Section("zendesk", dict(OPTS)), "s@acme.example")

    def test_check_refuses_end_user_token(self):
        self.zd.route = lambda *a: (200, {"user": users()[REQUESTER]})
        with self.assertRaises(zendesk.ZendeskRefused):
            self.h.check()

    def test_check_is_read_only(self):
        info = self.h.check()
        self.assertEqual((info["role"], info["can_export"]), ("admin", True))
        self.assertTrue(all(c["method"] == "GET" for c in self.zd.calls))


class InboundTests(ZendeskTestBase):
    def test_first_run_reads_one_day_back_and_maps_docs_example(self):
        msgs, cursor = self.h.fetch_inbound(None)
        call = self.zd.calls[0]
        self.assertEqual(call["path"], "/api/v2/incremental/ticket_events")
        self.assertEqual(call["query"], {"start_time": str(NOW - 24 * 3600), "include": "comment_events"})
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual((m.message_id, m.ticket, m.sender, m.subject, m.body),
                         ("zendesk:35436:1274", "35436", "johnny@example.com", "Help, my printer is on fire!",
                          "Thanks for your help!"))
        self.assertEqual(m.received_at, "2025-10-09T08:00:00+00:00")
        self.assertEqual(m.recipients, ("support@acme.example",))
        self.assertEqual(cursor, str(NOW - 100))

    def test_cursor_resumes_from_end_time(self):
        self.h.fetch_inbound(str(NOW - 500))
        self.assertEqual(self.zd.calls[0]["query"]["start_time"], str(NOW - 500))

    def test_never_asks_for_the_last_minute(self):
        self.h.fetch_inbound(str(NOW))
        self.assertEqual(self.zd.calls[0]["query"]["start_time"], str(NOW - 61))

    def test_agent_replies_and_private_notes_are_not_customer_mail(self):
        self.zd.events_pages[0]["ticket_events"][0]["child_events"] = [
            comment_event(1, AGENT, "agent public reply"),
            comment_event(2, REQUESTER, "private by end user", public=False),
            comment_event(3, REQUESTER, "real customer words"),
            {"id": 4, "type": "Change", "event_type": "Change", "field_name": "status", "value": "open"}]
        msgs, _ = self.h.fetch_inbound(None)
        self.assertEqual([m.body for m in msgs], ["real customer words"])

    def test_unknown_role_raises_and_cursor_does_not_move(self):
        self.zd.users[REQUESTER]["role"] = "light-agent"
        with self.assertRaises(zendesk.ZendeskError):
            self.h.fetch_inbound("123")

    def test_unreadable_author_raises(self):
        del self.zd.users[REQUESTER]
        with self.assertRaises(zendesk.ZendeskError):
            self.h.fetch_inbound(None)

    def test_naive_time_refused(self):
        self.zd.events_pages[0]["ticket_events"][0]["child_events"][0]["created_at"] = "2025-10-09 08:00:00"
        with self.assertRaises(zendesk.ZendeskError):
            self.h.fetch_inbound(None)

    def test_missing_end_time_raises(self):
        del self.zd.events_pages[0]["end_time"]
        with self.assertRaises(zendesk.ZendeskError):
            self.h.fetch_inbound(None)

    def test_bad_cursor_raises(self):
        with self.assertRaises(zendesk.ZendeskError):
            self.h.fetch_inbound("1970-01-01")

    def test_pages_capped_per_run(self):
        page = copy.deepcopy(self.zd.events_pages[0])
        page["end_of_stream"] = False
        self.zd.events_pages = [dict(page, end_time=NOW - 300 + i) for i in range(5)]
        _, cursor = self.h.fetch_inbound(None)
        exports = [c for c in self.zd.calls if c["path"].endswith("ticket_events")]
        self.assertEqual(len(exports), zendesk.INBOUND_PAGES)
        self.assertEqual(cursor, str(NOW - 300 + zendesk.INBOUND_PAGES - 1))

    def test_duplicate_events_keep_one_id(self):
        page = self.zd.events_pages[0]
        page["ticket_events"].append(copy.deepcopy(page["ticket_events"][0]))
        msgs, _ = self.h.fetch_inbound(None)
        self.assertEqual({m.message_id for m in msgs}, {"zendesk:35436:1274"})


class ReplyTests(ZendeskTestBase):
    def send(self, body="Hello Johnny,\n\nWe fixed it.\nThanks", key="reply:c1:abc"):
        return self.h.send_reply("35436", "johnny@example.com", "Re: printer", body, idempotency_key=key)

    def puts(self):
        return [c for c in self.zd.calls if c["method"] == "PUT"]

    def test_public_comment_with_metadata_and_idempotency_key(self):
        cid = self.send()
        put = self.puts()[0]
        self.assertEqual(put["path"], "/api/v2/tickets/35436")
        c = put["body"]["ticket"]["comment"]
        self.assertEqual(c, {"html_body": "<p>Hello Johnny,</p><p>We fixed it.<br>Thanks</p>", "public": True})
        self.assertEqual(put["body"]["ticket"]["metadata"]["sayso_key"], "reply:c1:abc")
        self.assertEqual(len(put["headers"]["Idempotency-Key"]), 64)
        self.assertNotIn("status", put["body"]["ticket"], "a reply must not change the ticket status")
        self.assertTrue(cid.isdigit())

    def test_html_is_escaped(self):
        self.send(body="<script>x</script> & **bold**")
        self.assertEqual(self.puts()[0]["body"]["ticket"]["comment"]["html_body"],
                         "<p>&lt;script&gt;x&lt;/script&gt; &amp; **bold**</p>")

    def test_retry_after_crash_finds_the_first_write(self):
        self.zd.fail_put_after_write = True
        with self.assertRaises(HttpError):
            self.send()
        self.zd.fail_put_after_write = False
        self.send()
        self.assertEqual(len(self.puts()), 1, "a retry must find the marked audit and not comment twice")
        self.assertEqual(sum(1 for c in self.zd.comments[35436] if c["author_id"] == AGENT), 1)

    def test_same_key_twice_writes_once(self):
        a = self.send()
        b = self.send()
        self.assertEqual(a, b)
        self.assertEqual(len(self.puts()), 1)

    def test_cc_refused(self):
        self.zd.tickets[35436]["email_cc_ids"] = [OTHER]
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send()
        self.assertEqual(self.puts(), [])

    def test_collaborator_refused(self):
        self.zd.tickets[35436]["collaborator_ids"] = [OTHER]
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send()
        self.assertEqual(self.puts(), [])

    def test_requester_must_be_the_approved_recipient(self):
        with self.assertRaises(zendesk.ZendeskRefused):
            self.h.send_reply("35436", "other@example.com", "s", "hi", idempotency_key="k")
        self.assertEqual(self.puts(), [])

    def test_placeholder_refused(self):
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send(body="Your id is {{ticket.id}}")
        self.assertEqual(self.zd.calls, [])

    def test_too_long_refused(self):
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send(body="x" * 70_000)
        self.assertEqual(self.zd.calls, [])

    def test_closed_ticket_refused(self):
        self.zd.tickets[35436]["status"] = "closed"
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send()
        self.assertEqual(self.puts(), [])

    def test_non_numeric_ticket_refused(self):
        for bad in ("../users/me", "1 2", "", "35436?x=1"):
            with self.assertRaises(zendesk.ZendeskRefused):
                self.h.send_reply(bad, "johnny@example.com", "s", "hi", idempotency_key="k")
        self.assertEqual(self.zd.calls, [])

    def test_empty_body_refused(self):
        with self.assertRaises(zendesk.ZendeskRefused):
            self.send(body="  \n ")


class ReadbackTests(ZendeskTestBase):
    BODY = "Hello Johnny,\n\nWe fixed it."

    def sha(self, body=BODY):
        import hashlib
        return hashlib.sha256(body.encode()).hexdigest()

    def send(self, body=BODY):
        return self.h.send_reply("35436", "johnny@example.com", "s", body, idempotency_key="reply:c1:k")

    def test_sent_and_matching(self):
        self.send()
        rows = self.h.sent_readback("35436", self.sha())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.status, r.recipient, r.sender), ("Sent", "johnny@example.com", "support@acme.example"))
        self.assertTrue(r.message_id.startswith("zendesk:35436:"))

    def test_nothing_written_reads_empty(self):
        self.assertEqual(self.h.sent_readback("35436", self.sha()), [])

    def test_changed_text_is_not_sent(self):
        self.zd.store_body = "Hello Johnny, we fixed it and refunded you."
        self.send()
        self.assertEqual(self.h.sent_readback("35436", self.sha())[0].status, "Not Sent")

    def test_private_note_is_not_sent(self):
        self.send()
        self.zd.comments[35436][-1]["public"] = False
        self.assertEqual(self.h.sent_readback("35436", self.sha())[0].status, "Not Sent")

    def test_deleted_comment_is_not_sent(self):
        self.send()
        self.zd.comments[35436].pop()
        self.assertEqual(self.h.sent_readback("35436", self.sha())[0].status, "Not Sent")

    def test_cc_added_later_shows_in_recipient(self):
        self.send()
        self.zd.tickets[35436]["email_cc_ids"] = [OTHER]
        self.assertEqual(self.h.sent_readback("35436", self.sha())[0].recipient,
                         "johnny@example.com,other@example.com")

    def test_mailbox_counts_the_requester_notification(self):
        self.send()
        mid = self.h.sent_readback("35436", self.sha())[0].message_id
        self.assertEqual(zendesk.ZendeskMailbox(self.h).sent_copies(mid), 1)

    def test_mailbox_zero_without_notification(self):
        self.zd.notify = False
        self.send()
        mid = self.h.sent_readback("35436", self.sha())[0].message_id
        self.assertEqual(zendesk.ZendeskMailbox(self.h).sent_copies(mid), 0)

    def test_mailbox_refuses_foreign_id(self):
        with self.assertRaises(zendesk.ZendeskError):
            zendesk.ZendeskMailbox(self.h).sent_copies("<abc@mail>")


class TimesTests(ZendeskTestBase):
    def test_last_sent_at_is_newest_public_agent_comment(self):
        self.assertIsNone(self.h.last_sent_at("35436"))
        self.h.send_reply("35436", "johnny@example.com", "s", "hi", idempotency_key="k")
        self.assertEqual(self.h.last_sent_at("35436"), "2025-10-10T09:00:00+00:00")

    def test_private_agent_note_is_not_a_send(self):
        self.zd.comments[35436].append({"id": 9, "author_id": AGENT, "body": "note", "public": False,
                                        "created_at": "2025-10-11T00:00:00Z"})
        self.assertIsNone(self.h.last_sent_at("35436"))

    def test_last_inbound_includes_other_tickets(self):
        self.zd.tickets[777] = ticket(777, updated_at="2025-10-12T00:00:00Z")
        self.zd.comments[777] = [{"id": 70, "author_id": REQUESTER, "body": "new thread", "public": True,
                                  "created_at": "2025-10-12T00:00:00Z"}]
        self.zd.requested[REQUESTER] = [777, 35436]
        self.assertEqual(self.h.last_inbound_at("35436", "johnny@example.com"), "2025-10-12T00:00:00+00:00")

    def test_last_inbound_ignores_someone_elses_comment_on_other_ticket(self):
        self.zd.tickets[777] = ticket(777, updated_at="2025-10-12T00:00:00Z")
        self.zd.comments[777] = [{"id": 70, "author_id": OTHER, "body": "x", "public": True,
                                  "created_at": "2025-10-12T00:00:00Z"}]
        self.zd.requested[REQUESTER] = [777]
        self.assertEqual(self.h.last_inbound_at("35436", "johnny@example.com"), "2025-10-09T08:00:00+00:00")

    def test_last_inbound_on_ticket_counts_only_end_users(self):
        self.h.send_reply("35436", "johnny@example.com", "s", "hi", idempotency_key="k")
        self.zd.requested[REQUESTER] = []
        self.assertEqual(self.h.last_inbound_at("35436", "johnny@example.com"), "2025-10-09T08:00:00+00:00")

    def test_older_other_tickets_are_not_opened(self):
        self.zd.tickets[777] = ticket(777, updated_at="2020-01-01T00:00:00Z")
        self.zd.comments[777] = []
        self.zd.requested[REQUESTER] = [777]
        self.h.last_inbound_at("35436", "johnny@example.com")
        self.assertFalse(any(c["path"] == "/api/v2/tickets/777/comments" for c in self.zd.calls))


class RateLimitTests(ZendeskTestBase):
    def test_retry_after_header_honoured(self):
        self.zd.ratelimit = [7]
        self.h.check()
        self.assertEqual(self.waits, [7.0])

    def test_long_wait_ends_the_run(self):
        self.zd.ratelimit = [600]
        with self.assertRaises(zendesk.RateLimited) as cm:
            self.h.check()
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(self.waits, [])

    def test_gives_up_after_retries(self):
        self.zd.ratelimit = [1, 1, 1, 1, 1]
        with self.assertRaises(zendesk.RateLimited):
            self.h.check()
        self.assertEqual(len(self.waits), 3)

    def test_non_json_raises(self):
        h = zendesk.ZendeskHelpdesk("acme", "Bearer x", "s@a.example", transport=lambda *a: (200, {}, b"<html>"))
        with self.assertRaises(zendesk.ZendeskError):
            h.check()


class WiringTests(unittest.TestCase):
    def test_config_accepts_zendesk_for_helpdesk_and_mailbox(self):
        from pathlib import Path

        from sayso import config
        raw = {"product": {"slug": "acme", "mode": "live", "support_address": "support@acme.example"},
               "operators": {"1": "Ann"}, "state": {"dir": "/tmp/sayso-zd-test"},
               "helpdesk": {"adapter": "zendesk", **OPTS}, "mailbox": {"adapter": "zendesk"}}
        cfg = config.parse(raw, Path("/tmp/x.toml"))
        self.assertEqual(cfg.section("helpdesk").adapter, "zendesk")
        with mock.patch.dict(os.environ, ENV):
            from sayso.adapters import reference
            h = reference.build("helpdesk", "zendesk", cfg.section("helpdesk"), cfg)
            m = reference.build("mailbox", "zendesk", cfg.section("mailbox"), cfg)
        self.assertIsInstance(h, zendesk.ZendeskHelpdesk)
        self.assertIsInstance(m, zendesk.ZendeskMailbox)

    def test_mailbox_zendesk_needs_helpdesk_zendesk(self):
        from pathlib import Path

        from sayso import config
        from sayso.adapters import reference
        raw = {"product": {"slug": "acme", "mode": "live", "support_address": "support@acme.example"},
               "operators": {"1": "Ann"}, "state": {"dir": "/tmp/sayso-zd-test"},
               "mailbox": {"adapter": "zendesk"}}
        cfg = config.parse(raw, Path("/tmp/x.toml"))
        with self.assertRaises(ConfigError):
            reference.build("mailbox", "zendesk", cfg.section("mailbox"), cfg)


if __name__ == "__main__":
    unittest.main()
