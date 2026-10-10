"""Stall checks: remind about open votes and flag cases nobody is working on.

Tests drive the real jobs on fakes with a movable clock and assert what landed:
which notices went out, to whom, how often, and that nothing else changed.
"""
from __future__ import annotations

import dataclasses

from sayso import cases, stages, votes
from sayso.jobs import dev
from tests.helpers import LoopTest

HARRY, KAVIRU = "1001", "1002"


class StallLoop(LoopTest):
    def setUp(self):
        super().setUp()
        self.ctx.config = dataclasses.replace(self.ctx.config, operators={HARRY: "Harry", KAVIRU: "Kaviru"})

    def policy(self, **kw):
        self.ctx.config = dataclasses.replace(self.ctx.config,
                                              policy=dataclasses.replace(self.ctx.config.policy, **kw))

    def notices(self, word=""):
        return [n for n in self.w.data.get("notices", []) if word in n["text"]]

    def later(self, **delta):
        self.ctx.clock.advance(**delta)

    def awaiting_case(self):
        self.write_in()
        self.tick("intake", "draft")
        return self.only_case()


class VoteReminderTests(StallLoop):
    def test_no_reminder_before_the_wait(self):
        self.awaiting_case()
        self.later(minutes=29)
        self.tick("stall")
        self.assertEqual(self.notices("still waiting"), [])

    def test_one_reminder_after_30_minutes_then_daily(self):
        self.awaiting_case()
        self.later(minutes=31)
        self.tick("stall", "stall")
        sent = [e for e in self.ctx.journal.read() if e.get("event") == "notice" and "stall:vote" in e.get("key", "")]
        self.assertEqual(len(sent), 1, "the loop itself must not re-send; the board dedupe is a backstop")
        self.assertEqual(len(self.notices("still waiting")), 1, "once, not every pass")
        self.later(hours=5)
        self.tick("stall")
        self.assertEqual(len(self.notices("still waiting")), 1, "no repeat inside a day")
        self.later(hours=20)
        self.tick("stall")
        self.assertEqual(len(self.notices("still waiting")), 2, "one more after a day")

    def test_reminder_pings_only_the_owner(self):
        key, rec = self.awaiting_case()
        cases.update(self.ctx, key, owner=KAVIRU, owner_source="override")
        self.later(minutes=31)
        self.tick("stall")
        (n,) = self.notices("still waiting")
        self.assertEqual(n["pings"], [KAVIRU])
        self.assertIn("For Kaviru", n["text"])

    def test_vote_for_anyone_pings_nobody(self):
        self.awaiting_case()
        self.later(minutes=31)
        self.tick("stall")
        self.assertEqual(self.notices("still waiting")[0]["pings"], [])

    def test_decided_vote_gets_no_reminder(self):
        self.awaiting_case()
        self.vote("reply", user=HARRY)
        self.tick("votes")
        self.later(minutes=31)
        self.tick("stall")
        self.assertEqual(self.notices("still waiting"), [])

    def test_notices_never_contain_the_customer_address(self):
        key, rec = self.awaiting_case()
        self.later(minutes=31)
        self.tick("stall")
        v = self.live_vote("reply")
        votes.set_status(self.ctx, v["key"], votes.VOID, void_reason="test")
        self.later(days=1)
        self.tick("stall")
        texts = [n["text"] for n in self.notices()]
        self.assertTrue(self.notices("nothing is moving") and self.notices("no activity"))
        self.assertFalse([t for t in texts if "@customer.test" in t], texts)

    def test_reminder_changes_nothing_else(self):
        key, rec = self.awaiting_case()
        before_votes, before_tags = votes.load(self.ctx), self.tags(rec["card_id"])
        self.later(minutes=31)
        self.tick("stall")
        self.assertEqual(votes.load(self.ctx), before_votes)
        self.assertEqual(self.tags(rec["card_id"]), before_tags)


class StallTests(StallLoop):
    def blocked_case(self):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        cases.move(self.ctx, rec["card_id"], stages.BLOCKED)
        return key, rec

    def test_blocked_card_flagged_after_its_limit_once_per_day(self):
        self.blocked_case()
        self.later(hours=3, minutes=59)
        self.tick("stall")
        self.assertEqual(self.notices("no activity"), [])
        self.later(minutes=2)
        self.tick("stall", "stall")
        self.assertEqual(len(self.notices("no activity")), 1)
        self.later(hours=23)
        self.tick("stall")
        self.assertEqual(len(self.notices("no activity")), 1)
        self.later(hours=2)
        self.tick("stall")
        self.assertEqual(len(self.notices("no activity")), 2)

    def test_new_activity_resets_the_clock(self):
        key, rec = self.blocked_case()
        self.later(hours=3)
        self.ctx.board.post(rec["card_id"], "Looking at this now.", idempotency_key="human-1")
        self.later(hours=2)
        self.tick("stall")
        self.assertEqual(self.notices("no activity"), [])

    def test_a_new_quiet_stretch_is_flagged_again_inside_the_day(self):
        key, rec = self.blocked_case()
        self.later(hours=5)
        self.tick("stall")
        self.ctx.board.post(rec["card_id"], "Tried again, still stuck.", idempotency_key="human-2")
        self.later(hours=5)
        self.tick("stall")
        self.assertEqual(len(self.notices("no activity")), 2, "quiet again after new activity is a new stall")

    def test_parked_card_is_never_flagged(self):
        key, rec = self.blocked_case()
        cases.set_tags_verified(self.ctx, rec["card_id"], [stages.BLOCKED, "Parked"])
        self.later(days=3)
        self.tick("stall")
        self.assertEqual(self.notices("no activity"), [])

    def test_done_and_waiting_cards_have_no_clock(self):
        key, rec = self.blocked_case()
        cases.set_tags_verified(self.ctx, rec["card_id"], [stages.DONE, stages.WAITING])
        self.later(days=3)
        self.tick("stall")
        self.assertEqual(self.notices(), [])

    def test_unreadable_activity_is_reported_not_guessed(self):
        key, rec = self.blocked_case()
        self.w.data["faults"] = ["activity_unreadable"]
        self.w.save()
        self.later(days=1)
        self.tick("stall")
        self.assertEqual(self.notices("no activity"), [])
        self.assertTrue(any("cannot read" in a for a in self.w.data["alerts"]))

    def test_stall_pings_the_owner(self):
        key, rec = self.blocked_case()
        cases.update(self.ctx, key, owner=HARRY, owner_source="override")
        self.later(hours=5)
        self.tick("stall")
        self.assertEqual(self.notices("no activity")[0]["pings"], [HARRY])

    def test_limits_come_from_settings(self):
        self.policy(stall_blocked_hours=1)
        self.blocked_case()
        self.later(minutes=61)
        self.tick("stall")
        self.assertEqual(len(self.notices("no activity")), 1)


class OrphanTests(StallLoop):
    def test_awaiting_approval_with_no_open_vote_is_flagged(self):
        key, rec = self.awaiting_case()
        v = self.live_vote("reply")
        votes.set_status(self.ctx, v["key"], votes.VOID, void_reason="test")
        self.later(minutes=16)
        self.tick("stall", "stall")
        found = self.notices("nothing is moving")
        self.assertEqual(len(found), 1)
        self.assertIn("no vote is open", found[0]["text"])

    def test_in_dev_without_a_dev_runner_is_flagged(self):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.ctx.dev_runner = None
        self.later(minutes=16)
        self.tick("stall")
        self.assertEqual(len(self.notices("nothing is moving")), 1)

    def test_in_dev_with_a_running_job_is_not_flagged(self):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        self.later(minutes=16)
        self.tick("stall")
        self.assertEqual(self.notices("nothing is moving"), [])

    def test_in_dev_waiting_for_a_free_slot_is_moving(self):
        self.policy(max_parallel_dev_runs=1)
        for who in ("pat", "sam"):
            self.write_in(sender=f"{who}@customer.test", subject=f"Export broken for {who}")
            self.tick("intake")
        for key in list(cases.load(self.ctx)):
            dev.request(self.ctx, key)
        self.tick("dev")
        self.later(minutes=16)
        self.tick("stall")
        self.assertEqual(self.notices("nothing is moving"), [])

    def test_in_support_after_a_draft_with_no_vote_is_flagged(self):
        key, rec = self.awaiting_case()
        v = self.live_vote("reply")
        votes.set_status(self.ctx, v["key"], votes.VOID, void_reason="test")
        cases.move(self.ctx, rec["card_id"], stages.IN_SUPPORT)
        self.later(minutes=16)
        self.tick("stall")
        self.assertIn("no draft is due and no vote is open", self.notices("nothing is moving")[0]["text"])

    def test_open_vote_keeps_a_support_card_moving(self):
        key, rec = self.awaiting_case()
        cases.move(self.ctx, rec["card_id"], stages.IN_SUPPORT)
        cases.update(self.ctx, key, needs_draft=False)
        self.later(minutes=16)
        self.tick("stall")
        self.assertEqual(self.notices("nothing is moving"), [])

    def test_orphan_waits_its_quiet_time(self):
        key, rec = self.awaiting_case()
        v = self.live_vote("reply")
        votes.set_status(self.ctx, v["key"], votes.VOID, void_reason="test")
        self.later(minutes=14)
        self.tick("stall")
        self.assertEqual(self.notices("nothing is moving"), [])

    def test_paused_loop_sends_nothing(self):
        self.awaiting_case()
        before = list(self.notices())
        self.ctx.paths.pause_flag.write_text("x")
        self.later(hours=2)
        out = self.tick("stall")["stall"]
        self.assertEqual(self.notices(), before)
        self.assertTrue(out.get("paused"))
