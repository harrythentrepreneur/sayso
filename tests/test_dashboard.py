"""The read-only dashboard: it shows the loop's state and can change nothing.

Tests go through a REAL HTTP server on a free loopback port, not the render
functions alone, so a handler that wrote or accepted a POST would be caught.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sayso import dashboard, votes


def tree_fingerprint(root: Path) -> dict[str, str]:
    """Every file under root with its content hash and mtime: any write changes this."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = f"{hashlib.sha256(p.read_bytes()).hexdigest()}:{st.st_mtime_ns}"
        else:
            out[str(p.relative_to(root)) + "/"] = "dir"
    return out


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sayso-dash-test-")
        cls.config = dashboard.build_demo_state(Path(cls._tmp.name))
        cls.state = cls.config.state_dir
        cls.server = dashboard.make_server([cls.config], "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def get(self, path, method="GET", data=None):
        req = urllib.request.Request(self.base + path, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

    def all_pages(self):
        paths = ["/", "/p/demo/", "/p/demo/overview", "/p/demo/board", "/p/demo/money", "/p/demo/health", "/p/demo/audit",
                 "/api/demo.json"]
        cases = json.loads((self.state / "cases.json").read_text())
        paths += [f"/p/demo/case/{k}" for k in cases]
        return paths

    # -- it shows the loop ---------------------------------------------------
    def test_every_screen_renders(self):
        for path in self.all_pages():
            status, body, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)

    def test_decisions_show_every_open_vote_with_its_exact_material(self):
        _, body, _ = self.get("/p/demo/")
        open_votes = [v for v in json.loads((self.state / "votes.json").read_text()).values()
                      if v["status"] == votes.OPEN]
        self.assertGreaterEqual(len(open_votes), 3)
        for v in open_votes:
            self.assertIn(dashboard.e(v["subject"]), body)
        self.assertIn("refund 29.00 USD?", body)  # money vote question
        self.assertIn("&quot;amount&quot;: 2900", body)  # the exact spec being voted on
        self.assertIn("Thanks for writing to us about", body)  # the exact draft text
        self.assertIn("This page cannot approve", body)

    def test_board_places_cards_by_live_stage(self):
        _, body, _ = self.get("/p/demo/board")
        self.assertIn("Awaiting approval · 3", body)
        self.assertIn("Done · 1", body)

    def test_case_page_leads_with_customer_words_and_shows_proof(self):
        cases = json.loads((self.state / "cases.json").read_text())
        pat = next(k for k, r in cases.items() if r["customer"] == "pat@customer.test")
        _, body, _ = self.get(f"/p/demo/case/{pat}")
        self.assertLess(body.index("Customer wrote"), body.index("Votes"))
        self.assertIn("when I press Export nothing happens", body)
        self.assertIn("SENT_AND_VERIFIED", body)
        self.assertIn("Operator One", body)  # decided_by shown as a name
        self.assertIn("reply-v2.txt", body)

    def test_overview_numbers_come_from_state(self):
        _, body, _ = self.get("/p/demo/overview")
        vs = json.loads((self.state / "votes.json").read_text())
        n_open = sum(1 for v in vs.values() if v["status"] == votes.OPEN)
        self.assertIn(f"<div class=v>{n_open}</div>", body)  # waiting on you
        self.assertIn(f"All {len(vs)} decisions", body)
        self.assertIn("<svg class=chart", body)
        self.assertIn("Not measured yet", body)  # no invented cost or satisfaction numbers

    def test_decision_time_is_measured_from_the_journal(self):
        snap = dashboard.Snapshot(self.config)
        times = dashboard.decision_times(snap)
        self.assertTrue(times)
        self.assertTrue(all(t >= 0 for t in times))

    def test_money_shows_provider_verification(self):
        _, body, _ = self.get("/p/demo/money")
        self.assertIn("VERIFIED_WITH_PROVIDER", body)
        self.assertIn("refund 19.00 USD", body)
        self.assertIn("refund 29.00 USD", body)

    def test_pause_is_visible(self):
        flag = self.state / "PAUSED"
        flag.write_text("checking a refund", encoding="utf-8")
        try:
            _, body, _ = self.get("/p/demo/health")
            self.assertIn("Paused.", body)
            self.assertIn("checking a refund", body)
        finally:
            flag.unlink()

    # -- it cannot change anything ---------------------------------------------
    def test_browsing_every_page_writes_nothing(self):
        before = tree_fingerprint(self.state)
        for _ in range(2):
            for path in self.all_pages():
                self.get(path)
        self.assertEqual(tree_fingerprint(self.state), before)

    def test_only_get_and_head(self):
        before = tree_fingerprint(self.state)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _, _ = self.get("/p/demo/", method=method, data=b"x=1")
            self.assertEqual(status, 405, method)
        status, body, _ = self.get("/p/demo/", method="HEAD")
        self.assertEqual((status, body), (200, ""))
        self.assertEqual(tree_fingerprint(self.state), before)

    def test_no_forms_or_buttons(self):
        for path in self.all_pages():
            _, body, _ = self.get(path)
            for tag in ("<form", "<button", "<input", "<script"):
                self.assertNotIn(tag, body.lower(), f"{path}: {tag}")

    def test_security_headers(self):
        _, _, headers = self.get("/p/demo/")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    # -- untrusted input -----------------------------------------------------------
    def test_customer_html_is_escaped(self):
        cases = json.loads((self.state / "cases.json").read_text())
        lee = next(k for k, r in cases.items() if r["customer"] == "lee@customer.test")
        _, body, _ = self.get(f"/p/demo/case/{lee}")
        self.assertIn("&lt;b&gt;Please fix&lt;/b&gt;", body)
        self.assertNotIn("<b>Please fix</b>", body)

    def test_draft_path_outside_state_dir_is_not_read(self):
        secret = Path(self._tmp.name) / "outside.txt"
        secret.write_text("TOP SECRET OUTSIDE STATE", encoding="utf-8")
        snap = dashboard.Snapshot(self.config)
        self.assertIsNone(snap.draft_text({"spec": {"path": str(secret)}}))
        self.assertIsNone(snap.draft_text({"spec": {"path": str(self.state / ".." / "outside.txt")}}))

    def test_unknown_paths_404(self):
        for path in ("/p/nope/", "/p/demo/case/nope", "/p/demo/../../etc/passwd", "/api/nope.json", "/x"):
            self.assertEqual(self.get(path)[0], 404, path)

    # -- fail closed ------------------------------------------------------------------
    def test_corrupt_state_is_an_error_not_an_empty_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dashboard.build_demo_state(Path(tmp))
            (cfg.state_dir / "votes.json").write_text("{not json", encoding="utf-8")
            srv = dashboard.make_server([cfg], "127.0.0.1", 0)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/p/demo/", timeout=10)
                self.assertEqual(caught.exception.code, 500)
                self.assertIn("state could not be read", caught.exception.read().decode())
            finally:
                srv.shutdown()
                srv.server_close()

    def test_refuses_to_listen_beyond_this_machine(self):
        for host in ("0.0.0.0", "192.168.1.5", "::", "example.com"):
            with self.assertRaises(dashboard.RemoteBindRefused, msg=host):
                dashboard.make_server([self.config], host, 0)
        for host in ("127.0.0.1", "localhost", "::1"):
            dashboard.check_host(host)

    def test_unreadable_real_board_shows_unreadable_not_a_guess(self):
        snap = dashboard.Snapshot(self.config, board_reader=lambda cid: (_ for _ in ()).throw(OSError("down")))
        snap.world = None
        card = next(iter(snap.cases.values()))["card_id"]
        self.assertEqual(snap.stage(card), "unreadable")

    def test_stale_job_is_flagged(self):
        from datetime import datetime, timezone
        snap = dashboard.Snapshot(self.config, now=datetime(2031, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(all(h["state"] == "stale" for h in snap.health() if h["at"]))


if __name__ == "__main__":
    unittest.main()
