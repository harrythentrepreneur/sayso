"""Workflow behaviour on fakes, including every seeded fault the loop must refuse."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from sayso import cases, demo, runtime, safety, stages, votes
from sayso.jobs import dev, money_job, sender
from sayso.plugins import reply_rules
from tests.helpers import OPERATOR, STRANGER, LoopTest

REFUND = {"action": "refund", "charge": "ch_test0001", "amount": 1900, "currency": "usd", "customer": "cus_test0001"}


class IntakeTests(LoopTest):
    def test_new_mail_opens_card_with_verbatim_email(self):
        self.write_in(body="Exact customer words here.")
        self.tick("intake")
        key, rec = self.only_case()
        msgs = self.ctx.board.messages(rec["card_id"])
        self.assertTrue(msgs[0].startswith("**Customer wrote**"))
        self.assertIn("Exact customer words here.", msgs[0])
        self.assertEqual(self.tags(rec["card_id"]), ["In support"])

    def test_second_email_same_card(self):
        self.write_in()
        self.tick("intake")
        self.write_in(sender="PAT@customer.test ", body="any update?")
        self.tick("intake")
        self.only_case()

    def test_own_address_never_a_customer(self):
        self.write_in(sender="support@example.com")
        self.tick("intake")
        self.assertEqual(cases.load(self.ctx), {})

    def test_new_mail_voids_pending_reply_and_reopens(self):
        self.write_in()
        self.tick("intake", "draft")
        v = self.live_vote("reply")
        self.w.operator_votes(v["poll_id"], OPERATOR, "yes")
        self.write_in(body="Actually, also this other thing.")
        self.tick("intake", "votes", "sender")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(self.w.data["outbox"], [], "a voided reply must never be sent")

    def test_intake_idempotent(self):
        self.write_in()
        self.tick("intake")
        self.ctx.paths.cursors.unlink()   # simulate a lost cursor: replays the whole inbox
        self.tick("intake")
        key, rec = self.only_case()
        self.assertEqual(len(self.ctx.board.messages(rec["card_id"])), 1)


class SenderTests(LoopTest):
    def approved_reply(self):
        self.write_in()
        self.tick("intake", "draft")
        v = self.vote("reply")
        self.tick("votes")
        return v

    def test_happy_path_sends_once_and_verifies(self):
        v = self.approved_reply()
        self.tick("sender")
        self.tick("sender")
        self.assertEqual(len(self.w.data["outbox"]), 1)
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)
        self.assertEqual(self.tags(v["card_id"]), ["Done", "Waiting on customer"])

    def test_unapproved_never_sent(self):
        self.write_in()
        self.tick("intake", "draft", "votes", "sender")
        self.assertEqual(self.w.data["outbox"], [])

    def test_stranger_yes_never_sends(self):
        self.write_in()
        self.tick("intake", "draft")
        self.vote("reply", user=STRANGER)
        self.tick("votes", "sender")
        self.assertEqual(self.w.data["outbox"], [])

    def test_operator_no_never_sends(self):
        self.write_in()
        self.tick("intake", "draft")
        self.vote("reply", answer="no")
        self.tick("votes", "sender")
        self.assertEqual(self.w.data["outbox"], [])

    def test_edited_draft_after_approval_is_void(self):
        v = self.approved_reply()
        Path(v["spec"]["path"]).write_text("Something else entirely.\n")
        self.tick("sender")
        self.assertEqual(self.w.data["outbox"], [])
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)

    def test_edit_before_tally_voids(self):
        self.write_in()
        self.tick("intake", "draft")
        v = self.vote("reply")
        Path(v["spec"]["path"]).write_text("changed\n")
        self.tick("votes")  # tally alone must void; it must not record an approval
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.tick("sender")
        self.assertEqual(self.w.data["outbox"], [])

    def test_pending_attempt_is_never_resent(self):
        """A crash left a pending attempt: prove, never send again (real providers may not dedupe)."""
        v = self.approved_reply()
        from sayso.store import save_json
        save_json(self.ctx.paths.attempts, {v["key"]: {"status": "pending", "started_at": "2030-01-06T09:00:00+00:00"}})
        self.tick("sender")
        self.assertEqual(self.w.data["outbox"], [], "an unresolved attempt must only be re-read")
        from sayso.store import load_json
        self.assertEqual(load_json(self.ctx.paths.attempts)[v["key"]]["status"], sender.UNVERIFIED)

    def test_receipt_rules(self):
        """Receipt needs Sent status, exact recipient, our sender and one mailbox copy."""
        from sayso.adapters.base import SentRecord

        class Desk:
            rows = []
            def sent_readback(self, ticket, body_sha):
                return self.rows

        class Box:
            n = 1
            def sent_copies(self, mid):
                return self.n

        self.ctx.helpdesk, self.ctx.mailbox = Desk(), Box()
        good = dict(ticket="T1", recipient="pat@customer.test", sender="support@example.com", subject="s",
                    body_sha="x", message_id="<m>", status="Sent")
        Desk.rows = [SentRecord(**good)]
        self.assertIsNotNone(sender.check_receipt(self.ctx, "T1", "pat@customer.test", "x"))
        for field, bad in (("status", "Queued"), ("recipient", "other@customer.test"),
                           ("sender", "someone@else.test")):
            Desk.rows = [SentRecord(**{**good, field: bad})]
            self.assertIsNone(sender.check_receipt(self.ctx, "T1", "pat@customer.test", "x"), field)
        Desk.rows = [SentRecord(**good), SentRecord(**good)]
        self.assertIsNone(sender.check_receipt(self.ctx, "T1", "pat@customer.test", "x"), "two sends")
        Desk.rows, Box.n = [SentRecord(**good)], 2
        self.assertIsNone(sender.check_receipt(self.ctx, "T1", "pat@customer.test", "x"), "two copies")

    def test_crash_after_write_does_not_resend(self):
        self.w.data["faults"] = ["send_raises_after_write"]
        self.w.save()
        v = self.approved_reply()
        self.tick("sender")
        self.tick("sender")
        self.assertEqual(len(self.w.data["outbox"]), 1)
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)

    def test_not_delivered_is_unverified_and_alerts_once(self):
        self.w.data["faults"] = ["send_not_delivered"]
        self.w.save()
        v = self.approved_reply()
        self.tick("sender")
        self.tick("sender")
        self.assertEqual(len(self.w.data["outbox"]), 1, "never re-sent")
        self.assertNotEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)
        self.assertEqual(sum("not proven sent" in a for a in self.w.data["alerts"]), 1)
        self.assertNotIn("Waiting on customer", self.tags(v["card_id"]))

    def test_missing_mailbox_copy_is_unverified(self):
        self.w.data["faults"] = ["mailbox_missing"]
        self.w.save()
        v = self.approved_reply()
        self.tick("sender")
        self.assertNotEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)

    def test_money_claim_without_receipt_held_before_vote(self):
        self.w.data["next_draft"] = "Hi, we have refunded your payment. Thanks."
        self.w.save()
        self.write_in()
        self.tick("intake", "draft")
        self.assertEqual(votes.load(self.ctx), {}, "no vote on a reply the sender would refuse")
        key, rec = self.only_case()
        self.assertEqual(stages.stage_of(self.tags(rec["card_id"])), stages.IN_SUPPORT)
        self.assertTrue(any("Draft held" in m and "refund" in m for m in self.ctx.board.messages(rec["card_id"])))

    def test_rule_added_after_approval_still_blocks_send(self):
        v = self.approved_reply()
        self.ctx.plugins = [reply_rules.make({"forbid": {"thanks": "thanks for writing"}})]
        out = self.tick("sender")["sender"]
        self.assertEqual(list(out.values()), ["refused"])
        self.assertEqual(self.w.data["outbox"], [])

    def test_plugin_rule_held_before_vote(self):
        self.ctx.plugins = [reply_rules.make({"forbid": {"thanks": "thanks for writing"}})]
        self.write_in()
        self.tick("intake", "draft")
        self.assertEqual(votes.load(self.ctx), {})
        key, rec = self.only_case()
        self.assertTrue(any("'thanks' rule" in m for m in self.ctx.board.messages(rec["card_id"])))

    def test_subject_has_exactly_one_re(self):
        self.write_in(subject="RE: re:  Export button does nothing")
        self.tick("intake", "draft")
        self.vote("reply")
        self.tick("votes", "sender")
        self.assertEqual([r["subject"] for r in self.w.data["outbox"]], ["Re: Export button does nothing"])

    def test_payment_id_in_draft_held_before_vote(self):
        self.w.data["next_draft"] = "Your charge ch_ABCDEFG123 was looked at."
        self.w.save()
        self.write_in()
        self.tick("intake", "draft")
        self.assertEqual(votes.load(self.ctx), {})

    def test_card_moved_off_awaiting_blocks_send(self):
        v = self.approved_reply()
        cases.set_tags_verified(self.ctx, v["card_id"], ["Blocked"])
        self.tick("sender")
        self.assertEqual(self.w.data["outbox"], [])

    def test_paused_sends_nothing(self):
        self.approved_reply()
        self.ctx.paths.pause_flag.write_text("x")
        with self.assertRaises(runtime.Paused):
            sender.run(self.ctx, sleep=lambda s: None)
        self.assertEqual(self.w.data["outbox"], [])


class MoneyTests(LoopTest):
    def setup_case(self):
        self.write_in()
        self.tick("intake")
        return self.only_case()

    def test_over_cap_refused_at_request(self):
        key, _ = self.setup_case()
        with self.assertRaises(Exception):
            money_job.request(self.ctx, case_key=key, subject="Pat - charged twice",
                              spec={**REFUND, "amount": 999999})

    def test_unapproved_never_refunds(self):
        key, _ = self.setup_case()
        money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.tick("votes", "money")
        self.assertEqual(self.w.data["refunds"], [])

    def test_approved_refund_once_and_verified(self):
        key, _ = self.setup_case()
        money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.vote("money")
        self.tick("votes", "money")
        self.tick("money")
        self.assertEqual(len(self.w.data["refunds"]), 1)

    def test_crash_after_refund_never_repeats(self):
        self.w.data["faults"] = ["refund_raises_after_write"]
        self.w.save()
        key, _ = self.setup_case()
        money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.vote("money")
        self.tick("votes", "money", "money")
        self.assertEqual(len(self.w.data["refunds"]), 1)

    def test_pending_money_attempt_never_reexecuted(self):
        key, _ = self.setup_case()
        v = money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.vote("money")
        self.tick("votes")
        from sayso.store import save_json
        save_json(self.ctx.paths.attempts, {v["key"]: {"status": "pending"}})
        self.tick("money")
        self.assertEqual(self.w.data["refunds"], [], "a pending attempt is only verified, never repeated")

    def test_wrong_amount_is_unverified(self):
        self.w.data["faults"] = ["refund_wrong_amount"]
        self.w.save()
        key, _ = self.setup_case()
        v = money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.vote("money")
        self.tick("votes", "money")
        self.assertNotEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)
        self.assertTrue(any("could not be verified" in a for a in self.w.data["alerts"]))

    def test_tampered_spec_voids(self):
        key, _ = self.setup_case()
        v = money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=REFUND)
        self.vote("money")
        self.tick("votes")
        from sayso.store import load_json, save_json
        allv = load_json(self.ctx.paths.votes)
        allv[v["key"]]["spec"]["amount"] = 1800
        save_json(self.ctx.paths.votes, allv)
        self.tick("money")
        self.assertEqual(self.w.data["refunds"], [])


class DevAndReleaseTests(LoopTest):
    def to_pr(self, head="b" * 40):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        run_id = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(run_id, pr=7, head=head)
        self.tick("dev", "qa")
        self.ctx.qa_runner.finish(cases.load(self.ctx)[key]["qa"]["run"],
                                  f"CUSTOMER-FIX: YES fixed\nVERDICT: PASS {head}")
        self.tick("qa")
        return key, rec

    def test_brief_carries_limits(self):
        key, rec = self.to_pr()
        run = next(iter(self.w.data["runs"].values()))
        for limit in dev.REQUIRED_BOUNDARY:
            self.assertIn(limit, run["brief"])

    def test_brief_without_limits_refused(self):
        with self.assertRaises(dev.BriefUnsafe):
            dev.assert_brief_safe("please fix it")

    def test_one_run_per_card_across_restarts(self):
        self.write_in()
        self.tick("intake")
        key, _ = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 1)

    def test_merge_needs_vote(self):
        self.to_pr()
        self.tick("votes", "release")
        self.assertEqual(self.ctx.codehost.pull_request(7).state, "open")

    def test_moved_head_voids_merge(self):
        self.to_pr()
        self.vote("merge")
        self.w.data["prs"]["7"]["head"] = "c" * 40
        self.w.save()
        self.tick("votes", "release")
        self.assertEqual(self.ctx.codehost.pull_request(7).state, "open")

    def test_head_moved_after_tally_still_refused(self):
        self.to_pr()
        v = self.vote("merge")
        self.tick("votes")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.APPROVED)
        self.w.data["prs"]["7"]["head"] = "c" * 40
        self.w.save()
        self.tick("release")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(self.ctx.codehost.pull_request(7).state, "open")

    def test_merge_ignored_alerts(self):
        self.w.data["faults"] = ["merge_ignored"]
        self.w.save()
        self.to_pr()
        self.vote("merge")
        self.tick("votes", "release")
        self.assertTrue(any("reads back as open" in a for a in self.w.data["alerts"]))


class CloseTests(LoopTest):
    def sent_case(self):
        self.write_in()
        self.tick("intake", "draft")
        v = self.vote("reply")
        self.tick("votes", "sender")
        return v["case_key"], v["card_id"]

    def test_quiet_close_after_n_days(self):
        key, card = self.sent_case()
        self.ctx.clock.advance(days=2)
        self.tick("close")
        self.assertEqual([v for v in votes.load(self.ctx).values() if v["kind"] == "close"], [])
        self.ctx.clock.advance(days=2)
        self.tick("close")
        self.vote("close")
        self.tick("votes", "close")
        self.assertEqual(self.tags(card), ["Done"])

    def test_customer_writes_voids_close(self):
        key, card = self.sent_case()
        self.ctx.clock.advance(days=4)
        self.tick("close")
        v = self.vote("close")
        self.write_in(body="wait, still broken")
        self.tick("intake", "votes", "close")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(self.tags(card), ["In support"])

    def test_reply_seen_by_close_before_intake(self):
        """The customer wrote but intake has not run yet: close must still refuse."""
        key, card = self.sent_case()
        self.ctx.clock.advance(days=4)
        self.tick("close")
        v = self.vote("close")
        self.tick("votes")
        self.ctx.clock.advance(minutes=1)
        self.write_in(body="still broken")
        self.tick("close")
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.VOID)
        self.assertEqual(self.tags(card), ["Done", "Waiting on customer"])

    def test_close_asked_once(self):
        self.sent_case()
        self.ctx.clock.advance(days=4)
        self.tick("close")
        v = self.vote("close", answer="no")
        self.tick("votes", "close", "close")
        self.assertEqual(len([x for x in votes.load(self.ctx).values() if x["kind"] == "close"]), 1)


class GuardTests(LoopTest):
    def test_tag_readback_catches_ignored_write(self):
        self.w.data["faults"] = ["tags_ignored"]
        self.w.save()
        self.write_in()
        with self.assertRaises(cases.CaseError):
            self.tick("intake")

    def test_reconciler_reports_unverified_and_never_repairs(self):
        self.w.data["faults"] = ["send_not_delivered"]
        self.w.save()
        self.write_in()
        self.tick("intake", "draft")
        self.vote("reply")
        self.tick("votes", "sender")
        before = self.w.data["outbox"][:]
        found = self.tick("reconcile")["reconcile"]["findings"]
        self.assertTrue(any(f.startswith("unverified-attempt:") for f in found))
        self.assertEqual(self.w.data["outbox"], before)

    def test_health_flags_stale_job(self):
        self.tick()
        self.ctx.clock.advance(hours=2)
        self.ctx.beat("health", "x")
        from sayso.jobs import health
        self.assertIn("intake", health.check(self.ctx)["stale"])

    def test_unmatched_failure_never_groups(self):
        from sayso.plugins.product_failure import make
        plugin = make({"patterns": {"quota": "out of credit"}})
        self.ctx.plugins = [plugin]
        self.w.customer_writes(sender="a@x.test", subject="fail", body="weird error 1")
        self.w.customer_writes(sender="a@x.test", subject="fail", body="another weird error")
        self.w.customer_writes(sender="a@x.test", subject="fail", body="Out of credit")
        self.w.customer_writes(sender="a@x.test", subject="fail", body="out of credit again")
        self.tick("intake")
        kinds = sorted(r["kind"] for r in cases.load(self.ctx).values())
        self.assertEqual(kinds, ["failure:quota", "unmatched", "unmatched"])


class EndToEnd(unittest.TestCase):
    def test_demo_first_email_to_close(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ctx = demo.build(Path(d))
            result = demo.scenario(ctx, say=lambda *_: None)
            self.assertEqual(result["final_tags"], ["Done"])
            self.assertEqual(len(result["outbox"]), 1)
            self.assertEqual(result["outbox"][0]["status"], "Sent")
            self.assertEqual(len(result["refunds"]), 1)
            self.assertEqual(result["alerts"], [])
            body_sha = result["outbox"][0]["body_sha"]
            draft = next(Path(d).rglob("reply-v2.txt")).read_text()
            self.assertEqual(hashlib.sha256(draft.encode()).hexdigest(), body_sha)
            self.assertIn("refunded", draft)  # allowed only because the refund receipt exists


class SafetyTextTests(unittest.TestCase):
    def test_reply_subject(self):
        self.assertEqual(safety.reply_subject("Hello"), "Re: Hello")
        self.assertEqual(safety.reply_subject("Re: Hello"), "Re: Hello")
        self.assertEqual(safety.reply_subject("RE:Re: Hello"), "Re: Hello")
        self.assertEqual(safety.reply_subject("Rebate question"), "Re: Rebate question")
        self.assertEqual(safety.reply_subject(""), "Re: (no subject)")

    def test_future_money_is_not_a_claim(self):
        self.assertEqual(safety.money_claims("Once approved, we will issue a refund."), set())
        self.assertEqual(safety.money_claims("We have refunded your payment."), {"refund"})
        self.assertEqual(safety.money_claims("Your subscription has been cancelled."), {"cancel"})

    def test_card_numbers_refused(self):
        with self.assertRaises(safety.Refused):
            safety.check_no_payment_identifiers("card 4242 4242 4242 4242")


if __name__ == "__main__":
    unittest.main()
