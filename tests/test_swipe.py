"""Swipe deck: the console's /swipe page and the JSON vote answer the deck script relies on. Fake data only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import shutil
import unittest
from pathlib import Path

from sayso import runtime, votes
from sayso.brand import swipe
from sayso.jobs import tally
from tests import test_console as _tc


class _Fixture(_tc.ConsoleTests):
    """The console fixture (server, sign-in, helpers) without its tests."""


for _name in [n for n in dir(_tc.ConsoleTests) if n.startswith("test_")]:
    setattr(_Fixture, _name, None)


class SwipeTests(_Fixture):

    def swipe_page(self, cookie):
        return self.req("GET", f"/p/{self.config.slug}/swipe", cookie=cookie)

    def post_swipe(self, cookie, form, headers=True):
        import http.client
        from urllib.parse import urlencode
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"sayso_session={cookie}"}
        if headers:
            h[swipe.SWIPE_HEADER] = "1"
        c.request("POST", f"/p/{self.config.slug}/vote", body=urlencode(form), headers=h)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode()

    def test_swipe_page_needs_sign_in(self):
        status, headers, body = self.swipe_page(None)
        self.assertEqual(status, 303)
        self.assertNotIn("customer.test", body)

    def test_swipe_page_one_card_per_open_vote_bound_to_its_fingerprint(self):
        _, cookie = self.login()
        status, headers, body = self.swipe_page(cookie)
        self.assertEqual(status, 200)
        opened = [v for v in json.loads((self.config.state_dir / "votes.json").read_text()).values()
                  if v["status"] == "open"]
        self.assertTrue(opened)
        self.assertEqual(body.count("class=s-card-swipe"), len(opened))
        for v in opened:
            self.assertIn(f"value='{v['fingerprint']}'", body)
        self.assertIn(f"{len(opened)} left", body)
        self.assertIn("<script src='/static/swipe.js' defer></script>", body)

    def test_swipe_only_the_swipe_page_may_run_script(self):
        _, cookie = self.login()
        _, h, _ = self.swipe_page(cookie)
        self.assertEqual(h["Content-Security-Policy"], swipe.DECK_CSP)
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline'; script", h["Content-Security-Policy"])
        for path in (f"/p/{self.config.slug}/", f"/p/{self.config.slug}/board"):
            _, h2, body2 = self.page(cookie, path)
            self.assertNotIn("script-src", h2["Content-Security-Policy"], path)
            self.assertNotIn("<script", body2, path)

    def test_swipe_script_served_same_origin_after_sign_in(self):
        _, cookie = self.login()
        status, h, body = self.req("GET", "/static/swipe.js", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(h["Content-Type"].startswith("application/javascript"))
        self.assertIn("data-sayso-deck", body)
        self.assertNotRegex(body, r"https?://")       # no third-party requests from the script
        self.assertEqual(self.req("GET", "/static/swipe.js")[0], 303)

    def test_swipe_yes_returns_json_and_counts_through_tally_like_a_tap(self):
        _, cookie = self.login()
        _, _, body = self.swipe_page(cookie)
        v = self.open_vote()
        world = (self.config.state_dir / "fake-world.json").read_text()
        status, h, out = self.post_swipe(cookie, {"csrf": self.csrf(body), "key": v["key"], "answer": "yes",
                                                  "fp": v["fingerprint"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertEqual((self.config.state_dir / "fake-world.json").read_text(), world)  # nothing acts in the request
        tally.run(runtime.build(self.config))
        self.assertEqual(votes.load(runtime.build(self.config))[v["key"]]["status"], "approved")

    def test_swipe_refusals_are_json_with_a_reason_and_record_nothing(self):
        _, cookie = self.login()
        _, _, body = self.swipe_page(cookie)
        v = self.open_vote()
        cases = (({"csrf": self.csrf(body), "key": v["key"], "answer": "yes", "fp": "0" * 64}, 409, "changed"),
                 ({"csrf": "bad", "key": v["key"], "answer": "yes", "fp": v["fingerprint"]}, 403, "expired"),
                 ({"csrf": self.csrf(body), "key": "nope", "answer": "yes", "fp": "x"}, 409, "no such"))
        for form, code, word in cases:
            status, h, out = self.post_swipe(cookie, form)
            self.assertEqual(status, code, form)
            self.assertTrue(h["Content-Type"].startswith("application/json"))
            self.assertIn(word, json.loads(out)["error"])
        self.assertFalse((self.config.state_dir / "web-votes.json").exists())
        status, h, out = self.post_swipe("x.y", {"csrf": "x", "key": v["key"], "answer": "yes", "fp": "x"})
        self.assertEqual(status, 401)
        self.assertIn("Signed out", json.loads(out)["error"])

    def test_swipe_answered_card_leaves_the_deck_until_the_draft_changes(self):
        _, cookie = self.login()
        _, _, body = self.swipe_page(cookie)
        n = body.count("class=s-card-swipe")
        v = self.open_vote()
        self.post_swipe(cookie, {"csrf": self.csrf(body), "key": v["key"], "answer": "yes", "fp": v["fingerprint"]})
        _, _, body2 = self.swipe_page(cookie)
        self.assertEqual(body2.count("class=s-card-swipe"), n - 1)
        self.assertNotIn(f"value='{v['fingerprint']}'", body2)
        # a vote for an older version does not hide the new version
        web = json.loads((self.config.state_dir / "web-votes.json").read_text())
        for ballots in web.values():
            for b in ballots.values():
                b["fingerprint"] = "old"
        (self.config.state_dir / "web-votes.json").write_text(json.dumps(web))
        _, _, body3 = self.swipe_page(cookie)
        self.assertEqual(body3.count("class=s-card-swipe"), n)

    def test_swipe_plain_form_post_still_redirects(self):
        _, cookie = self.login()
        _, _, body = self.swipe_page(cookie)
        v = self.open_vote()
        status, h, _ = self.post_swipe(cookie, {"csrf": self.csrf(body), "key": v["key"], "answer": "no",
                                                "fp": v["fingerprint"]}, headers=False)
        self.assertEqual(status, 303)

    def test_swipe_card_text_is_escaped(self):
        html_ = swipe.card_form(action="/p/x/vote", csrf="c", key="k'><script>", fingerprint="f",
                                question="<img src=x onerror=1>", who="<b>", body_html="ok")
        self.assertNotIn("<img", html_)
        self.assertNotIn("<script>", html_)
        self.assertNotIn("<b>", html_)

    def test_swipe_votes_off_shows_why_and_no_cards(self):
        self.srv.shutdown(); self.srv.server_close()
        from sayso import console
        import threading
        self.srv = console.make_server(self.config, self.auth, "127.0.0.1", 0, vote_off_reason="board is Discord")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        _, cookie = self.login()
        status, _, body = self.swipe_page(cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("s-card-swipe", body)
        self.assertIn("board is Discord", body)

    def test_swipe_tab_hidden_on_read_only_dashboard(self):
        from sayso import dashboard
        s = dashboard.Snapshot(self.config, dashboard.board_reader_for(self.config))
        self.assertNotIn("/swipe", dashboard.render_decisions(s, [self.config.slug]))

    def test_swipe_script_makes_every_card_but_the_top_one_inert(self):
        js = swipe.script_bytes().decode()
        # A card under the top one must never take focus or a click (QA round 4: Tab reached the hidden
        # next card's "Yes, send" and Enter voted on a card the user could not see).
        self.assertIn('c.inert = i !== 0;', js)
        self.assertIn('c.setAttribute("inert", "")', js)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_swipe_script_parses(self):
        js = Path(swipe.__file__).with_name("swipe.js")
        self.assertEqual(subprocess.run(["node", "--check", str(js)], capture_output=True).returncode, 0)


CHROME = os.environ.get("SAYSO_TEST_CHROME", "/home/harry/.local/opt/google-chrome/opt/google/chrome/chrome")
PW_NODE_MODULES = os.environ.get("SAYSO_TEST_PLAYWRIGHT",
                                 "/home/harry/qa/offline-ui-qa-verification/scratch-portable/node_modules")
_HAVE_TOUCH_RIG = bool(shutil.which("node")) and Path(CHROME).exists() and (Path(PW_NODE_MODULES) / "playwright").exists()


@unittest.skipUnless(_HAVE_TOUCH_RIG, "needs node, Chrome and playwright (SAYSO_TEST_CHROME / SAYSO_TEST_PLAYWRIGHT)")
class SwipeTouchTests(_Fixture):
    """Real touch on a phone-sized page (CDP Input.dispatchTouchEvent). On a phone most of the card is its
    text body; a drag that starts there must swipe sideways and scroll up and down (Kaviru, 19 Oct 2026)."""

    def drag(self, axis):
        _, cookie = self.login()
        url = f"http://127.0.0.1:{self.port}/p/{self.config.slug}/swipe"
        env = {**os.environ, "NODE_PATH": PW_NODE_MODULES}
        r = subprocess.run(["node", str(Path(__file__).with_name("swipe_touch.cjs")), CHROME, url,
                            "sayso_session", cookie, axis], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def web_votes(self):
        p = self.config.state_dir / "web-votes.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def test_touch_drag_sideways_from_card_body_moves_card_and_casts_vote(self):
        out = self.drag("x")
        self.assertTrue(out["during"]["transform"].startswith("translate("),
                        f"card did not move on a body drag: {out}")
        self.assertNotIn("pointercancel", out["after"]["events"], out)
        self.assertEqual(out["after"]["gone"], 1, out)
        ballots = self.web_votes().get(out["geo"]["key"], {})
        self.assertEqual([b["answer"] for b in ballots.values()], ["yes"], self.web_votes())
        v = json.loads((self.config.state_dir / "votes.json").read_text())[out["geo"]["key"]]
        self.assertEqual([b["fingerprint"] for b in ballots.values()], [v["fingerprint"]])

    def test_touch_drag_up_and_down_in_card_body_scrolls_and_casts_nothing(self):
        out = self.drag("y")
        self.assertGreater(out["geo"]["h"], out["geo"]["ch"], out)      # the body really can scroll
        self.assertGreater(out["during"]["scrollTop"], 0, f"body did not scroll: {out}")
        self.assertIn(out["during"]["transform"], ("", None), out)
        self.assertEqual(out["after"]["gone"], 0, out)
        self.assertEqual(self.web_votes(), {})


if __name__ == "__main__":
    unittest.main()
