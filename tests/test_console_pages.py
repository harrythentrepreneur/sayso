"""The console pages ported from Sayso Cloud: decisions queue, inbox list, refunds ledger, insights, health,
audit log + CSV. Read-only dashboard rules still hold on every new view and query string."""
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from sayso import dashboard
from sayso.brand import app as A

HOSTILE = "<img src=x onerror=alert(1)>"


class ConsolePagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sayso-pages-test-")
        cls.config = dashboard.build_demo_state(Path(cls._tmp.name))
        cls.products = [cls.config.slug]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def snap(self):
        return dashboard.Snapshot(self.config)

    def get(self, path):
        return dashboard.route(path, lambda _s: self.snap(), self.products)

    # -- decisions --------------------------------------------------------------------------------
    def test_decisions_queue_puts_money_first_and_shows_the_trust_line(self):
        _, _, body = self.get("/p/demo/")
        self.assertIn("<h1>Decisions</h1>", body)
        self.assertLess(body.index("Money &amp; billing"), body.index("Everything else"))
        for v in self.snap().open_votes():
            self.assertIn(str(v["fingerprint"])[:12], body)        # the say-so lock names the exact version
        self.assertIn("Say-so lock", body)
        self.assertIn("$29.00", body)                              # refund amount formatted

    def test_decisions_sort_is_a_link_not_a_form(self):
        _, _, body = self.get("/p/demo/?sort=oldest")
        self.assertIn("aria-current=page>Oldest", body)
        self.assertNotIn("Money &amp; billing", body)               # groups only in priority order

    def test_queue_order(self):
        order = [v["kind"] for v, _ in dashboard.queue(self.snap())]
        self.assertEqual(order[0], "money")

    def test_flags_need_a_real_repeat_phrase(self):
        s = self.snap()
        self.assertEqual(dashboard.flags(s, {"title": "Second problem here"}), [])
        self.assertIn(("warn", "Repeat contact"), dashboard.flags(s, {"title": "Third time asking about this"}))
        self.assertIn(("neutral", "Billing"), dashboard.flags(s, {"title": "Charged twice"}))

    def test_expiry_badge_only_near_the_policy_expiry(self):
        s = self.snap()
        v = s.open_votes()[0]
        ref = A.parse(v["opened_at"])
        from datetime import timedelta
        self.assertNotIn("Expires", dashboard.wait_badges(s, v, ref + timedelta(hours=1)))
        near = ref + timedelta(hours=s.config.policy.vote_expiry_hours - 3)
        self.assertIn("Expires in 3 h", dashboard.wait_badges(s, v, near))

    # -- inbox ------------------------------------------------------------------------------------
    def test_inbox_tabs_search_and_dense_rows(self):
        _, _, body = self.get("/p/demo/board?view=waiting")
        self.assertIn("<h1>Inbox</h1>", body)
        self.assertIn("aria-current=page>Waiting on you", body)
        _, _, body = self.get("/p/demo/board?q=dark+mode")
        rows = body.split("<tbody>")[1]
        self.assertIn("Feature request: dark mode", rows)
        self.assertNotIn("Charged after I cancelled", rows)
        self.assertRegex(body, r"<time datetime='[^']+' title='\d\d \w{3} \d{4}, \d\d:\d\d UTC'>")
        _, _, body = self.get("/p/demo/board?q=zzzz-nothing")
        self.assertIn("No cases match", body)

    def test_inbox_money_flag(self):
        _, _, body = self.get("/p/demo/board")
        row = [r for r in body.split("<tr") if "Charged after I cancelled" in r][0]
        self.assertIn(">Money<", row)

    def test_nav_groups_and_labels(self):
        _, _, body = self.get("/p/demo/")
        nav = body[body.index("<nav"):body.index("</nav>")]
        for label in ("Decisions", "Inbox", "Refunds", "Insights", "Health", "Audit log"):
            self.assertIn(label, nav)
        self.assertIn("navgroup admin", nav)
        self.assertLess(nav.index("Insights"), nav.index("Admin"))
        self.assertNotIn("Swipe", nav)                              # a mode of Decisions, not a place
        self.assertNotIn("Privacy", body)                           # no marketing footer in the work area

    def test_dry_run_strip_only_on_dry_run(self):
        _, _, body = self.get("/p/demo/")
        self.assertIn("demo-strip", body)

    def test_hostile_query_strings_are_escaped_everywhere(self):
        for path in ("/p/demo/", "/p/demo/board", "/p/demo/money", "/p/demo/overview", "/p/demo/audit"):
            for k in ("sort", "view", "q", "days", "event", "case"):
                status, _, body = self.get(f"{path}?{k}={HOSTILE}")
                self.assertEqual(status, 200, (path, k))
                self.assertNotIn("<img", body, (path, k))


if __name__ == "__main__":
    unittest.main()
