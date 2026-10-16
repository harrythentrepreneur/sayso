"""Slack board: one parent message per case in ONE channel, everything else in its thread.

Settings (``[board] adapter = "slack"``):
  channel_id         the channel that holds one parent message per case (C... or G...)
  alerts_channel_id  optional; alerts and decision notices go here (default: channel_id)
  token_env          names the env var holding the bot token (xoxb-...)

Bot scopes: chat:write, reactions:read, users:read, channels:history (groups:history for a private
channel). Invite the bot to the channel; it never asks for chat:write.public.

How each Board call maps to Slack (docs/ADAPTERS.md, "Slack"):
  ensure_card   chat.postMessage (parent: title, ``Stage:`` line, ``case:<key>`` marker), then the first
                message as thread replies
  get_tags      reactions.get on the parent (it returns the message text), exactly one ``Stage:`` line
  set_tags      chat.update of the parent's ``Stage:`` line (an edit: never a new message)
  post          chat.postMessage with thread_ts = the parent
  open_poll     a thread reply with a ``vote:<hash>`` marker; people react white_check_mark (yes) or x (no)
  read_poll     reactions.get(full=true) on that exact reply. Bots never count (users.info is_bot)
  alert/notify  chat.postMessage in the alerts channel; notify pings only the one given user
  messages, last_activity, message_authors   conversations.replies (all pages)

Rules the code holds:
- Slack has no idempotency key for chat.postMessage. Every write key is recorded in
  ``<state>/slack-board.json`` the moment Slack returns the message ts, so a retry never posts twice; a
  multi-part first message records each part as it lands, so one case is always one parent message. A
  crash between Slack's answer and that record can still leave one orphan; the parent's ``case:<key>``
  marker lets a person find it.
- Every write is read back (reactions.get on the written ts: it exists, in the right thread, by this bot).
- All text is escaped (``& < >``), so a customer email that says ``<!channel>`` pings nobody.
- HTTP 429 or ``ratelimited``: wait the ``Retry-After`` header (Slack puts it in the header), at most
  3 times, then raise. Posts are paced to one per second per channel, as Slack asks.
- Anything not understood raises: ``ok: false``, a vote message that is not ours, a reaction list that
  is shorter than its count, a parent without exactly one ``Stage:`` line.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from sayso.adapters.base import PollResult
from sayso.adapters.http import urllib_transport
from sayso.config import ConfigError, Section
from sayso.store import load_json, locked, save_json

API = "https://slack.com/api"
CHANNEL = re.compile(r"[CG][A-Z0-9]{6,20}")
USER = re.compile(r"[UW][A-Z0-9]{6,20}")
TS = re.compile(r"[0-9]{9,11}\.[0-9]{6}")
YES, NO = "white_check_mark", "x"
STAGE = "Stage: "
SEP = " · "
LIMIT = 3500            # chat.postMessage: "limit the number of characters in the text field to 4,000"
MAX_RETRIES = 3
MAX_WAIT = 30.0
POST_GAP = 1.0          # "apps may post no more than one message per second per channel"


class SlackError(RuntimeError):
    """Slack said no, or said something this adapter does not understand. Never guessed around."""

    def __init__(self, method: str, error: str):
        super().__init__(f"slack {method}: {error}")
        self.method = method
        self.error = error


def escape(text: str) -> str:
    """Slack's three control characters. After this, <!channel>, <!here> and <@U..> are plain text."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    return str(text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    out, rest = [], str(text).strip()
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return out + ([rest] if rest else [])


def vote_marker(idempotency_key: str) -> str:
    return "vote:" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]


def _header(headers, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if str(k).lower() == name.lower():
            return str(v)
    return None


class SlackBoard:
    API = API

    def __init__(self, section: Section | None, state_dir=None, transport=None, sleep=time.sleep,
                 clock=time.monotonic, *, token: str | None = None, channel_id: str | None = None,
                 alerts_channel_id: str | None = None):
        opts = dict(section.options) if section is not None else {}
        self.channel = str(channel_id or opts.get("channel_id") or "")
        self.alerts = str(alerts_channel_id or opts.get("alerts_channel_id") or self.channel)
        if not CHANNEL.fullmatch(self.channel) or not CHANNEL.fullmatch(self.alerts):
            raise ConfigError("[board] slack needs channel_id (C... or G...); alerts_channel_id is optional")
        self.token = token if token is not None else (section.secret("token") if section is not None else "")
        if not self.token:
            raise ConfigError("[board] slack: the bot token is missing")
        if state_dir is None:
            raise ConfigError("[board] slack needs a state directory for its write record")
        self.keys_path = Path(state_dir) / "slack-board.json"
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.clock = clock
        self.max_retries = MAX_RETRIES      # a web page that calls Slack may set 0: never block a request
        self._last_post: dict[str, float] = {}
        self._me: dict | None = None
        self._users: dict[str, dict] = {}

    # -- HTTP ----------------------------------------------------------------------------------------
    def call(self, method: str, *, get: bool = False, **args):
        """One Web API call. Returns the parsed body when ok is true; raises otherwise."""
        headers = {"Authorization": "Bearer " + self.token, "User-Agent": "sayso/0.2"}
        url, body = f"{self.API}/{method}", None
        if get:
            url += "?" + urllib.parse.urlencode({k: v for k, v in args.items() if v is not None})
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps({k: v for k, v in args.items() if v is not None}).encode()
        for attempt in range(self.max_retries + 1):
            status, resp_headers, raw = self.transport("GET" if get else "POST", url, headers, body)
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = None
            limited = status == 429 or (isinstance(data, dict) and data.get("error") == "ratelimited")
            if limited:
                if attempt >= self.max_retries:
                    raise SlackError(method, "ratelimited")
                try:
                    wait = float(_header(resp_headers, "Retry-After") or 1)
                except ValueError:
                    wait = 1.0
                self.sleep(min(max(wait, 0.0), MAX_WAIT))
                continue
            if status >= 400 or not isinstance(data, dict):
                raise SlackError(method, f"HTTP {status}")
            if data.get("ok") is not True:
                raise SlackError(method, str(data.get("error") or "not ok"))
            return data
        raise SlackError(method, "ratelimited")  # pragma: no cover - loop always returns or raises

    def _pace(self, channel: str) -> None:
        last = self._last_post.get(channel)
        if last is not None:
            gap = self.clock() - last
            if gap < POST_GAP:
                self.sleep(POST_GAP - gap)
        self._last_post[channel] = self.clock()

    def _post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        self._pace(channel)
        out = self.call("chat.postMessage", channel=channel, text=text, thread_ts=thread_ts,
                        parse="none", link_names=False, unfurl_links=False, unfurl_media=False)
        ts = str(out.get("ts") or "")
        if not TS.fullmatch(ts) or str(out.get("channel") or channel) != channel:
            raise SlackError("chat.postMessage", "answer without a message ts")
        return ts

    # -- write record ----------------------------------------------------------------------------------
    def _record(self, key: str, value) -> None:
        with locked(self.keys_path):
            data = load_json(self.keys_path)
            data[key] = value
            save_json(self.keys_path, data)

    def _recorded(self, key: str):
        return load_json(self.keys_path).get(key)

    # -- reads ---------------------------------------------------------------------------------------
    def me(self) -> dict:
        if self._me is None:
            out = self.call("auth.test")
            if not USER.fullmatch(str(out.get("user_id") or "")):
                raise SlackError("auth.test", "no bot user id")
            self._me = {"user_id": out["user_id"], "bot_id": str(out.get("bot_id") or ""),
                        "team_id": str(out.get("team_id") or "")}
        return self._me

    def _message(self, channel: str, ts: str) -> dict:
        """The message at ts, through reactions.get (needs only reactions:read)."""
        out = self.call("reactions.get", get=True, channel=channel, timestamp=ts, full="true")
        msg = out.get("message")
        if out.get("type") != "message" or not isinstance(msg, dict) or str(msg.get("ts") or ts) != ts:
            raise SlackError("reactions.get", "not a message")
        return msg

    def _ours(self, msg: dict) -> bool:
        me = self.me()
        return msg.get("user") == me["user_id"] or bool(me["bot_id"] and msg.get("bot_id") == me["bot_id"])

    def _readback(self, channel: str, ts: str, thread_ts: str | None) -> dict:
        """Every write is read back: the message exists, was written by this bot, in the right thread."""
        msg = self._message(channel, ts)
        if not self._ours(msg):
            raise SlackError("readback", "the message is not this bot's")
        if thread_ts and msg.get("thread_ts") not in (None, thread_ts):
            raise SlackError("readback", "the message is in another thread")
        return msg

    def _is_bot(self, uid: str) -> bool:
        if uid == "USLACKBOT" or uid == self.me()["user_id"]:
            return True
        if uid not in self._users:
            out = self.call("users.info", get=True, user=uid)
            user = out.get("user")
            if not isinstance(user, dict) or user.get("id") != uid:
                raise SlackError("users.info", "no such user")
            self._users[uid] = user
        user = self._users[uid]
        return bool(user.get("is_bot") or user.get("is_app_user") or user.get("deleted"))

    def _replies(self, card_id: str) -> list[dict]:
        rows, cursor = [], None
        while True:
            out = self.call("conversations.replies", get=True, channel=self.channel, ts=card_id,
                            limit=200, cursor=cursor)
            page = out.get("messages")
            if not isinstance(page, list):
                raise SlackError("conversations.replies", "no messages list")
            rows += page
            cursor = (out.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return sorted((m for m in rows if isinstance(m, dict) and TS.fullmatch(str(m.get("ts") or ""))),
                      key=lambda m: float(m["ts"]))

    # -- parent message (stage line) -----------------------------------------------------------------
    @staticmethod
    def _parent_text(title: str, tags: list[str], key: str) -> str:
        for t in tags:
            if "\n" in t or SEP.strip() in t:
                raise ConfigError(f"a stage or label cannot contain a line break or '{SEP.strip()}': {t!r}")
        return f"*{escape(title[:200])}*\n{STAGE}{escape(SEP.join(tags)) or '-'}\n`case:{escape(key)}`"

    @staticmethod
    def _stage_of(text: str) -> list[str]:
        lines = [ln for ln in unescape(text).splitlines() if ln.startswith(STAGE)]
        if len(lines) != 1 or "`case:" not in unescape(text):
            raise SlackError("stage", "the case message has no single Stage line")
        value = lines[0][len(STAGE):].strip()
        return [] if value == "-" else [t for t in value.split(SEP)]

    def ensure_card(self, idempotency_key, title, first_message, tags):
        key = "card:" + idempotency_key
        parts = chunks(escape(first_message))
        rec = self._recorded(key)
        if rec is None:
            ts = self._post(self.channel, self._parent_text(title, list(tags), idempotency_key))
            rec = {"ts": ts, "parts": 0, "total": len(parts)}
            self._record(key, rec)
        ts = rec["ts"]
        for i in range(int(rec.get("parts", 0)), len(parts)):
            self._post(self.channel, parts[i], thread_ts=ts)
            rec = {**rec, "parts": i + 1}
            self._record(key, rec)
        if not rec.get("checked"):
            msg = self._readback(self.channel, ts, None)
            if f"case:{idempotency_key}" not in unescape(msg.get("text") or ""):
                raise SlackError("readback", "the case message lost its marker")
            self._record(key, {**rec, "checked": True})
        return ts

    def get_tags(self, card_id):
        return self._stage_of(self._message(self.channel, card_id).get("text") or "")

    def set_tags(self, card_id, tags):
        msg = self._message(self.channel, card_id)
        if not self._ours(msg):
            raise SlackError("chat.update", "the case message is not this bot's")
        text = unescape(msg.get("text") or "")
        self._stage_of(text)                       # refuse anything that is not one of our parents
        line = STAGE + (SEP.join(tags) or "-")
        if any("\n" in t or SEP.strip() in t for t in tags):
            raise ConfigError("a stage or label cannot contain a line break or ' · '")
        new = "\n".join(line if ln.startswith(STAGE) else ln for ln in text.splitlines())
        self.call("chat.update", channel=self.channel, ts=card_id, text=escape(new), parse="none",
                  link_names=False)

    # -- thread writes -------------------------------------------------------------------------------
    def _once(self, key: str, channel: str, texts: list[str], thread_ts: str | None) -> str:
        rec = self._recorded(key) or {"parts": 0, "ts": None}
        if rec.get("checked"):
            return rec["ts"]
        for i in range(int(rec.get("parts", 0)), len(texts)):
            ts = self._post(channel, texts[i], thread_ts=thread_ts)
            rec = {"parts": i + 1, "ts": ts}
            self._record(key, rec)
        if not rec.get("ts"):
            raise SlackError("chat.postMessage", "nothing to post")
        self._readback(channel, rec["ts"], thread_ts)
        self._record(key, {**rec, "checked": True})
        return rec["ts"]

    def post(self, card_id, text, idempotency_key):
        return self._once("post:" + idempotency_key, self.channel, chunks(escape(text)), card_id)

    def open_poll(self, card_id, question, idempotency_key):
        text = (f":ballot_box_with_ballot: *Vote:* {escape(question[:1500])}\n"
                f"React :{YES}: to approve or :{NO}: to decline. Only linked teammates count; any :{NO}: declines.\n"
                f"`{vote_marker(idempotency_key)}`")
        ts = self._once("poll:" + idempotency_key, self.channel, [text], card_id)
        polls = self._recorded("polls") or {}
        if polls.get(ts) != {"card": card_id, "marker": vote_marker(idempotency_key)}:
            polls[ts] = {"card": card_id, "marker": vote_marker(idempotency_key)}
            self._record("polls", polls)
        return ts

    def read_poll(self, card_id, poll_id):
        mine = (self._recorded("polls") or {}).get(str(poll_id))
        if not mine or mine.get("card") != card_id:
            raise SlackError("read_poll", "this vote message was not opened by this board for this case")
        msg = self._message(self.channel, str(poll_id))
        if not self._ours(msg) or mine["marker"] not in (msg.get("text") or ""):
            raise SlackError("read_poll", "the vote message is not ours or lost its marker")
        if msg.get("thread_ts") not in (None, card_id):
            raise SlackError("read_poll", "the vote message is in another thread")
        reactions = msg.get("reactions") or []
        if not isinstance(reactions, list):
            raise SlackError("read_poll", "reactions are not a list")
        votes: dict[str, str] = {}
        for r in reactions:
            if not isinstance(r, dict) or not isinstance(r.get("users"), list):
                raise SlackError("read_poll", "a reaction without a users list")
            name = str(r.get("name") or "").split("::")[0]
            if name not in (YES, NO):
                continue                              # other emoji are chatter, never a vote
            if r.get("count") != len(r["users"]):
                raise SlackError("read_poll", "Slack returned a partial reaction list")
            for uid in r["users"]:
                uid = str(uid)
                if self._is_bot(uid):
                    continue                          # a bot's reaction is never a decision
                votes[uid] = "no" if name == NO or votes.get(uid) == "no" else "yes"
        return PollResult(votes=votes)

    def close_poll(self, card_id, poll_id):
        mine = (self._recorded("polls") or {}).get(str(poll_id))
        if not mine or mine.get("card") != card_id:
            raise SlackError("close_poll", "not our vote message")
        msg = self._message(self.channel, str(poll_id))
        text = unescape(msg.get("text") or "")
        if "Voting is closed" in text:
            return
        self.call("chat.update", channel=self.channel, ts=str(poll_id),
                  text=escape(text + "\nVoting is closed: this was decided."), parse="none", link_names=False)

    def alert(self, text, idempotency_key):
        self._once("alert:" + idempotency_key, self.alerts, [escape(text)[:LIMIT]], None)

    def notify(self, text, idempotency_key, mention=None):
        # Escaped text cannot ping: only the one <@U..> written here is a live mention.
        uid = str(mention) if mention and USER.fullmatch(str(mention)) else None
        body = (f"<@{uid}> " if uid else "") + escape(text)
        self._once("notice:" + idempotency_key, self.alerts, [body[:LIMIT]], None)

    # -- thread reads --------------------------------------------------------------------------------
    def messages(self, card_id):
        return [unescape(m.get("text") or "") for m in self._replies(card_id) if m["ts"] != card_id]

    def last_activity(self, card_id):
        """Time of the newest message in the thread. A stage edit is not a message, so it never counts."""
        rows = self._replies(card_id)
        if not rows:
            raise SlackError("last_activity", "the case thread has no readable message")
        return datetime.fromtimestamp(float(rows[-1]["ts"]), tz=timezone.utc).isoformat()

    def message_authors(self, card_id):
        out = []
        for m in self._replies(card_id):
            uid = str(m.get("user") or "")
            if m.get("bot_id") or m.get("subtype") == "bot_message" or not USER.fullmatch(uid):
                continue                              # a bot is never an owner
            out.append((uid, datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).isoformat()))
        return out
