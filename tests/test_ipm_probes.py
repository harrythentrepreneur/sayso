"""Checks for three PhonicsMaker-loop defects, run on Sayso to see if Sayso has them.

1. A pending customer-reply vote must keep its card in Awaiting approval while a PR
   for the same case is open (in the PhonicsMaker loop the release controller moved
   the card back to In QA, and the sender then refused the owner's Yes).
2. Discord system rows (member added/removed, pins, thread events) have empty text.
   They are valid history: card reads must not fail or stop on them
   (in the PhonicsMaker loop they raised false "history unreadable" alerts).
3. An Awaiting-approval card whose vote is open must not get a stall notice that
   only repeats the vote reminder (PhonicsMaker loop: vote repeats in the alerts room).
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from sayso import cases, stages, votes
from sayso.adapters import reference
from sayso.config import Section
from sayso.jobs import dev
from tests.helpers import LoopTest
from tests.test_reference_adapters import ENV, Recorder
from tests.test_qa import HEAD, passing

HARRY, KAVIRU = "1001", "1002"


class ReplyVoteHoldsTheCard(LoopTest):
    def test_open_pr_and_pending_reply_vote_stay_in_awaiting_and_the_yes_sends(self):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        card = rec["card_id"]
        dev.request(self.ctx, key)
        self.tick("dev")
        self.ctx.dev_runner.finish(cases.load(self.ctx)[key]["dev_run"], pr=7, head=HEAD)
        self.tick("dev", "qa")
        self.ctx.qa_runner.finish(cases.load(self.ctx)[key]["qa"]["run"], passing())
        self.tick("qa")
        self.vote("merge")
        self.tick("votes", "release", "draft")          # merged; the customer reply is drafted
        self.assertEqual(stages.stage_of(self.tags(card)), stages.AWAITING)
        self.ctx.codehost.add_pr(8, "e" * 40)            # a second PR for the case opens meanwhile
        cases.update(self.ctx, key, pr=8, prs=[8])
        for _ in range(3):
            self.tick()                                  # every job, several passes
        self.assertEqual(stages.stage_of(self.tags(card)), stages.AWAITING)
        self.vote("reply")
        self.tick("votes", "sender")
        self.assertEqual(len(self.w.data["outbox"]), 1, "the owner's Yes on the reply is sent")


@mock.patch.dict(os.environ, ENV)
class DiscordSystemRows(unittest.TestCase):
    def board(self, responses):
        rec = Recorder(responses)
        return reference.DiscordForumBoard(Section("discord", {"forum_id": "F", "alerts_channel_id": "A",
                                                               "token_env": "T_TOKEN"}), transport=rec)

    ROWS = [{"id": "3", "type": 2, "content": "", "author": {"id": "9", "bot": False}, "timestamp": "t3"},
            {"id": "2", "type": 0, "content": "It fails.", "author": {"id": "1001"}, "timestamp": "t2"},
            {"id": "1", "type": 18, "content": "", "author": {"id": "9"}, "timestamp": "t1"}]

    def test_messages_keep_the_real_text_and_do_not_fail(self):
        b = self.board([(200, self.ROWS)])
        self.assertIn("It fails.", b.messages("C"))

    def test_authors_read_past_system_rows(self):
        b = self.board([(200, list(reversed(self.ROWS)))])
        self.assertEqual([a for a, _ in b.message_authors("C")], ["9", "1001", "9"])

    def test_last_activity_with_a_system_row_last_is_readable(self):
        b = self.board([(200, {"id": "1477455328051200000", "last_message_id": "1558358419046400000"})])
        self.assertTrue(b.last_activity("1477455328051200000"))


class NoVoteRepeatsInAlerts(LoopTest):
    def setUp(self):
        super().setUp()
        self.ctx.config = dataclasses.replace(self.ctx.config, operators={HARRY: "Harry", KAVIRU: "Kaviru"})

    def test_open_vote_card_gets_reminders_not_stall_repeats(self):
        self.write_in()
        self.tick("intake", "draft")
        self.ctx.clock.advance(hours=3)                 # past stall_awaiting_hours (2)
        self.tick("stall")
        texts = [n["text"] for n in self.w.data.get("notices", [])]
        self.assertEqual(len([t for t in texts if "still waiting" in t]), 1)
        self.assertEqual([t for t in texts if "no activity for" in t], [],
                         "a stall line for a card whose vote is open only repeats the vote")


if __name__ == "__main__":
    unittest.main()
