"""Slack board against a fake Slack built from Slack's OWN documented responses. No network, no account.

Every response shape below is copied from docs.slack.dev (chat.postMessage, chat.update, reactions.get,
users.info, auth.test, conversations.replies). A socket guard refuses any non-localhost connection for
the whole module; ``test_socket_guard_bites`` proves it by trying one real outside call.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from sayso import config as config_mod
from sayso.adapters import reference, slack
from sayso.adapters.http import urllib_transport
from sayso.config import ConfigError, Section

# --------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo
LOCAL = {"localhost", "127.0.0.1", "::1"}


class OutsideConnection(AssertionError):
    pass


def _guard_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in LOCAL and not str(host).startswith("/"):
        raise OutsideConnection(f"test tried to reach {host!r}")
    return _real_connect(self, address)


def _guard_getaddrinfo(host, *a, **kw):
    if host not in LOCAL:
        raise OutsideConnection(f"test tried to resolve {host!r}")
    return _real_getaddrinfo(host, *a, **kw)


def setUpModule():
    socket.socket.connect = _guard_connect
    socket.getaddrinfo = _guard_getaddrinfo


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.getaddrinfo = _real_getaddrinfo


# --------------------------------------------------------------------------- fake Slack
BOT = "U0KRQLJ9H"          # oauth.v2.access example bot_user_id
BOT_ID = "BZYBOTHED"       # auth.test example bot_id
TEAM = "T9TK3CUKW"
CHAN = "C123ABC456"        # chat.postMessage example channel
ALERTS = "C0NF841BK"       # reactions.get example channel
OPS = ("W222222", "W333333")
OTHER_BOT = "B0OTHERBOT"


class FakeSlack:
    """A tiny stateful Slack. Answers in the documented shapes; records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.msgs: dict[str, dict[str, dict]] = {CHAN: {}, ALERTS: {}}
        self.users = {u: {"id": u, "name": u.lower(), "is_bot": False, "deleted": False} for u in OPS}
        self.users["U0BOTUSER"] = {"id": "U0BOTUSER", "is_bot": True, "deleted": False}
        self.users["W999999"] = {"id": "W999999", "is_bot": False, "deleted": False}   # a stranger
        self.seq = 1503435956
        self.fail_next: list[tuple[int, dict, dict]] = []   # (status, headers, body) queued answers
        self.drop_after_post = 0      # number of postMessage calls that succeed on Slack but lose the answer
        self.partial = False

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "slack.com" and parsed.path.startswith("/api/"), url
        assert headers["Authorization"] == "Bearer xoxb-test"
        name = parsed.path[len("/api/"):]
        args = dict(urllib.parse.parse_qsl(parsed.query)) if method == "GET" else json.loads(body or b"{}")
        self.calls.append({"method": name, "http": method, "args": args})
        if self.fail_next:
            status, h, b = self.fail_next.pop(0)
            return status, h, json.dumps(b).encode()
        out = getattr(self, "_" + name.replace(".", "_"))(args)
        return 200, {"Content-Type": "application/json"}, json.dumps(out).encode()

    def _ts(self):
        self.seq += 1
        return f"{self.seq}.000247"

    def _auth_test(self, a):
        return {"ok": True, "url": "https://subarachnoid.slack.com/", "team": "Subarachnoid Workspace",
                "user": "bot", "team_id": TEAM, "user_id": BOT, "bot_id": BOT_ID}

    def _chat_postMessage(self, a):
        ch = a["channel"]
        if ch not in self.msgs:
            return {"ok": False, "error": "channel_not_found"}
        ts = self._ts()
        msg = {"type": "message", "text": a["text"], "user": BOT, "bot_id": BOT_ID, "ts": ts, "team": TEAM}
        if a.get("thread_ts"):
            msg["thread_ts"] = a["thread_ts"]
        self.msgs[ch][ts] = msg
        if self.drop_after_post:
            self.drop_after_post -= 1
            raise ConnectionResetError("answer lost after Slack stored the message")
        return {"ok": True, "channel": ch, "ts": ts, "message": {**msg, "subtype": "bot_message"}}

    def _chat_update(self, a):
        msg = self.msgs.get(a["channel"], {}).get(a["ts"])
        if msg is None:
            return {"ok": False, "error": "message_not_found"}
        if msg.get("user") != BOT:
            return {"ok": False, "error": "cant_update_message"}
        msg["text"] = a["text"]
        return {"ok": True, "channel": a["channel"], "ts": a["ts"], "text": a["text"],
                "message": {"text": a["text"], "user": BOT}}

    def _reactions_get(self, a):
        msg = self.msgs.get(a["channel"], {}).get(a["timestamp"])
        if msg is None:
            return {"ok": False, "error": "message_not_found"}
        out = dict(msg)
        if self.partial and out.get("reactions"):
            out["reactions"] = [{**r, "users": r["users"][:1]} for r in out["reactions"]]
        return {"ok": True, "type": "message", "message": out, "channel": a["channel"]}

    def _users_info(self, a):
        u = self.users.get(a["user"])
        return {"ok": True, "user": u} if u else {"ok": False, "error": "user_not_found"}

    def _conversations_replies(self, a):
        ch, ts = a["channel"], a["ts"]
        thread = [m for m in self.msgs[ch].values() if m["ts"] == ts or m.get("thread_ts") == ts]
        thread.sort(key=lambda m: float(m["ts"]))
        start = int(a.get("cursor") or 0)
        page = thread[start:start + 2]                       # tiny pages so paging is exercised
        more = start + 2 < len(thread)
        return {"messages": page, "has_more": more, "ok": True,
                "response_metadata": {"next_cursor": str(start + 2) if more else ""}}

    # -- test helpers ------------------------------------------------------------------------------
    def react(self, ts, name, *users, channel=CHAN):
        msg = self.msgs[channel][ts]
        rs = msg.setdefault("reactions", [])
        row = next((r for r in rs if r["name"] == name), None)
        if row is None:
            row = {"name": name, "users": [], "count": 0}
            rs.append(row)
        for u in users:
            row["users"].append(u)
        row["count"] = len(row["users"])

    def human_reply(self, thread_ts, user, text):
        ts = self._ts()
        self.msgs[CHAN][ts] = {"type": "message", "user": user, "text": text, "ts": ts, "thread_ts": thread_ts}
        return ts

    def posts(self):
        return [c for c in self.calls if c["method"] == "chat.postMessage"]


ENV = {"T_SLACK": "xoxb-test"}


@mock.patch.dict(os.environ, ENV)
class SlackBoardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-slack-")
        self.tmp = Path(self._tmp.name)
        self.slack = FakeSlack()
        self.waits: list[float] = []
        self.now = [1000.0]

    def tearDown(self):
        self._tmp.cleanup()

    def board(self, **opts):
        section = Section("slack", {"channel_id": CHAN, "alerts_channel_id": ALERTS, "token_env": "T_SLACK", **opts})
        return slack.SlackBoard(section, self.tmp, transport=self.slack, sleep=self.sleep, clock=lambda: self.now[0])

    def sleep(self, s):
        self.waits.append(s)
        self.now[0] += s

    def card(self, b, key="c1", body="Customer wrote: export fails", tags=("New",)):
        return b.ensure_card(key, "Pat - export fails", body, list(tags))

    # -- socket guard ------------------------------------------------------------------------------
    def test_socket_guard_bites(self):
        """Seeded outside call: the real transport must be refused before it leaves this machine."""
        with self.assertRaises(OutsideConnection):
            urllib_transport("POST", "https://slack.com/api/auth.test", {}, b"")

    # -- config ------------------------------------------------------------------------------------
    def test_config_choice_and_build_branch(self):
        self.assertIn("slack", config_mod.ADAPTER_CHOICES["board"])
        cfg = mock.Mock(state_dir=self.tmp)
        b = reference.build("board", "slack", Section("slack", {"channel_id": CHAN, "token_env": "T_SLACK"}), cfg)
        self.assertIsInstance(b, slack.SlackBoard)
        self.assertEqual(b.alerts, CHAN, "alerts default to the case channel")

    def test_needs_channel_and_token(self):
        with self.assertRaises(ConfigError):
            slack.SlackBoard(Section("slack", {"token_env": "T_SLACK"}), self.tmp)
        with self.assertRaises(ConfigError):
            slack.SlackBoard(Section("slack", {"channel_id": "general", "token_env": "T_SLACK"}), self.tmp)
        with self.assertRaises(ConfigError):
            slack.SlackBoard(Section("slack", {"channel_id": CHAN, "token_env": "MISSING_ENV"}), self.tmp)

    def test_real_adapter_needs_live_mode(self):
        raw = {"product": {"slug": "acme", "support_address": "s@acme.test"}, "operators": {"W222222": "Pat"},
               "state": {"dir": str(self.tmp)}, "board": {"adapter": "slack", "channel_id": CHAN, "token_env": "T"}}
        with self.assertRaises(ConfigError):
            config_mod.parse(raw, self.tmp / "x.toml")
        raw["product"]["mode"] = "live"
        self.assertEqual(config_mod.parse(raw, self.tmp / "x.toml").section("board").adapter, "slack")

    # -- cards and stages --------------------------------------------------------------------------
    def test_one_parent_per_case_with_thread_and_stage(self):
        b = self.board()
        card = self.card(b, body="word " * 1500)          # long email: several thread parts
        parent = self.slack.msgs[CHAN][card]
        self.assertIn("Stage: New", parent["text"])
        self.assertIn("`case:c1`", parent["text"], "marker in the parent itself")
        replies = [m for m in self.slack.msgs[CHAN].values() if m.get("thread_ts") == card]
        self.assertGreaterEqual(len(replies), 2)
        self.assertTrue(all(len(m["text"]) <= slack.LIMIT for m in replies))
        self.assertEqual(self.card(b, body="word " * 1500), card, "same key, same card")
        self.assertEqual(len([m for m in self.slack.msgs[CHAN].values() if "thread_ts" not in m]), 1)
        self.assertEqual(b.get_tags(card), ["New"])

    def test_lost_answer_on_a_later_part_finishes_the_same_card(self):
        b = self.board()
        self.slack.drop_after_post = 0
        real = self.slack._chat_postMessage
        n = {"i": 0}

        def flaky(a):
            n["i"] += 1
            if n["i"] == 3:
                raise ConnectionResetError("network")
            return real(a)
        self.slack._chat_postMessage = flaky
        with self.assertRaises(ConnectionResetError):
            self.card(b, body="word " * 1500)
        self.slack._chat_postMessage = real
        card = self.card(b, body="word " * 1500)
        parents = [m for m in self.slack.msgs[CHAN].values() if "thread_ts" not in m]
        self.assertEqual(len(parents), 1, "one case is always one parent message")
        self.assertEqual(parents[0]["ts"], card)

    def test_set_tags_edits_the_parent_and_reads_back(self):
        b = self.board()
        card = self.card(b)
        before = len(self.slack.posts())
        b.set_tags(card, ["In support", "Waiting on customer"])
        self.assertEqual(len(self.slack.posts()), before, "a stage change is an edit, never a new message")
        self.assertEqual(b.get_tags(card), ["In support", "Waiting on customer"])
        self.assertIn("`case:c1`", self.slack.msgs[CHAN][card]["text"])

    def test_tags_read_from_slack_not_from_memory(self):
        b = self.board()
        card = self.card(b)
        self.slack.msgs[CHAN][card]["text"] = self.slack.msgs[CHAN][card]["text"].replace("Stage: New", "Stage: Done")
        self.assertEqual(b.get_tags(card), ["Done"])

    def test_parent_without_one_stage_line_is_refused(self):
        b = self.board()
        card = self.card(b)
        self.slack.msgs[CHAN][card]["text"] += "\nStage: Done"
        with self.assertRaises(slack.SlackError):
            b.get_tags(card)
        self.slack.msgs[CHAN][card]["text"] = "hello"
        with self.assertRaises(slack.SlackError):
            b.set_tags(card, ["Done"])

    def test_set_tags_refuses_a_message_that_is_not_ours(self):
        b = self.board()
        card = self.card(b)
        self.slack.msgs[CHAN][card]["user"] = "W222222"
        self.slack.msgs[CHAN][card]["bot_id"] = None
        with self.assertRaises(slack.SlackError):
            b.set_tags(card, ["Done"])
        self.assertFalse([c for c in self.slack.calls if c["method"] == "chat.update"])

    # -- posts: idempotency and read-back ----------------------------------------------------------
    def test_post_same_key_posts_once(self):
        b = self.board()
        card = self.card(b)
        n = len(self.slack.posts())
        m1 = b.post(card, "Draft v1", "k1")
        m2 = b.post(card, "Draft v1", "k1")
        self.assertEqual(m1, m2)
        self.assertEqual(len(self.slack.posts()), n + 1)
        self.assertEqual(self.slack.msgs[CHAN][m1]["thread_ts"], card)

    def test_post_record_survives_a_new_board_object(self):
        b = self.board()
        card = self.card(b)
        b.post(card, "x", "k1")
        n = len(self.slack.posts())
        self.board().post(card, "x", "k1")
        self.assertEqual(len(self.slack.posts()), n, "the write record lives in the state dir, not in memory")

    def test_failed_readback_is_not_success_and_retry_does_not_repost(self):
        b = self.board()
        card = self.card(b)
        real = self.slack._reactions_get
        self.slack._reactions_get = lambda a: {"ok": False, "error": "internal_error"}
        with self.assertRaises(slack.SlackError):
            b.post(card, "Draft", "k2")
        self.slack._reactions_get = real
        n = len(self.slack.posts())
        b.post(card, "Draft", "k2")
        self.assertEqual(len(self.slack.posts()), n, "posted but unproven: re-read, never re-post")

    def test_readback_refuses_someone_elses_message(self):
        b = self.board()
        card = self.card(b)
        real = self.slack._chat_postMessage

        def post_as_human(a):
            out = real(a)
            self.slack.msgs[a["channel"]][out["ts"]].update(user="W222222", bot_id=None)
            return out
        self.slack._chat_postMessage = post_as_human
        with self.assertRaises(slack.SlackError):
            b.post(card, "x", "k3")

    def test_ok_false_raises_with_the_slack_error(self):
        b = self.board()
        self.slack.fail_next.append((200, {}, {"ok": False, "error": "not_in_channel"}))
        with self.assertRaises(slack.SlackError) as cm:
            self.card(b)
        self.assertEqual(cm.exception.error, "not_in_channel")

    def test_text_is_escaped_and_posted_with_parse_none(self):
        b = self.board()
        card = self.card(b, body="Hi <!channel> and <@W333333> & <https://evil.example|click>")
        call = self.slack.posts()[1]["args"]
        self.assertEqual(call["parse"], "none")
        self.assertFalse(call["link_names"])
        self.assertNotIn("<!channel>", call["text"])
        self.assertNotIn("<@W333333>", call["text"])
        self.assertIn("&lt;!channel&gt;", call["text"])
        self.assertEqual(b.messages(card)[0], "Hi <!channel> and <@W333333> & <https://evil.example|click>")

    # -- rate limits -------------------------------------------------------------------------------
    def test_429_waits_the_retry_after_header(self):
        b = self.board()
        self.slack.fail_next.append((429, {"Retry-After": "7"}, {"ok": False, "error": "ratelimited"}))
        self.card(b)
        self.assertIn(7.0, self.waits)

    def test_rate_limit_gives_up_after_three_waits(self):
        b = self.board()
        for _ in range(4):
            self.slack.fail_next.append((429, {"retry-after": "100"}, {"ok": False, "error": "ratelimited"}))
        with self.assertRaises(slack.SlackError):
            self.card(b)
        self.assertEqual(self.waits.count(slack.MAX_WAIT), 3, "capped at 30 s, 3 tries")

    def test_no_retries_when_a_caller_asks_for_none(self):
        b = self.board()
        b.max_retries = 0
        self.slack.fail_next.append((429, {"Retry-After": "5"}, {"ok": False, "error": "ratelimited"}))
        with self.assertRaises(slack.SlackError):
            b.me()
        self.assertEqual(self.waits, [])

    def test_posts_are_paced_one_per_second_per_channel(self):
        b = self.board()
        card = self.card(b)
        self.waits.clear()
        b.post(card, "a", "p1")
        b.post(card, "b", "p2")
        self.assertTrue(self.waits and all(w <= 1.0 for w in self.waits))
        self.assertAlmostEqual(sum(self.waits), 2.0, places=3)

    # -- polls -------------------------------------------------------------------------------------
    def poll(self, b):
        card = self.card(b)
        pid = b.open_poll(card, "Pat - export fails - Send this exact reply?", "reply:c1:v1")
        return card, pid

    def test_votes_from_reactions_bots_skipped(self):
        b = self.board()
        card, pid = self.poll(b)
        self.slack.react(pid, "white_check_mark", "W222222", "U0BOTUSER", BOT)
        self.slack.react(pid, "thumbsup", "W333333")
        self.assertEqual(b.read_poll(card, pid).votes, {"W222222": "yes"})

    def test_any_no_from_a_person_wins_over_their_yes(self):
        b = self.board()
        card, pid = self.poll(b)
        self.slack.react(pid, "white_check_mark", "W222222")
        self.slack.react(pid, "x", "W222222")
        self.assertEqual(b.read_poll(card, pid).votes, {"W222222": "no"})

    def test_skin_tone_variant_counts_as_the_same_emoji(self):
        b = self.board()
        card, pid = self.poll(b)
        self.slack.react(pid, "white_check_mark::skin-tone-2", "W222222")
        self.assertEqual(b.read_poll(card, pid).votes, {"W222222": "yes"})

    def test_partial_reaction_list_is_refused(self):
        b = self.board()
        card, pid = self.poll(b)
        self.slack.react(pid, "x", "W222222", "W333333")
        self.slack.partial = True
        with self.assertRaises(slack.SlackError):
            b.read_poll(card, pid)
        self.assertEqual(self.slack.calls[-1]["args"].get("full"), "true")

    def test_vote_on_a_message_we_did_not_open_is_refused(self):
        b = self.board()
        card, pid = self.poll(b)
        human = self.slack.human_reply(card, "W222222", "approve pls")
        self.slack.react(human, "white_check_mark", "W222222")
        with self.assertRaises(slack.SlackError):
            b.read_poll(card, human)

    def test_unrecorded_vote_message_is_refused_even_if_it_looks_right(self):
        """Our own message, right thread, right marker, but this board never recorded opening it."""
        b = self.board()
        card, pid = self.poll(b)
        data = json.loads((self.tmp / "slack-board.json").read_text())
        data["polls"] = {}
        (self.tmp / "slack-board.json").write_text(json.dumps(data))
        self.slack.react(pid, "white_check_mark", "W222222")
        with self.assertRaises(slack.SlackError):
            b.read_poll(card, pid)

    def test_poll_for_another_card_is_refused(self):
        b = self.board()
        card, pid = self.poll(b)
        other = b.ensure_card("c2", "Sam - refund", "body", ["New"])
        with self.assertRaises(slack.SlackError):
            b.read_poll(other, pid)

    def test_poll_that_lost_its_marker_is_refused(self):
        b = self.board()
        card, pid = self.poll(b)
        self.slack.msgs[CHAN][pid]["text"] = "Vote: something else"
        with self.assertRaises(slack.SlackError):
            b.read_poll(card, pid)

    def test_open_poll_same_key_once(self):
        b = self.board()
        card, pid = self.poll(b)
        n = len(self.slack.posts())
        self.assertEqual(b.open_poll(card, "Pat - export fails - Send this exact reply?", "reply:c1:v1"), pid)
        self.assertEqual(len(self.slack.posts()), n)

    def test_close_poll_edits_once(self):
        b = self.board()
        card, pid = self.poll(b)
        b.close_poll(card, pid)
        b.close_poll(card, pid)
        self.assertEqual(len([c for c in self.slack.calls if c["method"] == "chat.update"]), 1)
        self.assertIn("Voting is closed", self.slack.msgs[CHAN][pid]["text"])

    # -- alerts and notices ------------------------------------------------------------------------
    def test_notify_pings_only_the_given_user(self):
        b = self.board()
        b.notify("Vote for <@W333333> and <!here> now", "n1", mention="W222222")
        text = self.slack.posts()[-1]["args"]["text"]
        self.assertTrue(text.startswith("<@W222222> "))
        self.assertEqual(text.count("<@"), 1)
        self.assertNotIn("<!here>", text)
        self.assertEqual(self.slack.posts()[-1]["args"]["channel"], ALERTS)

    def test_notify_without_a_valid_user_pings_nobody(self):
        b = self.board()
        b.notify("hello", "n2", mention="all")
        self.assertNotIn("<@", self.slack.posts()[-1]["args"]["text"])

    def test_alert_and_notice_once_per_key(self):
        b = self.board()
        b.alert("broken", "a1")
        b.alert("broken", "a1")
        b.notify("hi", "n3")
        b.notify("hi", "n3")
        self.assertEqual(len(self.slack.posts()), 2)

    # -- thread reads ------------------------------------------------------------------------------
    def test_messages_authors_activity_read_every_page(self):
        b = self.board()
        card = self.card(b)
        for i in range(4):
            b.post(card, f"bot {i}", f"b{i}")
        last = self.slack.human_reply(card, "W333333", "I'll take this")
        self.assertEqual(b.messages(card)[-1], "I'll take this")
        self.assertEqual(len(b.messages(card)), 6)
        self.assertEqual([a for a, _ in b.message_authors(card)], ["W333333"], "bots are never owners")
        self.assertEqual(b.last_activity(card), b.message_authors(card)[-1][1])
        self.assertTrue(b.last_activity(card).endswith("+00:00"))
        self.assertTrue(last)

    def test_stage_edit_is_not_activity(self):
        b = self.board()
        card = self.card(b)
        before = b.last_activity(card)
        b.set_tags(card, ["In support"])
        self.assertEqual(b.last_activity(card), before)


if __name__ == "__main__":
    unittest.main()


@mock.patch.dict(os.environ, ENV)
class SlackLoopTests(unittest.TestCase):
    """The real core jobs, end to end, with the Slack board in place of the fake board."""

    def setUp(self):
        import dataclasses
        from sayso import demo
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-slack-loop-")
        self.tmp = Path(self._tmp.name)
        self.ctx = demo.build(self.tmp)
        self.ctx.config = dataclasses.replace(self.ctx.config, operators={"W222222": "Pat Operator"})
        self.slack = FakeSlack()
        self.ctx.board = slack.SlackBoard(Section("slack", {"channel_id": CHAN, "alerts_channel_id": ALERTS,
                                                             "token_env": "T_SLACK"}),
                                          self.ctx.config.state_dir, transport=self.slack, sleep=lambda s: None)

    def tearDown(self):
        self._tmp.cleanup()

    def tick(self):
        from sayso.jobs import JOBS, ORDER
        for name in ORDER:
            JOBS[name](self.ctx, **({"sleep": lambda s: None} if name == "sender" else {}))

    def reply_vote(self):
        from sayso import votes
        live = [v for v in votes.load(self.ctx).values() if v["kind"] == "reply" and v["status"] == votes.OPEN]
        self.assertEqual(len(live), 1, votes.load(self.ctx))
        return live[0]

    def test_linked_reaction_approves_exact_version_and_reply_is_sent(self):
        from sayso import votes
        self.ctx.world.customer_writes(sender="pat@customer.test", subject="Export button does nothing",
                                       body="It fails.")
        self.tick()
        rec = self.reply_vote()
        parents = [m for m in self.slack.msgs[CHAN].values() if "thread_ts" not in m]
        self.assertEqual(len(parents), 1)
        self.assertIn("Stage: Awaiting approval", parents[0]["text"])
        self.slack.react(rec["poll_id"], "white_check_mark", "W999999")      # not linked: an opinion only
        self.tick()
        self.assertEqual(votes.load(self.ctx)[rec["key"]]["status"], votes.OPEN)
        self.slack.react(rec["poll_id"], "white_check_mark", "W222222")
        self.tick()
        self.assertIn(votes.load(self.ctx)[rec["key"]]["status"], (votes.APPROVED, votes.DONE))
        self.assertEqual(len(self.ctx.world.data["outbox"]), 1)
        self.assertEqual(len([m for m in self.slack.msgs[CHAN].values() if "thread_ts" not in m]), 1,
                         "still one parent message for the case")

    def test_linked_no_declines(self):
        from sayso import votes
        self.ctx.world.customer_writes(sender="pat@customer.test", subject="Export button does nothing",
                                       body="It fails.")
        self.tick()
        rec = self.reply_vote()
        self.slack.react(rec["poll_id"], "x", "W222222")
        self.tick()
        self.assertEqual(votes.load(self.ctx)[rec["key"]]["status"], votes.DECLINED)
        self.assertEqual(self.ctx.world.data["outbox"], [])
