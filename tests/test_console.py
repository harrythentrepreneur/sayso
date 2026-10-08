"""The console: sign-in, CSRF, web votes bound to a fingerprint, pause. Fake data only."""
from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from sayso import console, dashboard, runtime, votes
from sayso.jobs import tally

SECRET = "s" * 40
PASSWORD = "correct horse battery"


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = dashboard.build_demo_state(Path(self.tmp.name))
        self.op = next(iter(self.config.operators))
        console.add_user(self.config, "Op One", self.op, PASSWORD)
        self.auth = console.Auth(self.config, SECRET)
        self.srv = console.make_server(self.config, self.auth, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------------
    def req(self, method, path, form=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urlencode(form) if form is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        if cookie:
            headers["Cookie"] = f"sayso_session={cookie}"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read().decode()
        return r.status, dict(r.getheaders()), data

    def login(self, name="Op One", password=PASSWORD):
        status, headers, _ = self.req("POST", "/login", {"name": name, "password": password})
        m = re.search(r"sayso_session=([^;]+)", headers.get("Set-Cookie", ""))
        return status, (m.group(1) if m else None)

    def page(self, cookie, path=None):
        return self.req("GET", path or f"/p/{self.config.slug}/", cookie=cookie)

    def csrf(self, html_text):
        return re.search(r"name=csrf value='([^']+)'", html_text).group(1)

    def open_vote(self):
        return next(v for v in json.loads((self.config.state_dir / "votes.json").read_text()).values()
                    if v["status"] == "open")

    # -- tests ---------------------------------------------------------------
    def test_every_page_needs_sign_in(self):
        for path in ("/", f"/p/{self.config.slug}/", f"/p/{self.config.slug}/board", "/api/x.json"):
            status, headers, body = self.req("GET", path)
            self.assertEqual(status, 303, path)
            self.assertEqual(headers["Location"], "/login")
            self.assertNotIn("customer.test", body)

    def test_wrong_password_and_unknown_user_refused(self):
        self.assertEqual(self.login(password="wrong password!!")[0], 401)
        self.assertEqual(self.login(name="nobody")[0], 401)

    def test_forged_or_tampered_cookie_refused(self):
        _, good = self.login()
        body, sig = good.rsplit(".", 1)
        for bad in ("x.y", body + "." + sig[::-1], console.Auth(self.config, "t" * 40).make_cookie(
                json.loads((self.config.state_dir / "console-users.json").read_text())["op one"])):
            self.assertEqual(self.page(bad)[0], 303)

    def test_only_operators_can_be_users(self):
        with self.assertRaises(console.ConsoleError):
            console.add_user(self.config, "Stranger", "not-an-operator", PASSWORD)
        with self.assertRaises(console.ConsoleError):
            console.add_user(self.config, "Short", self.op, "short")

    def test_password_change_ends_old_sessions(self):
        _, cookie = self.login()
        self.assertEqual(self.page(cookie)[0], 200)
        console.add_user(self.config, "Op One", self.op, PASSWORD + "2")
        self.assertEqual(self.page(cookie)[0], 303)

    def test_lockout_after_repeated_failures(self):
        for _ in range(console.MAX_FAILS):
            self.login(password="wrong password!!")
        status, _ = self.login()
        self.assertEqual(status, 401)

    def test_post_without_csrf_refused(self):
        _, cookie = self.login()
        v = self.open_vote()
        status, _, _ = self.req("POST", f"/p/{self.config.slug}/vote",
                                {"key": v["key"], "answer": "yes", "fp": v["fingerprint"]}, cookie)
        self.assertEqual(status, 403)
        self.assertFalse((self.config.state_dir / "web-votes.json").exists())

    def test_web_yes_is_counted_by_tally_and_nothing_acts_in_the_request(self):
        _, cookie = self.login()
        _, _, html_text = self.page(cookie)
        self.assertIn("Yes, approve", html_text)
        v = self.open_vote()
        world_before = (self.config.state_dir / "fake-world.json").read_text()
        status, _, _ = self.req("POST", f"/p/{self.config.slug}/vote",
                                {"csrf": self.csrf(html_text), "key": v["key"], "answer": "yes",
                                 "fp": v["fingerprint"]}, cookie)
        self.assertEqual(status, 303)
        self.assertEqual((self.config.state_dir / "fake-world.json").read_text(), world_before)  # nothing sent
        self.assertEqual(votes.load(runtime.build(self.config))[v["key"]]["status"], "open")
        tally.run(runtime.build(self.config))
        rec = votes.load(runtime.build(self.config))[v["key"]]
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["decided_by"], self.config.operators[self.op])

    def test_stale_page_vote_refused(self):
        _, cookie = self.login()
        _, _, html_text = self.page(cookie)
        v = self.open_vote()
        status, _, body = self.req("POST", f"/p/{self.config.slug}/vote",
                                   {"csrf": self.csrf(html_text), "key": v["key"], "answer": "yes",
                                    "fp": "0" * 64}, cookie)
        self.assertEqual(status, 409)
        self.assertIn("changed", body)

    def test_web_vote_on_old_fingerprint_is_not_counted(self):
        v = self.open_vote()
        path = console.web_votes_path(self.config)
        path.write_text(json.dumps({v["key"]: {self.op: {"answer": "yes", "fingerprint": "old"}}}))
        self.assertEqual(console.web_votes_for(runtime.build(self.config), v), {})

    def test_web_no_declines(self):
        _, cookie = self.login()
        _, _, html_text = self.page(cookie)
        v = self.open_vote()
        self.req("POST", f"/p/{self.config.slug}/vote", {"csrf": self.csrf(html_text), "key": v["key"],
                                                         "answer": "no", "fp": v["fingerprint"]}, cookie)
        tally.run(runtime.build(self.config))
        self.assertEqual(votes.load(runtime.build(self.config))[v["key"]]["status"], "declined")

    def test_pause_and_resume_are_audited(self):
        _, cookie = self.login()
        _, _, html_text = self.page(cookie, f"/p/{self.config.slug}/health")
        self.req("POST", f"/p/{self.config.slug}/pause", {"csrf": self.csrf(html_text)}, cookie)
        self.assertTrue((self.config.state_dir / "PAUSED").exists())
        _, _, html_text = self.page(cookie, f"/p/{self.config.slug}/health")
        self.req("POST", f"/p/{self.config.slug}/resume", {"csrf": self.csrf(html_text)}, cookie)
        self.assertFalse((self.config.state_dir / "PAUSED").exists())
        journal = (self.config.state_dir / "journal.jsonl").read_text()
        self.assertIn("Op One", journal)

    def test_session_cookie_flags(self):
        _, headers, _ = self.req("POST", "/login", {"name": "Op One", "password": PASSWORD})
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_votes_off_refuses_and_shows_why(self):
        srv = console.make_server(self.config, self.auth, "127.0.0.1", 0, vote_off_reason="mirror of the live loop")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port, self.port = self.port, srv.server_address[1]
            _, cookie = self.login()
            _, _, html_text = self.page(cookie)
            self.assertNotIn("Yes, approve", html_text)
            self.assertIn("mirror of the live loop", html_text)
            v = self.open_vote()
            status, _, _ = self.req("POST", f"/p/{self.config.slug}/vote", {"csrf": self.csrf(html_text),
                                    "key": v["key"], "answer": "yes", "fp": v["fingerprint"]}, cookie)
            self.assertEqual(status, 409)
            self.assertFalse(console.web_votes_path(self.config).exists())
            status, _, _ = self.req("POST", f"/p/{self.config.slug}/tick", {"csrf": self.csrf(html_text)}, cookie)
            self.assertEqual(status, 409)
        finally:
            self.port = port
            srv.shutdown()
            srv.server_close()

    def test_invite_works_once_then_is_dead(self):
        token = console.make_invite(self.config, "Op One", self.op)
        self.assertEqual(self.req("GET", f"/invite/{token}")[0], 200)
        self.assertEqual(self.req("POST", f"/invite/{token}", {"password": "short"})[0], 400)
        status, headers, _ = self.req("POST", f"/invite/{token}", {"password": "a brand new password"})
        self.assertEqual(status, 303)
        self.assertIn("sayso_session=", headers.get("Set-Cookie", ""))
        self.assertEqual(self.login(password="a brand new password")[0], 303)
        self.assertEqual(self.req("GET", f"/invite/{token}")[0], 410)
        self.assertEqual(self.req("POST", f"/invite/{token}", {"password": "another new password"})[0], 410)
        self.assertEqual(self.req("GET", "/invite/not-a-real-token")[0], 410)

    def test_expired_invite_refused(self):
        token = console.make_invite(self.config, "Op One", self.op, minutes=-1)
        self.assertEqual(self.req("GET", f"/invite/{token}")[0], 410)

    def test_invite_file_holds_no_token(self):
        token = console.make_invite(self.config, "Op One", self.op)
        self.assertNotIn(token, console.invites_path(self.config).read_text())

    def test_secret_must_be_long(self):
        with self.assertRaises(console.ConsoleError):
            console.Auth(self.config, "short")


if __name__ == "__main__":
    unittest.main()
