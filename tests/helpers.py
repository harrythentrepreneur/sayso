"""Shared test helpers: a throwaway product on fakes with a movable clock."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sayso import cases, demo, votes
from sayso.jobs import JOBS, ORDER

OPERATOR = demo.OPERATOR
STRANGER = "9999"


class LoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-test-")
        self.tmp = Path(self._tmp.name)
        self.ctx = demo.build(self.tmp)
        self.w = self.ctx.world

    def tearDown(self):
        self._tmp.cleanup()

    def tick(self, *names):
        out = {}
        for name in names or ORDER:
            kw = {"sleep": lambda s: None} if name == "sender" else {}
            out[name] = JOBS[name](self.ctx, **kw)
        return out

    def write_in(self, sender="pat@customer.test", subject="Export button does nothing", body="It fails."):
        return self.w.customer_writes(sender=sender, subject=subject, body=body)

    def only_case(self):
        items = list(cases.load(self.ctx).items())
        self.assertEqual(len(items), 1, items)
        return items[0]

    def live_vote(self, kind):
        live = [v for v in votes.load(self.ctx).values() if v["kind"] == kind and v["status"] == votes.OPEN]
        self.assertEqual(len(live), 1, f"open {kind} votes: {live}")
        return live[0]

    def vote(self, kind, answer="yes", user=OPERATOR):
        v = self.live_vote(kind)
        self.w.operator_votes(v["poll_id"], user, answer)
        return v

    def tags(self, card):
        return self.ctx.board.get_tags(card)
