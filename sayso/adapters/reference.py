"""Reference adapters for real systems. OFF unless a product sets mode = "live".

Each class satisfies one Protocol in ``adapters/base.py``. They are written to
be READ and adapted: every vendor has quirks, and the comments name the ones
that bit the reference deployment. They are exercised in tests against a
recording transport only - no network, no account. Before trusting one with a
real product, run its live read-only checks from docs/ONBOARDING.md.

Secrets come from environment variables named in the settings file
(``token_env = "ACME_DISCORD_TOKEN"``); nothing secret is ever in the file.
"""
from __future__ import annotations

import email
import imaplib
import subprocess
from typing import Any

from sayso.adapters.base import InboundMessage, MoneyReceipt, PollResult, PrFacts, PullRequest, SentRecord
from sayso.adapters.http import Client, HttpError
from sayso.config import Config, ConfigError, Section


# --------------------------------------------------------------------------- Discord
class DiscordForumBoard:
    """One forum post per case; forum tags are stages; native polls are votes.

    Quirks learned the hard way:
    - ``applied_tags`` is a whole-list write. Always send the full list.
    - Editing a forum's ``available_tags`` without each tag's ``id`` DELETES all
      tags and orphans every post. This adapter never edits available_tags;
      create the tags by hand (docs/ONBOARDING.md).
    - Renaming a post leaves an undeletable system message. Titles are final.
    - Messages over 2000 chars are split on word boundaries.
    - Discord strips trailing whitespace; read-back compares stripped text.
    - A ROLE mention cannot wake an agent; only user mentions do.
    """

    API = "https://discord.com/api/v10"

    def __init__(self, section: Section, transport=None):
        self.forum = str(section.options.get("forum_id") or "")
        self.alerts = str(section.options.get("alerts_channel_id") or "")
        if not self.forum or not self.alerts:
            raise ConfigError("[board] discord needs forum_id and alerts_channel_id")
        self.http = Client(self.API, {"Authorization": "Bot " + section.secret("token")}, transport)
        self._tags: dict[str, str] | None = None
        self._keys: dict[str, str] = {}

    def _tag_ids(self) -> dict[str, str]:
        if self._tags is None:
            forum = self.http.request("GET", f"/channels/{self.forum}")
            self._tags = {t["name"]: t["id"] for t in forum.get("available_tags", [])}
        return self._tags

    def _ids(self, names: list[str]) -> list[str]:
        ids = self._tag_ids()
        missing = [n for n in names if n not in ids]
        if missing:
            raise ConfigError(f"forum is missing tags {missing}; add them by hand")
        return [ids[n] for n in names]

    @staticmethod
    def chunks(text: str, limit: int = 1900) -> list[str]:
        out, rest = [], text.strip()
        while len(rest) > limit:
            cut = rest.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        return out + ([rest] if rest else [])

    def ensure_card(self, idempotency_key, title, first_message, tags):
        # Durable idempotency lives in the caller's case file (cases.ensure_case
        # records the card before returning). A marker in the first message lets
        # an operator find an orphan after a crash.
        parts = self.chunks(first_message + f"\n\n`case:{idempotency_key}`")
        post = self.http.request("POST", f"/channels/{self.forum}/threads",
                                 json_body={"name": title[:95], "applied_tags": self._ids(tags),
                                            "message": {"content": parts[0]}})
        for part in parts[1:]:
            self.http.request("POST", f"/channels/{post['id']}/messages", json_body={"content": part})
        return post["id"]

    def get_tags(self, card_id):
        thread = self.http.request("GET", f"/channels/{card_id}")
        names = {v: k for k, v in self._tag_ids().items()}
        return [names.get(t, f"unknown:{t}") for t in thread.get("applied_tags", [])]

    def set_tags(self, card_id, tags):
        self.http.request("PATCH", f"/channels/{card_id}", json_body={"applied_tags": self._ids(tags)})

    def post(self, card_id, text, idempotency_key):
        last = None
        for i, part in enumerate(self.chunks(text)):
            nonce = f"{abs(hash((idempotency_key, i))) % 10**18}"
            last = self.http.request("POST", f"/channels/{card_id}/messages",
                                     json_body={"content": part, "nonce": nonce, "enforce_nonce": True})
        return last["id"] if last else ""

    def messages(self, card_id):
        rows = self.http.request("GET", f"/channels/{card_id}/messages", query={"limit": 100})
        return [m.get("content", "") for m in reversed(rows)]

    def open_poll(self, card_id, question, idempotency_key):
        msg = self.http.request("POST", f"/channels/{card_id}/messages", json_body={
            "nonce": f"{abs(hash(idempotency_key)) % 10**18}", "enforce_nonce": True,
            "poll": {"question": {"text": question[:300]}, "duration": 168, "allow_multiselect": False,
                     "answers": [{"poll_media": {"text": "Yes"}}, {"poll_media": {"text": "No"}}]}})
        return msg["id"]

    def read_poll(self, card_id, poll_id):
        votes: dict[str, str] = {}
        for answer_id, word in ((1, "yes"), (2, "no")):
            users = self.http.request("GET", f"/channels/{card_id}/polls/{poll_id}/answers/{answer_id}",
                                      query={"limit": 100})
            for user in users.get("users", []):
                if user.get("bot"):
                    continue  # a bot's vote is never a decision
                votes[str(user["id"])] = "no" if word == "no" or votes.get(str(user["id"])) == "no" else "yes"
        return PollResult(votes=votes)

    def close_poll(self, card_id, poll_id):
        """End a native Discord poll early, so the question shows as settled."""
        self.http.request("POST", f"/channels/{card_id}/polls/{poll_id}/expire")

    def alert(self, text, idempotency_key):
        self.http.request("POST", f"/channels/{self.alerts}/messages",
                          json_body={"content": text[:1900], "nonce": f"{abs(hash(idempotency_key)) % 10**18}",
                                     "enforce_nonce": True})


# --------------------------------------------------------------------------- Frappe Helpdesk
class FrappeHelpdesk:
    """Frappe Helpdesk (HD Ticket + Communication + Email Queue).

    - The customer history lives here. The loop never keeps a competing copy.
    - A ticket can hold mail from several people; always match each
      Communication to the exact sender before trusting it.
    - A reply is proven by ONE Sent Communication and ONE Email Queue row with
      status Sent to exactly the one recipient.
    """

    def __init__(self, section: Section, support_address: str, transport=None):
        url = section.options.get("url")
        if not url:
            raise ConfigError("[helpdesk] frappe needs url")
        token = f"token {section.secret('api_key')}:{section.secret('api_secret')}"
        self.http = Client(str(url), {"Authorization": token, "Accept": "application/json"}, transport)
        self.support = support_address

    def _list(self, doctype, filters, fields, order="creation asc", limit=200):
        import json as _json
        return self.http.request("GET", f"/api/resource/{doctype}", query={
            "filters": _json.dumps(filters), "fields": _json.dumps(fields), "order_by": order,
            "limit_page_length": limit})["data"]

    def fetch_inbound(self, cursor):
        rows = self._list("Communication", [["sent_or_received", "=", "Received"],
                                            ["reference_doctype", "=", "HD Ticket"],
                                            ["creation", ">", cursor or "1970-01-01"]],
                          ["name", "reference_name", "sender", "recipients", "subject", "content", "creation"])
        msgs = [InboundMessage(message_id=r["name"], ticket=str(r["reference_name"]), sender=r["sender"],
                               recipients=tuple(x.strip() for x in (r.get("recipients") or "").split(",") if x.strip()),
                               subject=r.get("subject") or "", body=r.get("content") or "",
                               received_at=_frappe_time(r["creation"])) for r in rows]
        return msgs, (rows[-1]["creation"] if rows else cursor)

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        out = self.http.request("POST", "/api/method/helpdesk.helpdesk.doctype.hd_ticket.api.reply_via_agent",
                                json_body={"ticket_id": ticket, "message": body, "to": recipient})
        return str((out or {}).get("message", ""))

    def sent_readback(self, ticket, body_sha):
        import hashlib
        comms = self._list("Communication", [["reference_name", "=", ticket], ["sent_or_received", "=", "Sent"]],
                           ["name", "recipients", "sender", "subject", "content", "message_id"])
        out = []
        for c in comms:
            if hashlib.sha256((c.get("content") or "").encode()).hexdigest() != body_sha:
                continue
            queue = self._list("Email Queue", [["communication", "=", c["name"]]], ["name", "status"])
            status = "Sent" if len(queue) == 1 and queue[0]["status"] == "Sent" else "Not Sent"
            out.append(SentRecord(ticket=ticket, recipient=c.get("recipients") or "", sender=c.get("sender") or "",
                                  subject=c.get("subject") or "", body_sha=body_sha,
                                  message_id=c.get("message_id") or "", status=status))
        return out

    def _last(self, filters):
        rows = self._list("Communication", filters, ["creation"], order="creation desc", limit=1)
        return _frappe_time(rows[0]["creation"]) if rows else None

    def last_sent_at(self, ticket):
        return self._last([["reference_name", "=", ticket], ["sent_or_received", "=", "Sent"]])

    def last_inbound_at(self, ticket, customer):
        on_ticket = self._last([["reference_name", "=", ticket], ["sent_or_received", "=", "Received"]])
        from_them = self._last([["sender", "=", customer], ["sent_or_received", "=", "Received"]])
        return max([t for t in (on_ticket, from_them) if t], default=None)


def _frappe_time(value: str) -> str:
    """Frappe returns naive site-local time. The site timezone MUST be UTC (docs/ADAPTERS.md)."""
    return value.replace(" ", "T") + ("" if "+" in value else "+00:00")


# --------------------------------------------------------------------------- IMAP Sent folder
class ImapMailbox:
    """Counts copies of a Message-ID in the mailbox's own Sent folder."""

    def __init__(self, section: Section, connect=None):
        self.host = section.options.get("host")
        self.user = section.options.get("user")
        self.folder = section.options.get("sent_folder", '"[Gmail]/Sent Mail"')
        if not self.host or not self.user:
            raise ConfigError("[mailbox] imap needs host and user")
        self.password = section.secret("password")
        self.connect = connect or (lambda: imaplib.IMAP4_SSL(str(self.host)))

    READ_ATTEMPTS = 3

    def sent_copies(self, message_id):
        """Read only, so a dropped connection is retried. Only a read that fails
        every attempt is an error (and the caller treats that as unproven)."""
        mid = message_id.strip()
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        last = None
        for _ in range(self.READ_ATTEMPTS):
            try:
                return self._count(mid)
            except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
                last = exc
        raise RuntimeError(f"IMAP read failed {self.READ_ATTEMPTS} times: {type(last).__name__}") from last

    def _count(self, mid):
        conn = self.connect()
        try:
            conn.login(str(self.user), self.password)
            conn.select(self.folder, readonly=True)
            status, data = conn.search(None, "HEADER", "Message-ID", mid)
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            return len((data[0] or b"").split())
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- Stripe
class StripePayments:
    """Stripe refunds and cancellations. Uses Stripe's own Idempotency-Key header."""

    def __init__(self, section: Section, transport=None):
        self.http = Client("https://api.stripe.com/v1", {"Authorization": "Bearer " + section.secret("api_key")},
                           transport)

    def refund(self, charge, amount, currency, idempotency_key):
        field = "payment_intent" if charge.startswith("pi_") else "charge"
        out = self.http.request("POST", "/refunds", form={field: charge, "amount": amount},
                                extra_headers={"Idempotency-Key": idempotency_key})
        if out.get("currency") != currency:
            raise RuntimeError("refund currency differs from the approved currency")
        return out["id"]

    def cancel_subscription(self, subscription, idempotency_key):
        out = self.http.request("DELETE", f"/subscriptions/{subscription}",
                                extra_headers={"Idempotency-Key": idempotency_key})
        return out["id"]

    def read_refunds(self, charge):
        field = "payment_intent" if charge.startswith("pi_") else "charge"
        rows = self.http.request("GET", "/refunds", query={field: charge, "limit": 100})["data"]
        return [MoneyReceipt("refund", r["id"], r["amount"], r["currency"], r["status"]) for r in rows]

    def read_subscription(self, subscription):
        s = self.http.request("GET", f"/subscriptions/{subscription}")
        return MoneyReceipt("cancel_subscription", s["id"], None, None, s["status"])


# --------------------------------------------------------------------------- GitHub
class GitHubCodeHost:
    """Pull requests on one repository. Merges only at the approved head SHA."""

    def __init__(self, section: Section, transport=None):
        self.repo = section.options.get("repo")
        if not self.repo or "/" not in str(self.repo):
            raise ConfigError("[codehost] github needs repo = \"owner/name\"")
        self.http = Client("https://api.github.com", {"Authorization": "Bearer " + section.secret("token"),
                                                      "Accept": "application/vnd.github+json"}, transport)

    def pull_request(self, number):
        pr = self.http.request("GET", f"/repos/{self.repo}/pulls/{number}")
        state = "merged" if pr.get("merged") else pr["state"]
        return PullRequest(number, state, pr["head"]["sha"], pr["html_url"])

    def pr_facts(self, number, head):
        """CI state, changed files and added lines for one exact head. Anything that
        cannot be read is returned as unreadable, which the QA gate fails closed on."""
        files, added = [], []
        try:
            page = 1
            while True:
                rows = self.http.request("GET", f"/repos/{self.repo}/pulls/{number}/files",
                                         query={"per_page": 100, "page": page})
                for f in rows:
                    files.append(str(f.get("filename") or ""))
                    if "patch" not in f and f.get("status") != "removed":
                        added = None          # a patch GitHub would not show: diff unreadable
                    elif added is not None:
                        added += [ln[1:] for ln in str(f.get("patch") or "").splitlines()
                                  if ln.startswith("+") and not ln.startswith("+++")]
                if len(rows) < 100:
                    break
                page += 1
        except HttpError:
            files, added = [], None
        return PrFacts(ci=self._ci(head), files=tuple(files), added=None if added is None else tuple(added))

    def _ci(self, head):
        try:
            runs = self.http.request("GET", f"/repos/{self.repo}/commits/{head}/check-runs",
                                     query={"per_page": 100})["check_runs"]
            status = self.http.request("GET", f"/repos/{self.repo}/commits/{head}/status")
        except (HttpError, KeyError, TypeError):
            return "red"                      # unreadable CI is never green
        if not runs and not status.get("statuses"):
            return "pending"                  # no CI reported yet on this head
        if any(r.get("status") != "completed" for r in runs) or status.get("state") == "pending" \
                and status.get("statuses"):
            return "pending"
        bad = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
        if bad or (status.get("statuses") and status.get("state") != "success"):
            return "red"
        return "green"

    def merge(self, number, expected_head, idempotency_key):
        # GitHub refuses with 409 if the head moved: the SHA pin is enforced server-side too.
        try:
            self.http.request("PUT", f"/repos/{self.repo}/pulls/{number}/merge",
                              json_body={"sha": expected_head, "merge_method": "squash"})
        except HttpError as exc:
            if exc.status == 405 and self.pull_request(number).state == "merged":
                return  # already merged by an earlier attempt
            raise


# --------------------------------------------------------------------------- Hermes
class HermesDrafter:
    """Asks a Hermes profile for a draft. The output is data, never sent unapproved."""

    def __init__(self, section: Section, run=subprocess.run):
        self.profile = section.options.get("profile")
        if not self.profile:
            raise ConfigError("[drafter] hermes needs profile")
        self.style = str(section.options.get("style") or "").strip()  # e.g. "a short, friendly text message"
        self.run = run

    def draft(self, case, thread):
        prompt = ("Write ONE plain-text customer reply for this support case. Do not claim money moved, "
                  "do not promise dates, do not include payment ids. Output only the reply.\n"
                  + (f"Style: {self.style}\n" if self.style else "") + "\n"
                  + "\n\n---\n\n".join(thread))
        out = self.run(["hermes", "-p", str(self.profile), "chat", "--oneshot", "--query", prompt],
                       capture_output=True, text=True, timeout=600, check=True)
        return out.stdout.strip()


class HermesDevRunner:
    """Starts `hermes -p <profile> chat --oneshot` as a systemd transient unit.

    The unit outlives the timer that started it. The result is read from a file
    the run writes; the run must end with a `PR: <number>` line.
    """

    def __init__(self, section: Section, state_dir, run=subprocess.run):
        self.profile = section.options.get("profile")
        if not self.profile:
            raise ConfigError("[dev_runner] hermes needs profile")
        self.dir = state_dir / "dev-runs"
        self.run = run

    def start(self, case_key, brief, idempotency_key):
        import re
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", idempotency_key)[:80]
        self.dir.mkdir(parents=True, exist_ok=True)
        brief_path, out_path = self.dir / f"{safe}.md", self.dir / f"{safe}.out"
        if out_path.exists() or brief_path.exists():
            return safe
        brief_path.write_text(brief, encoding="utf-8")
        self.run(["systemd-run", "--user", f"--unit=sayso-dev-{safe}", "--property=RuntimeMaxSec=5400",
                  "bash", "-c", f"hermes -p {self.profile} chat --oneshot --query-file {brief_path} > {out_path}"],
                 check=True)
        return safe

    def result(self, run_id):
        import re
        out = self.dir / f"{run_id}.out"
        if not out.exists():
            return None
        m = re.search(r"^PR:\s*#?(\d+)\s*$", out.read_text(encoding="utf-8"), re.M)
        return {"pr": int(m.group(1)), "summary": "See the dev run output."} if m else None


class HermesQaRunner:
    """Independent QA through a Hermes profile, started as a systemd transient unit.

    Options:
      profile         the QA profile (must not be the dev profile)
      red_proof_cmd   a command that prints one JSON line {"state": ..., "reason": ...};
                      it receives the PR number and head SHA as its last two arguments.
                      Missing = the check reports "error", so QA never starts.
    """

    def __init__(self, section: Section, state_dir, run=subprocess.run, dev_profile=None):
        self.profile = section.options.get("profile")
        if not self.profile:
            raise ConfigError("[qa_runner] hermes needs profile")
        if dev_profile and self.profile == dev_profile:
            raise ConfigError("[qa_runner] profile must differ from [dev_runner] profile: QA must be independent")
        self.red_cmd = section.options.get("red_proof_cmd")
        self.dir = state_dir / "qa-runs"
        self.run = run

    def fails_on_old_code(self, pr, head):
        import json
        import shlex
        if not self.red_cmd:
            return {"state": "error", "reason": "red_proof_cmd is not set"}
        out = self.run(shlex.split(str(self.red_cmd)) + [str(int(pr)), str(head)],
                       capture_output=True, text=True, timeout=1800)
        try:
            got = json.loads((out.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"state": "error", "reason": "red_proof_cmd printed no JSON verdict"}
        return got if isinstance(got, dict) else {"state": "error", "reason": "bad verdict"}

    def start(self, case_key, brief, idempotency_key):
        import re
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", idempotency_key)[:80]
        self.dir.mkdir(parents=True, exist_ok=True)
        brief_path, out_path = self.dir / f"{safe}.md", self.dir / f"{safe}.out"
        if out_path.exists() or brief_path.exists():
            return safe
        brief_path.write_text(brief, encoding="utf-8")
        self.run(["systemd-run", "--user", f"--unit=sayso-qa-{safe}", "--property=RuntimeMaxSec=5400",
                  "bash", "-c", f"hermes -p {self.profile} chat --oneshot --query-file {brief_path} "
                  f"> {out_path}.part 2>&1; mv {out_path}.part {out_path}"],
                 check=True)
        return safe

    def result(self, run_id):
        out = self.dir / f"{run_id}.out"
        return {"text": out.read_text(encoding="utf-8", errors="replace")} if out.exists() else None


def build(name: str, kind: str, section: Section, config: Config) -> Any:
    if kind == "baker":
        from sayso.adapters import baker
        return baker.BakerHelpdesk(section, config.support_address) if name == "helpdesk" else baker.BakerDelivery(section)
    if kind == "discord":
        return DiscordForumBoard(section)
    if kind == "frappe":
        return FrappeHelpdesk(section, config.support_address)
    if kind == "imap":
        return ImapMailbox(section)
    if kind == "stripe":
        return StripePayments(section)
    if kind == "github":
        return GitHubCodeHost(section)
    if kind == "hermes" and name == "drafter":
        return HermesDrafter(section)
    if kind == "hermes" and name == "dev_runner":
        return HermesDevRunner(section, config.state_dir)
    if kind == "hermes" and name == "qa_runner":
        return HermesQaRunner(section, config.state_dir,
                              dev_profile=config.section("dev_runner").options.get("profile"))
    raise ConfigError(f"no reference adapter {kind!r} for {name}")
