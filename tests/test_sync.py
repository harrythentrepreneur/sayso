"""Discord (board poll) and the console stay in sync on one decision record. Fake data only."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sayso import console, dashboard, runtime, votes
from sayso.jobs import tally


class Journal:
    def __init__(self):
        self.rows = []

    def append(self, event, **kw):
        self.rows.append((event, kw))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = dashboard.build_demo_state(Path(self.tmp.name))
        ops = sorted(self.config.operators)
        self.op = ops[0]
        self.user = {"name": "Op One", "operator_id": self.op}

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self):
        return runtime.build(self.config)

    def open_vote(self):
        return next(v for v in votes.load(self.ctx()).values() if v["status"] == "open")

    def web(self, v, answer, uid=None):
        user = {"name": "x", "operator_id": uid or self.op}
        console.record_web_vote(self.config, Journal(), user=user, key=v["key"], answer=answer,
                                fingerprint=v["fingerprint"])

    def world(self):
        return json.loads((self.config.state_dir / "fake-world.json").read_text())

    def card_messages(self, v):
        return self.world()["cards"][v["card_id"]]["messages"]

    # -- ballots ---------------------------------------------------------------
    def test_merge_one_ballot_per_person_no_wins_on_conflict(self):
        m = tally.merge_ballots
        self.assertEqual(m({"a": "yes"}, {"a": "no"}), {"a": "no"})
        self.assertEqual(m({"a": "no"}, {"a": "yes"}), {"a": "no"})
        self.assertEqual(m({"a": "yes"}, {"b": "yes"}), {"a": "yes", "b": "yes"})
        self.assertEqual(m({}, {}), {})

    def test_console_yes_shows_on_the_board_and_closes_the_poll(self):
        v = self.open_vote()
        self.web(v, "yes")
        tally.run(self.ctx())
        rec = votes.load(self.ctx())[v["key"]]
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["decided_via"], "console")
        msgs = "\n".join(self.card_messages(v))
        self.assertIn("voted Yes from the console", msgs)
        self.assertIn("approved (from the console)", msgs)
        self.assertTrue(self.world()["polls"][v["poll_id"]]["closed"])

    def test_board_vote_shows_in_the_console(self):
        v = self.open_vote()
        other = sorted(self.config.operators)[0]
        self.ctx().world.operator_votes(v["poll_id"], other, "no")
        tally.run(self.ctx())
        rec = votes.load(self.ctx())[v["key"]]
        self.assertEqual(rec["status"], "declined")
        self.assertEqual(rec["decided_via"], "board")
        s = dashboard.Snapshot(self.config)
        self.assertIn((self.config.operators[other], "no", "board"), dashboard.ballots_for(s, rec))

    def test_console_vote_visible_before_the_next_run(self):
        v = self.open_vote()
        self.web(v, "yes")
        s = dashboard.Snapshot(self.config)
        rec = s.votes[v["key"]]
        self.assertIn((self.config.operators[self.op], "yes", "console"), dashboard.ballots_for(s, rec))

    def test_board_yes_and_console_no_from_same_person_declines(self):
        v = self.open_vote()
        self.ctx().world.operator_votes(v["poll_id"], self.op, "yes")
        self.web(v, "no")
        tally.run(self.ctx())
        self.assertEqual(votes.load(self.ctx())[v["key"]]["status"], "declined")

    def test_settled_decision_refuses_more_console_votes(self):
        v = self.open_vote()
        self.web(v, "yes")
        tally.run(self.ctx())
        with self.assertRaises(console.ConsoleError):
            self.web(v, "no")

    def test_mirror_post_is_not_repeated_on_later_runs(self):
        v = self.open_vote()
        self.web(v, "yes")
        tally.run(self.ctx())
        tally.run(self.ctx())
        msgs = [m for m in self.card_messages(v) if "from the console:" in m]
        self.assertEqual(len(msgs), 1)

    def test_non_operator_console_vote_never_decides(self):
        v = self.open_vote()
        path = console.web_votes_path(self.config)
        path.write_text(json.dumps({v["key"]: {"stranger": {"answer": "yes", "fingerprint": v["fingerprint"],
                                                            "at": datetime.now(timezone.utc).isoformat()}}}))
        tally.run(self.ctx())
        self.assertEqual(votes.load(self.ctx())[v["key"]]["status"], "open")

    def test_poll_close_failure_does_not_undo_the_decision(self):
        v = self.open_vote()
        self.web(v, "yes")
        ctx = self.ctx()

        def boom(*_a):
            raise RuntimeError("discord down")
        ctx.board.close_poll = boom
        tally.run(ctx)
        self.assertEqual(votes.load(self.ctx())[v["key"]]["status"], "approved")
        journal = (self.config.state_dir / "journal.jsonl").read_text()
        self.assertIn("poll_close_failed", journal)


if __name__ == "__main__":
    unittest.main()
