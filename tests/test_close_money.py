"""Part 7, closing cases and money: ported from the PhonicsMaker loop.

1. No-reply close: support says no reply is owed -> one close vote -> Done.
2. Drafter-proposed money: a MONEY line becomes a refund/cancel vote; the reply
   waits for the provider's receipt instead of being redrafted.
3. A busy job lock is waited for, and every enabled job is watched by health.
4. A reopened case's new vote is never voided by an old close.

Every test drives the real jobs on fakes and asserts what landed.
"""
from __future__ import annotations

import json
import threading
import time

from sayso import cases, stages, votes
from sayso.jobs import health
from sayso.store import single_flight
from tests.helpers import LoopTest

REFUND = {"action": "refund", "charge": "ch_test0001", "amount": 1900, "currency": "usd", "customer": "cus_test0001"}
CANCEL = {"action": "cancel_subscription", "subscription": "sub_test0001", "customer": "cus_test0001"}


def money_line(spec):
    return "MONEY: " + json.dumps(spec)


class Base(LoopTest):
    def drafter_says(self, text):
        self.w.data["next_draft"] = text
        self.w.save()

    def card(self):
        return self.only_case()[1]["card_id"]

    def stage(self):
        return stages.stage_of(self.tags(self.card()))

    def posts(self):
        return "\n".join(self.ctx.board.messages(self.card()))

    def all_votes(self, kind=None):
        return [v for v in votes.load(self.ctx).values() if kind is None or v["kind"] == kind]


class NoReplyTests(Base):
    def no_reply_case(self, reason="spam: a link-farm invite, not a customer"):
        self.write_in(subject="Partner with us!!!", body="Click here")
        self.drafter_says(f"NO-REPLY: {reason}")
        self.tick("intake", "draft")

    def test_no_reply_opens_one_close_vote_and_no_reply_vote(self):
        self.no_reply_case()
        self.assertEqual(self.all_votes("reply"), [])
        (v,) = self.all_votes("close")
        self.assertIn("close without a reply", v["question"])
        self.assertEqual(self.stage(), stages.AWAITING)
        self.assertIn("spam: a link-farm invite", self.posts())

    def test_approved_no_reply_close_goes_to_done_once_and_sends_nothing(self):
        self.no_reply_case()
        self.vote("close")
        self.tick("votes", "close", "close", "draft", "sender")
        self.assertEqual(self.tags(self.card()), [stages.DONE])
        self.assertEqual(self.w.data["outbox"], [])
        self.assertEqual(self.posts().count("Closed with no reply"), 1)

    def test_settled_no_reply_case_is_not_redrafted(self):
        self.no_reply_case()
        self.vote("close")
        self.tick("votes", "close")
        calls = []
        orig = self.ctx.drafter.draft
        self.ctx.drafter.draft = lambda *a: calls.append(1) or orig(*a)
        for _ in range(5):
            self.tick("intake", "draft")
        self.assertEqual(calls, [], "a settled no-reply card is never restarted")

    def test_customer_writes_again_voids_the_close_and_redrafts(self):
        self.no_reply_case()
        v = self.live_vote("close")
        self.ctx.clock.advance(minutes=5)
        self.write_in(subject="Partner with us!!!", body="Actually I am a real teacher and need help")
        self.tick("intake", "draft")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(len(self.all_votes("reply")), 1, "a new message gets a real draft")

    def test_customer_wrote_before_intake_ran_voids_the_close(self):
        self.no_reply_case()
        v = self.vote("close")
        self.ctx.clock.advance(minutes=5)
        self.write_in(subject="Partner with us!!!", body="hello?")
        self.tick("votes", "close")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertNotEqual(self.tags(self.card()), [stages.DONE])

    def test_declined_no_reply_close_asks_for_a_real_draft(self):
        self.no_reply_case()
        self.vote("close", answer="no")
        self.tick("votes", "close", "draft")
        self.assertEqual(len(self.all_votes("reply")), 1)
        self.assertIn("close declined", self.posts())

    def test_second_no_reply_after_a_decline_is_held_for_a_human(self):
        self.no_reply_case()
        self.vote("close", answer="no")
        self.drafter_says("NO-REPLY: still spam")
        self.tick("votes", "close", "draft", "draft")
        self.assertEqual(len(self.all_votes("close")), 1, "never asked twice for the same message")
        self.assertTrue(any("no-reply" in a for a in self.w.data["alerts"]))

    def test_held_second_no_reply_is_not_redrafted_every_pass(self):
        self.no_reply_case()
        self.vote("close", answer="no")
        self.drafter_says("NO-REPLY: still spam")
        self.tick("votes", "close", "draft")
        calls = []
        orig = self.ctx.drafter.draft
        self.ctx.drafter.draft = lambda *a: calls.append(1) or orig(*a)
        for _ in range(5):
            self.tick("draft")
        self.assertEqual(calls, [], "a held no-reply waits for a person; the drafter is not re-run")

    def test_open_no_reply_vote_does_not_close(self):
        self.no_reply_case()
        self.tick("votes", "close", "close")
        self.assertEqual(self.stage(), stages.AWAITING)

    def test_card_moved_by_hand_voids_the_no_reply_close(self):
        self.no_reply_case()
        v = self.vote("close")
        cases.move(self.ctx, self.card(), stages.IN_SUPPORT)
        self.tick("votes", "close")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(self.stage(), stages.IN_SUPPORT)

    def test_unreadable_helpdesk_never_closes(self):
        self.no_reply_case()
        self.vote("close")
        self.tick("votes")

        def broken(*a):
            raise RuntimeError("helpdesk down")
        self.ctx.helpdesk.last_inbound_at = broken
        self.tick("close")
        self.assertEqual(self.stage(), stages.AWAITING, "cannot prove the customer is quiet, so no close")

    def test_no_reply_without_a_reason_is_held(self):
        self.write_in()
        self.drafter_says("NO-REPLY:   ")
        self.tick("intake", "draft")
        self.assertEqual(self.all_votes(), [])
        self.assertIn("held", self.posts())

    def test_no_reply_line_inside_a_reply_is_not_a_decision(self):
        self.write_in()
        self.drafter_says("Hi Pat,\n\nWe looked into it.\nNO-REPLY: ignore\nThanks")
        self.tick("intake", "draft")
        self.assertEqual(self.all_votes("close"), [])


class DraftedMoneyTests(Base):
    def refund_case(self, body="Hi Pat,\n\nWe have refunded the duplicate charge.\n\nSupport", spec=REFUND):
        self.write_in(subject="Charged twice", body="You charged me twice")
        self.drafter_says(f"{body}\n{money_line(spec)}")
        self.tick("intake", "draft")

    def test_money_line_opens_a_money_vote_and_holds_the_reply(self):
        self.refund_case()
        (m,) = self.all_votes("money")
        self.assertEqual(m["spec"], REFUND)
        self.assertEqual(self.all_votes("reply"), [], "the reply waits for the receipt")
        self.assertEqual(self.stage(), stages.AWAITING)
        self.assertIn("waits for the refund", self.posts())

    def test_money_line_is_never_part_of_the_reply(self):
        self.refund_case()
        self.vote("money")
        self.tick("votes", "money", "draft")
        self.vote("reply")
        self.tick("votes", "sender")
        (sent,) = self.w.data["outbox"]
        (r,) = self.all_votes("reply")
        body = open(r["spec"]["path"], encoding="utf-8").read()
        import hashlib
        self.assertEqual(sent["body_sha"], hashlib.sha256(body.encode()).hexdigest(), "this exact file was sent")
        self.assertNotIn("MONEY", body)
        self.assertNotIn("ch_test0001", body)
        self.assertIn("refunded the duplicate charge", body)

    def test_receipt_releases_the_same_draft_without_redrafting(self):
        self.refund_case()
        calls = []
        orig = self.ctx.drafter.draft
        self.ctx.drafter.draft = lambda *a: calls.append(1) or orig(*a)
        self.vote("money")
        self.tick("votes", "money", "draft")
        (r,) = self.all_votes("reply")
        self.assertEqual(r["spec"]["version"], 1)
        self.assertEqual(calls, [], "the held reply is offered as written, not redrafted")
        self.assertEqual(len(self.w.data["refunds"]), 1)

    def test_no_reply_vote_before_the_receipt(self):
        self.w.data["faults"] = ["refund_wrong_amount"]
        self.w.save()
        self.refund_case()
        self.vote("money")
        self.tick("votes", "money", "draft", "draft")
        self.assertEqual(self.all_votes("reply"), [], "an unverified refund never releases the reply")

    def test_declined_money_asks_for_a_new_draft(self):
        self.refund_case()
        self.vote("money", answer="no")
        self.drafter_says("Hi Pat,\n\nWe could not refund this one; here is why.\n\nSupport")
        self.tick("votes", "money", "draft", "draft")
        (r,) = self.all_votes("reply")
        self.assertEqual(r["spec"]["version"], 2)
        self.assertIn("declined", self.posts())

    def test_declined_money_proposed_again_is_held_not_looped(self):
        """r4 QA-1 p30 D: after a declined refund the drafter (unchanged prompt) proposed the SAME refund again.
        open_vote handed back the declined vote, release_held dropped the reply at once and asked for a new
        draft: one drafter call every other pass, for ever. Now the repeat is held for a person: one call, one
        held note, then nothing for 6 passes."""
        self.refund_case()
        self.vote("money", answer="no")
        self.tick("votes", "money")
        self.drafter_says(f"Hi Pat,\n\nWe have refunded the duplicate charge.\n\nSupport\n{money_line(REFUND)}")
        calls = []
        orig = self.ctx.drafter.draft
        self.ctx.drafter.draft = lambda *a: calls.append(1) or orig(*a)
        for _ in range(8):
            self.tick("draft")
        self.assertEqual(len(calls), 1, "one redraft after the decline, then it waits")
        self.assertEqual(self.posts().count("already declined or voided"), 1)
        self.assertEqual(len(self.all_votes("money")), 1)
        self.assertEqual(self.all_votes("money")[0]["status"], votes.DECLINED)
        self.assertEqual(self.all_votes("reply"), [])
        key = self.only_case()[0]
        self.assertTrue(cases.load(self.ctx)[key].get("held_draft"), "held: Cloud shows it as Needs you")

    def test_declined_money_then_a_person_asks_and_the_drafter_drops_it(self):
        self.refund_case()
        self.vote("money", answer="no")
        self.drafter_says(f"Hi Pat,\n\nWe have refunded the duplicate charge.\n\nSupport\n{money_line(REFUND)}")
        self.tick("votes", "money", "draft", "draft")
        key = self.only_case()[0]
        self.assertTrue(cases.load(self.ctx)[key].get("held_draft"))
        self.drafter_says("Hi Pat,\n\nWe could not refund this one; here is why.\n\nSupport")
        cases.update(self.ctx, key, needs_draft=True)          # "Write a new draft"
        self.tick("draft")
        (r,) = self.all_votes("reply")
        self.assertEqual(r["status"], votes.OPEN)

    def test_a_different_refund_after_a_decline_still_opens_a_vote(self):
        self.refund_case()
        self.vote("money", answer="no")
        other = {**REFUND, "charge": "ch_test0002"}
        self.drafter_says(f"Hi Pat,\n\nWe have refunded the other charge.\n\nSupport\n{money_line(other)}")
        self.tick("votes", "money", "draft", "draft")
        live = [v for v in self.all_votes("money") if v["status"] == votes.OPEN]
        self.assertEqual([v["spec"]["charge"] for v in live], ["ch_test0002"])

    def test_held_file_changed_is_never_offered(self):
        self.refund_case()
        held = cases.load(self.ctx)[self.only_case()[0]]["held_reply"]
        with open(held["path"], "a", encoding="utf-8") as stream:
            stream.write("P.S. tampered\n")
        self.vote("money")
        self.tick("votes", "money", "draft")
        for r in self.all_votes("reply"):
            self.assertNotIn("tampered", open(r["spec"]["path"], encoding="utf-8").read())
        self.assertTrue(any("held reply file changed" in a for a in self.w.data["alerts"]))

    def test_customer_writes_while_money_pending_gets_a_fresh_draft(self):
        self.refund_case()
        self.ctx.clock.advance(minutes=5)
        self.write_in(subject="Charged twice", body="also my login is broken")
        self.tick("intake")
        self.vote("money")
        self.tick("votes", "money", "draft", "draft")
        (r,) = self.all_votes("reply")
        self.assertEqual(r["spec"]["version"], 2, "the held v1 does not answer the newer message")
        self.assertIn("replaced by a new draft", self.posts())

    def test_over_the_cap_is_held_not_voted(self):
        self.refund_case(spec={**REFUND, "amount": 999999})
        self.assertEqual(self.all_votes(), [])
        self.assertIn("held", self.posts())

    def test_payments_off_is_held(self):
        self.ctx.payments = None
        self.refund_case()
        self.assertEqual(self.all_votes(), [])
        self.assertIn("payments are off", self.posts())

    def test_bad_json_is_held(self):
        self.write_in(subject="Charged twice")
        self.drafter_says("Hi Pat,\n\nRefunded.\nMONEY: {not json")
        self.tick("intake", "draft")
        self.assertEqual(self.all_votes(), [])
        self.assertIn("held", self.posts())

    def test_refund_and_cancel_both_must_be_verified(self):
        body = "Hi Pat,\n\nWe have refunded you.\n\nSupport"
        self.write_in(subject="Cancel and refund please")
        self.drafter_says(f"{body}\n{money_line(REFUND)}\n{money_line(CANCEL)}")
        self.tick("intake", "draft")
        self.assertEqual(len(self.all_votes("money")), 2)
        refund = next(v for v in self.all_votes("money") if v["spec"]["action"] == "refund")
        self.w.operator_votes(refund["poll_id"], "1001", "yes")
        self.tick("votes", "money", "draft")
        self.assertEqual(self.all_votes("reply"), [], "the cancel is still unverified")

    def test_reply_that_claims_more_than_it_proposes_is_held_before_any_vote(self):
        body = "Hi Pat,\n\nWe have refunded you and cancelled your subscription.\n\nSupport"
        self.refund_case(body=body)
        self.assertEqual(self.all_votes(), [], "a cancel claim with only a refund proposed opens nothing")
        self.assertIn("cancel", self.posts())

    def test_payment_id_in_the_reply_part_is_held_before_any_vote(self):
        self.refund_case(body="Hi Pat,\n\nCharge ch_ABCDEFG123 was refunded.\n\nSupport")
        self.assertEqual(self.all_votes(), [])

    def test_customer_writes_while_money_pending_keeps_the_money_vote(self):
        self.refund_case()
        m = self.live_vote("money")
        self.ctx.clock.advance(minutes=5)
        self.write_in(subject="Charged twice", body="any news?")
        self.tick("intake")
        self.assertEqual(votes.load(self.ctx)[m["key"]]["status"], votes.OPEN,
                         "a follow-up does not cancel the operator's money decision")


    def _count_draft_calls(self, passes=5):
        calls = []
        orig = self.ctx.drafter.draft
        self.ctx.drafter.draft = lambda *a: calls.append(1) or orig(*a)
        before = self.posts().count("Draft held")
        for _ in range(passes):
            self.tick("draft")
        return len(calls), self.posts().count("Draft held") - before

    def test_held_money_draft_waits_for_a_person(self):
        """QA 13 Oct 2026: a held draft was redrafted on every pass (one paid AI call + one held note a minute)."""
        self.refund_case(spec={**REFUND, "amount": 999999})
        self.assertEqual(self._count_draft_calls(), (0, 0))

    def test_held_payments_off_waits_for_a_person(self):
        self.ctx.payments = None
        self.refund_case()
        self.assertEqual(self._count_draft_calls(), (0, 0))

    def test_held_plain_reply_waits_for_a_person(self):
        self.write_in(subject="Charged twice")
        self.drafter_says("Hi Pat,\n\nI have refunded you in full.")   # claims money with no receipt -> held
        self.tick("intake", "draft")
        self.assertIn("held", self.posts())
        self.assertEqual(self._count_draft_calls(), (0, 0))

    def test_held_case_redrafts_when_the_customer_writes_again(self):
        self.refund_case(spec={**REFUND, "amount": 999999})
        self.drafter_says("Hi Pat,\n\nThanks, we are looking into it.")
        self.write_in(subject="Charged twice", body="Any news?")
        self.tick("intake", "draft")
        self.assertEqual(len(self.all_votes("reply")), 1, "a new customer message reopens drafting")

    def test_held_case_redrafts_when_a_person_asks(self):
        self.refund_case(spec={**REFUND, "amount": 999999})
        self.drafter_says("Hi Pat,\n\nThanks, we are looking into it.")
        key = next(iter(cases.load(self.ctx)))
        cases.update(self.ctx, key, needs_draft=True)          # what Sayso Cloud's "Write a new draft" does
        self.tick("draft")
        self.assertEqual(len(self.all_votes("reply")), 1)


class LockTests(Base):
    def test_run_waits_for_a_busy_lock_instead_of_skipping(self):
        from sayso import cli
        lock = self.ctx.paths.locks / "close.run"
        held, released = threading.Event(), threading.Event()

        def hold():
            with single_flight(lock) as mine:
                self.assertTrue(mine)
                held.set()
                time.sleep(0.6)
            released.set()
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(held.wait(10), "holder never took the lock")   # not a fixed sleep: flaky under load
        ran = cli.run_job_locked(self.ctx, "close", lambda ctx: {"ran": True}, wait_seconds=5)
        t.join()
        self.assertTrue(released.is_set())
        self.assertEqual(ran, {"ran": True})

    def test_still_busy_after_the_wait_is_recorded_not_silent(self):
        from sayso import cli
        lock = self.ctx.paths.locks / "close.run"
        with single_flight(lock):
            ran = cli.run_job_locked(self.ctx, "close", lambda ctx: {"ran": True}, wait_seconds=0.2)
        self.assertIsNone(ran)
        self.assertTrue(any(e.get("event") == "job_skipped_busy" and e.get("job") == "close"
                            for e in self.ctx.journal.read()))

    def test_health_watches_every_enabled_job(self):
        self.tick()
        self.ctx.clock.advance(hours=3)
        for job in ("intake", "draft", "votes", "sender", "close", "reconcile", "health", "release", "qa", "dev"):
            self.ctx.beat(job, "ok")
        report = health.check(self.ctx)
        self.assertIn("money", report["stale"], "a stopped money job must be noticed")
        self.assertIn("stall", report["stale"])

    def test_health_ignores_jobs_whose_adapter_is_off(self):
        self.ctx.payments = None
        self.tick()
        self.ctx.clock.advance(hours=3)
        report = health.check(self.ctx)
        self.assertNotIn("money", report["stale"] + report["never"])


class ReopenTests(Base):
    def test_reopened_case_new_reply_vote_survives_the_old_close(self):
        self.write_in()
        self.tick("intake", "draft")
        self.vote("reply")
        self.tick("votes", "sender")
        self.ctx.clock.advance(days=4)
        self.tick("close")
        self.vote("close")
        self.tick("votes", "close")
        self.assertEqual(self.tags(self.card()), [stages.DONE])
        self.ctx.clock.advance(hours=1)
        self.write_in(body="it broke again")
        self.tick("intake", "draft")
        new = self.live_vote("reply")
        for _ in range(3):
            self.ctx.clock.advance(minutes=1)
            self.tick("votes", "close", "reconcile", "stall")
        self.assertEqual(votes.load(self.ctx)[new["key"]]["status"], votes.OPEN)
        self.assertEqual(self.stage(), stages.AWAITING)
