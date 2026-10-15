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

    # -- case -------------------------------------------------------------------------------------
    def test_case_conversation_first_then_decision_panel_with_lock(self):
        s = self.snap()
        key = next(k for k, r in s.cases.items() if r["customer"] == "lee@customer.test")
        _, _, body = self.get(f"/p/demo/case/{key}")
        main = body[body.index("<main>"):]
        self.assertLess(main.index("Customer wrote"), main.index("<h3 style='margin-top:0'>Decision"))
        self.assertIn("Say-so lock", main)
        self.assertIn("&lt;b&gt;Please fix&lt;/b&gt;", main)          # customer HTML stays text
        self.assertNotIn("<b>Please fix</b>", main)
        self.assertIn("Email headers", main)
        self.assertIn("aria-current=\"page\"", body.split("Inbox</a>")[0][-300:] + body)  # Inbox highlighted

    def test_case_receipts_show_provider_refund(self):
        s = self.snap()
        key = next(k for k, r in s.cases.items() if s.attempts and any(
            s.votes.get(vk, {}).get("case_key") == k and s.votes[vk]["kind"] == "money" for vk in s.attempts))
        _, _, body = self.get(f"/p/demo/case/{key}")
        self.assertIn("VERIFIED_WITH_PROVIDER", body)
        self.assertIn("$19.00", body)
        self.assertIn("re_19", body)

    # -- refunds ----------------------------------------------------------------------------------
    def test_refunds_tiles_tabs_and_provider_column(self):
        _, _, body = self.get("/p/demo/money")
        self.assertIn("<h1>Refunds</h1>", body)
        main = body[body.index("<main>"):]; tiles = main[main.index("class=sa-tiles"):main.index("class=sa-bar")]
        self.assertIn("Waiting for a decision", tiles)
        self.assertIn("$29.00", tiles)
        self.assertIn("Refunded and verified", tiles)
        self.assertIn("$50.00", tiles)                               # cap from the settings file
        self.assertIn("Provider check", body)
        _, _, body = self.get("/p/demo/money?view=waiting")
        rows = body.split("<tbody>")[1]
        self.assertIn("ch_demo0002", rows)
        self.assertNotIn("ch_demo0001", rows)

    # -- insights ---------------------------------------------------------------------------------
    def test_insights_periods_sources_and_axis(self):
        for d, want in (("7", "Last 7 days"), ("30", "Last 30 days"), ("", "Last 14 days"), ("99", "Last 14 days")):
            _, _, body = self.get(f"/p/demo/overview?days={d}")
            self.assertIn(want, body)
        main = body[body.index("<main>"):]
        self.assertEqual(main.count("class=sa-tile-s"), 6)              # every KPI says where it came from
        self.assertIn("text-anchor='end'>0</text>", main)              # y axis starts at zero, labelled
        self.assertIn("Time waiting on you", main)
        self.assertIn("Not measured yet", main)

    # -- health -----------------------------------------------------------------------------------
    def test_health_jobs_in_run_order_and_fix_text_when_stale(self):
        from sayso.jobs import ORDER
        from datetime import timedelta
        s = self.snap()
        _, _, body = self.get("/p/demo/health")
        main = body[body.index("<main>"):]
        names = re.findall(r"<code class=raw>(\w+)</code>", main)
        self.assertEqual(names, [j for j in ORDER if j in names])
        self.assertIn("Running", main)
        self.assertNotIn("What to do", main)
        later = dashboard.Snapshot(self.config, now=A.parse(max(h["at"] for h in s.health() if h["at"])) + timedelta(days=1))
        page = dashboard.render_health(later, self.products)
        self.assertIn("Needs attention", page)
        self.assertIn("What to do:", page)
        self.assertIn("journalctl --user -u sayso-demo-intake", page)

    # -- audit ------------------------------------------------------------------------------------
    def test_audit_day_groups_filters_and_fingerprints(self):
        _, _, body = self.get("/p/demo/audit")
        main = body[body.index("<main>"):]
        self.assertIn("class=aday", main)
        self.assertIn("fingerprint <code", main)
        self.assertIn("Export CSV", main)
        _, _, body = self.get("/p/demo/audit?kind=sends")
        self.assertNotIn(">Case opened<", body)
        self.assertIn(">Vote done<", body)

    def test_audit_csv_has_every_line_and_is_spreadsheet_safe(self):
        status, ctype, text = self.get("/p/demo/audit.csv")
        self.assertEqual((status, ctype), (200, "text/csv"))
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][0], "at_utc")
        self.assertEqual(len(rows) - 1, len(self.snap().journal))
        self.assertEqual(dashboard._csv_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(dashboard._csv_cell("@x"), "'@x")
        _, _, only = self.get("/p/demo/audit.csv?kind=alerts")
        self.assertEqual(len(list(csv.reader(io.StringIO(only)))), 1 + sum(
            1 for j in self.snap().journal if j.get("event") in ("alert", "notice")))

    def test_job_summary_in_words(self):
        self.assertEqual(dashboard.job_summary("{'opened': 1, 'appended': 0, 'held': []}"), "1 opened")
        self.assertEqual(dashboard.job_summary("{}"), "Nothing to do")
        self.assertEqual(dashboard.job_summary("{'stale': ['intake'], 'paused': False}"), "stale: intake")
        self.assertEqual(dashboard.job_summary("0 findings"), "0 findings")
        self.assertEqual(dashboard.job_summary("{__import__('os')}"), "{__import__('os')}")   # never evaluated

    def test_every_audit_line_has_a_category_and_words(self):
        s = self.snap()
        for j in s.journal:
            self.assertIn(j["event"], dashboard.AUDIT_OF, j["event"])
            self.assertNotEqual(dashboard.event_text(s, j), j["event"])

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
