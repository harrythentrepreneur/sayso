"""Vote owners: every vote says whose decision it is and pings only that person.

A label only. These tests drive the real jobs on fakes and assert what landed:
the poll question, the card post, the notice and exactly who it pings, and
that the decision rule did not change.
"""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from sayso import cases, owners, votes
from sayso.adapters import reference
from sayso.config import Section
from tests.helpers import LoopTest

HARRY, KAVIRU, STRANGER = "1001", "1002", "9999"


class OwnerLoop(LoopTest):
    def setUp(self):
        super().setUp()
        self.ctx.config = dataclasses.replace(self.ctx.config, operators={HARRY: "Harry", KAVIRU: "Kaviru"})

    def open_case(self):
        self.write_in()
        self.tick("intake")
        return self.only_case()

    def poll_question(self, card):
        return [p["question"] for p in self.w.data["polls"].values() if p["card_id"] == card][-1]

    def notices(self):
        return self.w.data.get("notices", [])


class InferTests(OwnerLoop):
    def test_first_operator_to_post_owns_the_vote_and_only_they_are_pinged(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], STRANGER, "me too")
        self.w.operator_posts(rec["card_id"], KAVIRU, "I'll take this one")
        self.w.operator_posts(rec["card_id"], HARRY, "ok")
        self.tick("draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For Kaviru: "))
        self.assertEqual(self.notices()[-1]["pings"], [KAVIRU])
        self.assertIn("Work started by Kaviru. Any operator can still vote.",
                      self.ctx.board.messages(rec["card_id"]))
        self.assertEqual(cases.load(self.ctx)[key]["owner"], KAVIRU)

    def test_no_operator_post_is_for_either_and_pings_nobody(self):
        key, rec = self.open_case()
        self.tick("draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For either of you: "))
        self.assertEqual(self.notices()[-1]["pings"], [])
        self.assertNotIn("owner", cases.load(self.ctx)[key], "nothing to store until a person posts")

    def test_unreadable_card_is_never_a_guessed_person(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        self.w.data["faults"] = ["authors_unreadable"]
        self.w.save()
        self.tick("draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For either of you: "))
        self.assertEqual(self.notices()[-1]["pings"], [])
        self.assertNotIn("owner", cases.load(self.ctx)[key])
        self.assertEqual(self.live_vote("reply")["owner_source"], owners.UNREADABLE,
                         "the vote says WHY it is for either of you")

    def test_recorded_owner_survives_a_later_unreadable_card(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        self.tick("draft")
        self.w.data["faults"] = ["authors_unreadable"]
        self.w.save()
        self.w.customer_writes(sender="pat@customer.test", subject="Export button does nothing", body="still",
                               ticket=rec["ticket"])
        self.tick("intake", "draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For Harry: "),
                        "a recorded owner is not re-read, so an unreadable card cannot erase it")

    def test_first_owner_sticks_when_another_operator_posts_later(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        self.tick("draft")
        self.w.operator_posts(rec["card_id"], KAVIRU, "also here")
        self.w.customer_writes(sender="pat@customer.test", subject="Export button does nothing", body="still",
                               ticket=rec["ticket"])
        self.tick("intake", "draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For Harry: "))

    def test_board_without_author_support_reads_as_either(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        with mock.patch.object(type(self.ctx.board), "message_authors", new=None, create=True):
            self.tick("draft")
        self.assertEqual(self.notices()[-1]["pings"], [])


class OverrideTests(OwnerLoop):
    def test_override_wins_and_records_who_set_it(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        owners.set_override(self.ctx, key, "kaviru", by="Harry")
        self.tick("draft")
        self.assertTrue(self.poll_question(rec["card_id"]).startswith("For Kaviru: "))
        self.assertEqual(self.notices()[-1]["pings"], [KAVIRU])
        self.assertIn("Kaviru's decision (set by Harry). Any operator can still vote.",
                      self.ctx.board.messages(rec["card_id"]))
        self.assertEqual(cases.load(self.ctx)[key]["owner_set_by"], HARRY)

    def test_override_to_all_is_not_replaced_by_inference(self):
        key, rec = self.open_case()
        owners.set_override(self.ctx, key, "all", by=KAVIRU)
        self.w.operator_posts(rec["card_id"], HARRY, "mine")
        self.tick("draft")
        self.assertEqual(self.notices()[-1]["pings"], [])
        self.assertEqual(cases.load(self.ctx)[key]["owner_source"], owners.OVERRIDE)

    def test_owner_must_be_an_operator(self):
        key, _ = self.open_case()
        for bad in (STRANGER, "Bob", ""):
            with self.assertRaises(owners.OwnerError):
                owners.set_override(self.ctx, key, bad, by="Harry")
        with self.assertRaises(owners.OwnerError):
            owners.set_override(self.ctx, key, "Harry", by="all")

    def test_notice_refuses_to_ping_a_non_operator(self):
        with self.assertRaises(ValueError):
            self.ctx.notify("k", "text", mention=STRANGER)


class LabelOnlyTests(OwnerLoop):
    def test_the_other_operator_can_still_approve(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], KAVIRU, "mine")
        self.tick("draft")
        self.vote("reply", user=HARRY)
        self.tick("votes", "sender")
        self.assertEqual(len(self.w.data["outbox"]), 1, "the owner is a label; Harry's Yes still counts")

    def test_the_other_operator_no_still_declines(self):
        key, rec = self.open_case()
        self.w.operator_posts(rec["card_id"], KAVIRU, "mine")
        self.tick("draft")
        v = self.live_vote("reply")
        self.w.operator_votes(v["poll_id"], KAVIRU, "yes")
        self.w.operator_votes(v["poll_id"], HARRY, "no")
        self.tick("votes", "sender")
        self.assertEqual(self.w.data["outbox"], [])
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DECLINED)

    def test_reopened_vote_key_does_not_post_twice(self):
        key, rec = self.open_case()
        self.tick("draft")
        before = list(self.ctx.board.messages(rec["card_id"]))
        v = self.live_vote("reply")
        votes.open_vote(self.ctx, key=v["key"], kind="reply", case_key=key, card_id=rec["card_id"],
                        subject=v["subject"], question=v["question"], identity=v["identity"], material={})
        self.assertEqual(self.ctx.board.messages(rec["card_id"]), before)
        self.assertEqual(len(self.notices()), 1)


class LabelTextTests(OwnerLoop):
    def test_three_operators_say_any_operator(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, operators={HARRY: "Harry", KAVIRU: "Kaviru",
                                                                          "1003": "Sam"})
        self.assertEqual(owners.label(self.ctx, owners.ALL), "For any operator: ")
        self.assertEqual(owners.label(self.ctx, "1003"), "For Sam: ")

    def test_removed_operator_reads_as_any(self):
        self.assertEqual(owners.label(self.ctx, "4242"), "For either of you: ")


@mock.patch.dict(os.environ, {"T_TOKEN": "tok"})
class DiscordNoticeTests(unittest.TestCase):
    def board(self, responses):
        calls = []

        def transport(method, url, headers, body):
            calls.append({"method": method, "url": url, "body": json.loads(body) if body else None})
            return 200, {}, json.dumps(responses.pop(0)).encode()
        b = reference.DiscordForumBoard(Section("discord", {"forum_id": "1", "alerts_channel_id": "2",
                                                            "token_env": "T_TOKEN"}), transport=transport)
        return b, calls

    def test_notice_pings_exactly_one_user_and_nothing_else(self):
        b, calls = self.board([{"id": "m"}])
        b.notify("For Kaviru: @everyone <@&55> <@77> merge?", "k", mention="1002")
        body = calls[0]["body"]
        self.assertEqual(body["allowed_mentions"], {"parse": [], "users": ["1002"]})
        self.assertTrue(body["content"].startswith("<@1002> "))

    def test_notice_for_either_pings_nobody(self):
        b, calls = self.board([{"id": "m"}])
        b.notify("For either of you: merge?", "k")
        self.assertEqual(calls[0]["body"]["allowed_mentions"], {"parse": [], "users": []})

    def test_authors_oldest_first_across_pages_bots_skipped(self):
        page1 = [{"id": str(i), "author": {"id": "1001" if i == 150 else "9"}, "timestamp": f"t{i}"}
                 for i in range(101, 201)]
        page1[0]["author"] = {"id": "1002", "bot": True}
        page2 = [{"id": "201", "author": {"id": "1002"}, "timestamp": "t201"}]
        b, calls = self.board([list(reversed(page1)), page2])
        authors = b.message_authors("c")
        self.assertEqual(authors[0], ("9", "t102"), "the bot at 101 is skipped; oldest human first")
        self.assertEqual(authors[-1], ("1002", "t201"), "the second page is read")
        self.assertEqual(calls[1]["url"].split("after=")[1].split("&")[0], "200")


if __name__ == "__main__":
    unittest.main()
