"""Board tidy (opt-in, off by default): archive finished cards so the board shows working cases.

Archiving hides a card; it never locks it, deletes a message or changes a stage.
Every test drives the real jobs on fakes and asserts the board state.
"""
from __future__ import annotations

import dataclasses

from sayso import cases, stages, votes
from sayso.config import ConfigError, Policy, validate_tidy
from sayso.jobs import health
from tests.helpers import LoopTest


class TidyLoop(LoopTest):
    def policy(self, **kw):
        self.ctx.config = dataclasses.replace(self.ctx.config,
                                              policy=dataclasses.replace(self.ctx.config.policy, **kw))

    def on(self, **kw):
        self.policy(archive_finished=True, **kw)

    def archived(self, card):
        return bool(self.w.data["cards"][card].get("archived"))

    def card(self):
        return self.only_case()[1]["card_id"]

    def sent_case(self):
        """A reply sent and verified: the card is Done + Waiting on customer."""
        self.write_in()
        self.tick("intake", "draft")
        self.vote("reply")
        self.tick("votes", "sender")
        return self.card()

    def closed_case(self):
        card = self.sent_case()
        self.ctx.clock.advance(days=4)
        self.tick("close")
        self.vote("close")
        self.tick("votes", "close")
        self.assertEqual(self.tags(card), [stages.DONE])
        return card


class OffByDefaultTests(TidyLoop):
    def test_default_is_off(self):
        self.assertFalse(Policy().archive_finished)

    def test_off_archives_nothing(self):
        card = self.closed_case()
        self.ctx.clock.advance(days=30)
        out = self.tick("tidy")["tidy"]
        self.assertFalse(self.archived(card))
        self.assertTrue(out.get("off"))


class ArchiveTests(TidyLoop):
    def test_done_card_archived_after_quiet_hour(self):
        self.on()
        card = self.closed_case()
        self.ctx.clock.advance(minutes=59)
        self.tick("tidy")
        self.assertFalse(self.archived(card), "activity in the last hour keeps it")
        self.ctx.clock.advance(minutes=2)
        self.tick("tidy")
        self.assertTrue(self.archived(card))

    def test_archive_keeps_stage_and_messages(self):
        self.on()
        card = self.closed_case()
        before = (self.tags(card), list(self.ctx.board.messages(card)))
        self.ctx.clock.advance(hours=2)
        self.tick("tidy")
        self.assertEqual((self.tags(card), list(self.ctx.board.messages(card))), before)
        self.assertFalse(self.w.data["cards"][card].get("locked"))

    def test_waiting_card_archived_only_after_its_days(self):
        self.on(archive_waiting_days=7)
        card = self.sent_case()
        self.ctx.clock.advance(days=6, hours=23)
        self.tick("tidy")
        self.assertFalse(self.archived(card))
        self.ctx.clock.advance(hours=2)
        self.tick("tidy")
        self.assertTrue(self.archived(card))

    def test_open_vote_keeps_the_card(self):
        self.on()
        card = self.sent_case()
        self.ctx.clock.advance(days=4)
        self.tick("close")
        self.ctx.clock.advance(days=10)
        self.tick("tidy")
        self.assertFalse(self.archived(card), "an open close vote needs the card visible")

    def test_working_stages_are_never_archived(self):
        self.on()
        self.write_in()
        self.tick("intake")
        card = self.card()
        for stage in (stages.IN_SUPPORT, stages.BLOCKED):
            cases.set_tags_verified(self.ctx, card, [stage])
            self.ctx.clock.advance(days=30)
            self.tick("tidy")
            self.assertFalse(self.archived(card), stage)

    def test_parked_card_is_left_alone(self):
        self.on()
        card = self.closed_case()
        cases.set_tags_verified(self.ctx, card, [stages.DONE, "Parked"])
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        self.assertFalse(self.archived(card))

    def test_unreadable_activity_never_archives(self):
        self.on()
        card = self.closed_case()
        self.w.data["faults"] = ["activity_unreadable"]
        self.w.save()
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        self.assertFalse(self.archived(card))

    def test_archive_that_did_not_land_is_reported(self):
        self.on()
        card = self.closed_case()
        self.w.data["faults"] = ["archive_ignored"]
        self.w.save()
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        self.assertTrue(any("did not archive" in a for a in self.w.data["alerts"]))

    def test_archiving_twice_is_harmless(self):
        self.on()
        card = self.closed_case()
        self.ctx.clock.advance(days=2)
        self.tick("tidy", "tidy")
        self.assertTrue(self.archived(card))
        self.assertEqual(self.w.data["cards"][card].get("archive_writes"), 1)

    def test_paused_loop_archives_nothing(self):
        self.on()
        card = self.closed_case()
        self.ctx.paths.pause_flag.write_text("x")
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        self.assertFalse(self.archived(card))


class ReopenTests(TidyLoop):
    def test_new_email_brings_an_archived_card_back(self):
        self.on()
        card = self.closed_case()
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        self.write_in(body="it broke again")
        self.tick("intake")
        self.assertFalse(self.archived(card))
        self.assertEqual(self.tags(card), [stages.IN_SUPPORT])

    def test_any_stage_move_unarchives_first(self):
        self.on()
        card = self.closed_case()
        self.ctx.clock.advance(days=2)
        self.tick("tidy")
        cases.move(self.ctx, card, stages.IN_SUPPORT)
        self.assertFalse(self.archived(card))


class StaleVoteTests(TidyLoop):
    def test_vote_opened_before_the_close_does_not_pin_the_card(self):
        self.on()
        card = self.closed_case()
        key = self.only_case()[0]
        # A vote left live from before the case closed (opened by hand earlier, never acted on).
        self.ctx.clock.advance(minutes=1)
        closed_at = self.ctx.clock.now()
        from sayso.store import load_json, save_json
        all_votes = load_json(self.ctx.paths.votes)
        all_votes["money:old"] = {"key": "money:old", "kind": "money", "case_key": key, "card_id": card,
                                  "poll_id": None, "subject": "Pat - old question", "question": "refund?",
                                  "identity": {}, "fingerprint": "x", "spec": {}, "status": votes.APPROVED,
                                  "opened_at": (closed_at - __import__("datetime").timedelta(days=5)).isoformat()}
        save_json(self.ctx.paths.votes, all_votes)
        self.ctx.clock.advance(hours=2)
        self.tick("tidy")
        self.assertTrue(self.archived(card), "a vote from before the close no longer pins a closed card")

    def test_vote_opened_after_the_close_still_pins(self):
        self.on()
        card = self.closed_case()
        key = self.only_case()[0]
        self.ctx.clock.advance(minutes=5)
        votes.open_vote(self.ctx, key=f"money:{key}:refund:ch_test0002", kind="money", case_key=key, card_id=card,
                        subject="Pat - export button does nothing", question="refund 19.00 USD?",
                        identity={"case_key": key, "action": "refund"},
                        material={"x": 1}, spec={"x": 1})
        self.ctx.clock.advance(hours=2)
        self.tick("tidy")
        self.assertFalse(self.archived(card))


class ConfigAndHealthTests(TidyLoop):
    def test_bad_limits_refused(self):
        for bad in ({"archive_waiting_days": 0}, {"archive_quiet_minutes": 0}):
            with self.assertRaises(ConfigError):
                validate_tidy(dataclasses.replace(Policy(), **bad))

    def test_health_watches_tidy_only_when_on(self):
        self.tick()
        self.assertNotIn("tidy", health.watched(self.ctx))
        self.on()
        self.assertIn("tidy", health.watched(self.ctx))

    def test_board_without_archive_support_is_refused_when_on(self):
        self.on()
        self.ctx.board.set_archived = None
        out = self.tick("tidy")["tidy"]
        self.assertTrue(out.get("unsupported"))
        self.assertTrue(any("cannot archive" in a for a in self.w.data["alerts"]))
