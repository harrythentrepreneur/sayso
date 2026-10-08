"""The Baker (texting product) adapter against a RECORDING transport, and its cards through the real loop."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sayso import cases, config as config_mod, runtime, stages, votes
from sayso.adapters import baker
from sayso.adapters.base import SentRecord
from sayso.config import ConfigError, Section
from sayso.jobs import intake, sender

T1 = "11111111-2222-3333-4444-555555555555"
T2 = "99999999-2222-3333-4444-555555555555"
ENV = {"B_TOKEN": "tok"}


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None})
        status, payload = self.responses.pop(0)
        return status, {}, json.dumps(payload).encode()


def section(**extra):
    return Section("baker", {"url": "https://api.baker.test", "token_env": "B_TOKEN", **extra})


def teacher(tid=T1, **kw):
    return {"teacher_id": tid, "name": "Priya", "plan": "trial", "channel": "whatsapp", "phone": "…0702",
            "resources": 2, "opted_out": False, "qa": False, "year_group": "Year 1", **kw}


def signal(n=1, tid=T1, kind="correspondence", **kw):
    return {"type": "signal", "id": f"signal:{n}", "at": "2026-10-05T01:00:00+00:00", "source": "flag",
            "reason": "wrong_content", "label": "Something Baker made was wrong", "kind": kind,
            "summary": "Pitched at Year 6, she teaches Year 1", "teacher": teacher(tid, **kw),
            "transcript": "**Teacher** (05 Oct 01:00): my class can't read this, it's year 1 not year 6"}


@mock.patch.dict(os.environ, ENV)
class AdapterTests(unittest.TestCase):
    def helpdesk(self, responses, **extra):
        rec = Recorder(responses)
        return baker.BakerHelpdesk(section(**extra), "hello@textbaker.com", transport=rec), rec

    def test_feed_becomes_messages_with_kind_card_and_no_phone(self):
        follow = {"type": "text", "id": "message:9", "at": "2026-10-05T02:00:00+00:00", "kind": "correspondence",
                  "teacher": teacher(), "body": "hello? anyone", "baker_before": "On it."}
        h, rec = self.helpdesk([(200, {"events": [signal(), follow], "cursor": "s1:m9"})])
        msgs, cur = h.fetch_inbound("s0:m0")
        self.assertEqual(cur, "s1:m9")
        self.assertIn("cursor=s0%3Am0", rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["headers"]["Authorization"], "Bearer tok")
        first, second = msgs
        self.assertEqual((first.sender, first.ticket, first.kind), (f"teacher:{T1}",) * 2 + ("correspondence",))
        self.assertIn("my class can't read this", first.card)                 # Her words, verbatim.
        self.assertIn("…0702", first.card)
        self.assertEqual(first.subject, "Priya - Something Baker made was wrong")
        self.assertIn("hello? anyone", second.card)
        self.assertIn("Baker had said: On it.", second.card)

    def test_qa_teacher_is_labelled(self):
        h, _ = self.helpdesk([(200, {"events": [signal(qa=True)], "cursor": "s1:m0"})])
        self.assertEqual(h.fetch_inbound(None)[0][0].labels, ("QA test",))

    def test_bad_feed_shapes_raise_instead_of_guessing(self):
        for payload in ({"events": "x", "cursor": "c"}, {"events": []},
                        {"events": [{**signal(), "type": "mystery"}], "cursor": "c"},
                        {"events": [signal(tid="not-a-uuid")], "cursor": "c"}):
            h, _ = self.helpdesk([(200, payload)])
            with self.assertRaises((RuntimeError, ValueError)):
                h.fetch_inbound(None)

    def test_send_posts_key_teacher_and_exact_body(self):
        h, rec = self.helpdesk([(200, {"key": "k1", "status": "sent"})])
        h.send_reply(f"teacher:{T1}", f"teacher:{T1}", "Re: x", "Hi Priya", idempotency_key="k1")
        self.assertEqual(rec.calls[0]["body"], {"key": "k1", "teacher_id": T1, "body": "Hi Priya"})

    def test_send_refuses_another_recipient_and_a_refusal_raises(self):
        h, rec = self.helpdesk([(200, {"key": "k", "status": "refused", "note": "outside WhatsApp's 24-hour window"})])
        with self.assertRaises(ValueError):
            h.send_reply(f"teacher:{T1}", f"teacher:{T2}", "s", "b", idempotency_key="k")
        self.assertEqual(rec.calls, [])
        with self.assertRaisesRegex(RuntimeError, "24-hour"):
            h.send_reply(f"teacher:{T1}", f"teacher:{T1}", "s", "b", idempotency_key="k")

    def test_sending_off_never_calls_baker(self):
        h, rec = self.helpdesk([], sending=False)
        with self.assertRaises(baker.SendingOff):
            h.send_reply(f"teacher:{T1}", f"teacher:{T1}", "s", "b", idempotency_key="k")
        self.assertEqual(rec.calls, [])
        with self.assertRaises(ConfigError):
            baker.BakerHelpdesk(section(sending="no"), "hello@textbaker.com")

    def test_readback_counts_only_the_exact_text_for_this_teacher(self):
        replies = [{"key": "k1", "teacher_id": T1, "status": "sent", "body_sha": "aaa"},
                   {"key": "k2", "teacher_id": T1, "status": "failed", "body_sha": "aaa"},
                   {"key": "k3", "teacher_id": T1, "status": "sent", "body_sha": "bbb"}]
        h, rec = self.helpdesk([(200, {"replies": replies})])
        rows = h.sent_readback(f"teacher:{T1}", "aaa")
        self.assertEqual([(r.message_id, r.status, r.recipient) for r in rows],
                         [("k1", "Sent", f"teacher:{T1}"), ("k2", "Not Sent", f"teacher:{T1}")])
        self.assertIn("sha=aaa", rec.calls[0]["url"])

    def test_delivery_needs_the_providers_own_status(self):
        for row, want in (({"status": "sent", "delivery": "delivered"}, 1), ({"status": "sent", "delivery": "read"}, 1),
                          ({"status": "sent", "delivery": None}, 0), ({"status": "sent", "delivery": "failed"}, 0),
                          ({"status": "refused", "delivery": "logged"}, 0)):
            rec = Recorder([(200, row)])
            self.assertEqual(baker.BakerDelivery(section(), transport=rec).sent_copies("reply:a/b:v1"), want, row)
        self.assertIn("/ops/cases/reply/reply%3Aa%2Fb%3Av1", rec.calls[0]["url"])

    def test_url_and_secret_rules(self):
        with self.assertRaises(ConfigError):
            baker.BakerHelpdesk(Section("baker", {"url": "http://api.baker.test", "token_env": "B_TOKEN"}), "a@b.c")
        with self.assertRaises(ConfigError):
            baker.BakerHelpdesk(Section("baker", {"url": "https://x", "token_env": "MISSING_ENV"}), "a@b.c")


class FakeBaker:
    """Stands in for the two Baker adapters in the real loop: a feed, a sender and the provider's status."""

    def __init__(self):
        self.events, self.sent, self.delivery = [], [], {}

    def fetch_inbound(self, cursor):
        """Through the REAL Baker adapter, so the loop sees exactly what it would see live."""
        start = int(cursor or 0)
        rec = Recorder([(200, {"events": self.events[start:], "cursor": str(len(self.events))})])
        with mock.patch.dict(os.environ, ENV):
            return baker.BakerHelpdesk(section(), "hello@textbaker.com", transport=rec).fetch_inbound(cursor)

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        self.sent.append((ticket, body))
        return idempotency_key

    def sent_readback(self, ticket, body_sha):
        import hashlib
        return [SentRecord(ticket=ticket, recipient=ticket, sender="hello@textbaker.com", subject="",
                           body_sha=body_sha, message_id=f"key{i}", status="Sent")
                for i, (t, b) in enumerate(self.sent) if t == ticket and hashlib.sha256(b.encode()).hexdigest() == body_sha]

    def sent_copies(self, message_id):
        return self.delivery.get(message_id, 0)

    def last_sent_at(self, ticket):
        return None

    def last_inbound_at(self, ticket, customer):
        return None


TOML = """
[product]
name = "TextBaker"
slug = "textbaker"
support_address = "hello@textbaker.com"
labels = ["QA test"]
[operators]
"1001" = "Operator One"
[state]
dir = "{state}"
"""


class LoopWithBakerTests(unittest.TestCase):
    """The REAL intake and sender jobs, with Baker-shaped messages."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hcml-baker-")
        tmp = Path(self._tmp.name)
        (tmp / "p.toml").write_text(TOML.format(state=tmp / "state"))
        self.ctx = runtime.build(config_mod.load(tmp / "p.toml"))
        self.fb = FakeBaker()
        self.ctx.helpdesk = self.ctx.mailbox = self.fb

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_card_per_teacher_per_kind_with_her_words_and_a_readable_poll(self):
        self.fb.events += [signal(1), signal(2, kind="failure:resource_failed"), signal(3),
                           signal(4, tid=T2, qa=True)]
        out = intake.run(self.ctx)
        self.assertEqual(out["opened"], 3)                      # T1 chat, T1 failure, T2 chat.
        self.assertEqual(out["appended"], 1)                    # T1's second signal joins her first card.
        all_cases = cases.load(self.ctx)
        by_kind = sorted((r["customer"], r["kind"]) for r in all_cases.values())
        self.assertEqual(by_kind, [(f"teacher:{T1}", "correspondence"), (f"teacher:{T1}", "failure:resource_failed"),
                                   (f"teacher:{T2}", "correspondence")])
        card = next(r for r in all_cases.values() if r["kind"] == "correspondence" and T1 in r["customer"])
        first = self.ctx.board.messages(card["card_id"])[0]
        self.assertIn("my class can't read this", first)
        self.assertNotIn("+44", first)
        qa = next(r for r in all_cases.values() if T2 in r["customer"])
        self.assertIn("QA test", self.ctx.board.get_tags(qa["card_id"]))
        self.assertEqual(cases.subject(card), "Priya - Something Baker made was wrong")
        votes.check_human_subject(cases.subject(card))            # A person can read it cold.

    def approved_reply(self):
        self.fb.events.append(signal(1))
        intake.run(self.ctx)
        key, rec = next(iter(cases.load(self.ctx).items()))
        path = self.ctx.paths.drafts / key / "reply-v1.txt"
        path.parent.mkdir(parents=True)
        path.write_text("Hi Priya, Harry from Baker here. I've redone it for Year 1.\n")
        import hashlib
        sha = hashlib.sha256(path.read_text().encode()).hexdigest()
        cases.move(self.ctx, rec["card_id"], stages.AWAITING)
        v = votes.open_vote(self.ctx, key=f"reply:{key}:v1", kind="reply", case_key=key, card_id=rec["card_id"],
                            subject=cases.subject(rec), question="send draft reply v1 as written?",
                            identity={"case_key": key, "ticket": rec["ticket"], "recipient": rec["customer"],
                                      "path": str(path)}, material={"sha256": sha},
                            spec={"path": str(path), "sha256": sha, "version": 1})
        votes.set_status(self.ctx, v["key"], votes.APPROVED, decided_by="Operator One")
        return v["key"], rec

    def test_sender_needs_the_providers_delivery_to_call_it_sent(self):
        key, rec = self.approved_reply()
        self.assertEqual(sender.run(self.ctx, sleep=lambda s: None)[key], sender.UNVERIFIED)
        self.assertEqual(len(self.fb.sent), 1)
        self.assertEqual(sender.run(self.ctx, sleep=lambda s: None)[key], "already-UNVERIFIED")  # Never re-sent.
        self.assertEqual(len(self.fb.sent), 1)

    def test_sender_proves_with_baker_record_and_delivery(self):
        key, rec = self.approved_reply()
        self.fb.delivery["key0"] = 1
        self.assertEqual(sender.run(self.ctx, sleep=lambda s: None)[key], sender.VERIFIED)
        self.assertEqual(self.fb.sent, [(f"teacher:{T1}", "Hi Priya, Harry from Baker here. I've redone it for Year 1.\n")])
        tags = self.ctx.board.get_tags(rec["card_id"])
        self.assertIn(stages.DONE, tags)
        self.assertIn(stages.WAITING, tags)

    def test_shadow_run_never_sends_even_when_approved(self):
        key, rec = self.approved_reply()
        rec_ = Recorder([])
        with mock.patch.dict(os.environ, ENV):
            self.ctx.helpdesk = baker.BakerHelpdesk(section(sending=False), "hello@textbaker.com", transport=rec_)
        self.ctx.helpdesk.sent_readback = lambda t, s: []
        self.assertEqual(sender.run(self.ctx, sleep=lambda s: None)[key], sender.UNVERIFIED)
        self.assertEqual(rec_.calls, [])
        posts = "\n".join(self.ctx.board.messages(rec["card_id"]))
        self.assertIn("sending is off", posts)


if __name__ == "__main__":
    unittest.main()
