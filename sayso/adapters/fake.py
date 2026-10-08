"""In-memory fakes for every adapter, persisted to one JSON file.

The fakes are the test bed and the demo. They behave like careful real
systems (idempotency keys are honoured, read-back returns only what was
written) and they accept injected FAULTS so tests can prove the loop fails
closed. Faults are named strings in ``world["faults"]``:

  send_raises_after_write   helpdesk accepts the email, then the call errors
  send_not_delivered        helpdesk queues the email but it never goes out
  mailbox_missing           the Sent folder has no copy
  refund_raises_after_write payments refunds, then the call errors
  refund_wrong_amount       payments refunds a different amount
  tags_ignored              board returns OK on a tag write but keeps old tags
  merge_ignored             code host returns OK on merge but does not merge
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sayso.adapters.base import (InboundMessage, MoneyReceipt, PollResult, PrFacts, PullRequest,
                                         SentRecord)
from sayso.store import load_json, save_json


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FaultInjected(RuntimeError):
    pass


class World:
    """Shared fake state for all fakes of one product."""

    def __init__(self, path: str | Path, clock):
        self.path = Path(path)
        self.clock = clock
        self.data: dict[str, Any] = load_json(self.path, default={})
        for key, default in {"faults": [], "inbox": [], "outbox": [], "cards": {}, "alerts": [],
                             "keys": {}, "refunds": [], "subs": {}, "prs": {}, "runs": {},
                             "mailbox": {}, "seq": 0, "polls": {}, "qa_runs": {}}.items():
            self.data.setdefault(key, default)

    def save(self) -> None:
        save_json(self.path, self.data)

    def fault(self, name: str) -> bool:
        return name in self.data["faults"]

    def next_id(self, prefix: str) -> str:
        self.data["seq"] += 1
        return f"{prefix}{self.data['seq']}"

    def once(self, key: str, make) -> Any:
        """Idempotency: the same key returns the first result."""
        if key in self.data["keys"]:
            return self.data["keys"][key]
        value = make()
        self.data["keys"][key] = value
        return value

    # -- test helpers -------------------------------------------------------
    def customer_writes(self, *, sender: str, subject: str, body: str, ticket: str | None = None,
                        to: str = "support@example.com") -> InboundMessage:
        ticket = ticket or self.next_id("T")
        msg = {"message_id": self.next_id("m"), "ticket": ticket, "sender": sender, "recipients": [to],
               "subject": subject, "body": body, "received_at": self.clock.now().isoformat()}
        self.data["inbox"].append(msg)
        self.save()
        return InboundMessage(**{**msg, "recipients": tuple(msg["recipients"])})

    def operator_votes(self, poll_id: str, user_id: str, answer: str) -> None:
        """Someone taps Yes or No on one exact poll."""
        poll = self.data["polls"].get(poll_id)
        if poll is None or poll["closed"]:
            raise LookupError(f"no open poll {poll_id}")
        poll["votes"][user_id] = answer
        self.save()


class FakeBoard:
    def __init__(self, world: World):
        self.w = world

    def ensure_card(self, idempotency_key, title, first_message, tags):
        def make():
            cid = self.w.next_id("card")
            self.w.data["cards"][cid] = {"title": title, "tags": list(tags), "messages": [first_message]}
            return cid
        cid = self.w.once("card:" + idempotency_key, make)
        self.w.save()
        return cid

    def get_tags(self, card_id):
        return list(self.w.data["cards"][card_id]["tags"])

    def set_tags(self, card_id, tags):
        if not self.w.fault("tags_ignored"):
            self.w.data["cards"][card_id]["tags"] = list(tags)
        self.w.save()

    def post(self, card_id, text, idempotency_key):
        def make():
            self.w.data["cards"][card_id]["messages"].append(text)
            return self.w.next_id("msg")
        mid = self.w.once("post:" + idempotency_key, make)
        self.w.save()
        return mid

    def messages(self, card_id):
        return list(self.w.data["cards"][card_id]["messages"])

    def open_poll(self, card_id, question, idempotency_key):
        def make():
            pid = self.w.next_id("poll")
            self.w.data["polls"][pid] = {"card_id": card_id, "question": question, "votes": {}, "closed": False}
            self.w.data["cards"][card_id]["messages"].append("POLL: " + question)
            return pid
        pid = self.w.once("poll:" + idempotency_key, make)
        self.w.save()
        return pid

    def read_poll(self, card_id, poll_id):
        poll = self.w.data["polls"][poll_id]
        if poll["card_id"] != card_id:
            raise LookupError("poll belongs to another card")
        return PollResult(votes=dict(poll["votes"]), closed=poll["closed"])

    def close_poll(self, card_id, poll_id):
        poll = self.w.data["polls"][poll_id]
        if poll["card_id"] != card_id:
            raise LookupError("poll belongs to another card")
        poll["closed"] = True
        self.w.save()

    def alert(self, text, idempotency_key):
        self.w.once("alert:" + idempotency_key, lambda: self.w.data["alerts"].append(text) or True)
        self.w.save()


class FakeHelpdesk:
    def __init__(self, world: World, support_address: str):
        self.w = world
        self.support = support_address

    def fetch_inbound(self, cursor):
        start = int(cursor or 0)
        rows = self.w.data["inbox"][start:]
        return ([InboundMessage(**{**r, "recipients": tuple(r["recipients"])}) for r in rows],
                str(len(self.w.data["inbox"])))

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        def make():
            mid = f"<{self.w.next_id('out')}@fake>"
            status = "Not Sent" if self.w.fault("send_not_delivered") else "Sent"
            self.w.data["outbox"].append({"ticket": ticket, "recipient": recipient, "sender": self.support,
                                          "subject": subject, "body_sha": sha(body), "message_id": mid,
                                          "status": status, "at": self.w.clock.now().isoformat()})
            if status == "Sent" and not self.w.fault("mailbox_missing"):
                self.w.data["mailbox"][mid] = self.w.data["mailbox"].get(mid, 0) + 1
            return mid
        mid = self.w.once("send:" + idempotency_key, make)
        self.w.save()
        if self.w.fault("send_raises_after_write"):
            raise FaultInjected("connection reset after write")
        return mid

    def sent_readback(self, ticket, body_sha):
        return [SentRecord(**{k: v for k, v in r.items() if k != "at"}) for r in self.w.data["outbox"]
                if r["ticket"] == ticket and r["body_sha"] == body_sha]

    def last_sent_at(self, ticket):
        times = [r["at"] for r in self.w.data["outbox"] if r["ticket"] == ticket and r["status"] == "Sent"]
        return max(times) if times else None

    def last_inbound_at(self, ticket, customer):
        who = customer.strip().lower()
        times = [r["received_at"] for r in self.w.data["inbox"]
                 if r["ticket"] == ticket or r["sender"].strip().lower() == who]
        return max(times) if times else None


class FakeMailbox:
    def __init__(self, world: World):
        self.w = world

    def sent_copies(self, message_id):
        return int(self.w.data["mailbox"].get(message_id, 0))


class FakePayments:
    def __init__(self, world: World):
        self.w = world

    def refund(self, charge, amount, currency, idempotency_key):
        def make():
            rid = self.w.next_id("re_")
            paid = amount + 1 if self.w.fault("refund_wrong_amount") else amount
            self.w.data["refunds"].append({"id": rid, "charge": charge, "amount": paid,
                                           "currency": currency, "status": "succeeded"})
            return rid
        rid = self.w.once("refund:" + idempotency_key, make)
        self.w.save()
        if self.w.fault("refund_raises_after_write"):
            raise FaultInjected("timeout after refund")
        return rid

    def cancel_subscription(self, subscription, idempotency_key):
        self.w.data["subs"][subscription] = "canceled"
        self.w.save()
        return subscription

    def read_refunds(self, charge):
        return [MoneyReceipt("refund", r["id"], r["amount"], r["currency"], r["status"])
                for r in self.w.data["refunds"] if r["charge"] == charge]

    def read_subscription(self, subscription):
        return MoneyReceipt("cancel_subscription", subscription, None, None,
                            self.w.data["subs"].get(subscription, "active"))


class FakeCodeHost:
    def __init__(self, world: World):
        self.w = world

    def add_pr(self, number: int, head: str) -> None:
        self.w.data["prs"][str(number)] = {"state": "open", "head": head}
        self.w.save()

    def pull_request(self, number):
        pr = self.w.data["prs"][str(number)]
        return PullRequest(number, pr["state"], pr["head"], f"https://example.invalid/pr/{number}")

    def pr_facts(self, number, head):
        pr = self.w.data["prs"][str(number)]
        if pr["head"] != head:
            raise RuntimeError("head moved")
        added = pr.get("added", [])
        return PrFacts(ci=pr.get("ci", "green"), files=tuple(pr.get("files", ["src/app.py", "tests/test_app.py"])),
                       added=None if added is None else tuple(added))

    def merge(self, number, expected_head, idempotency_key):
        pr = self.w.data["prs"][str(number)]
        if pr["head"] != expected_head:
            raise RuntimeError("head moved; merge refused")
        if not self.w.fault("merge_ignored"):
            pr["state"] = "merged"
        self.w.save()


class FakeDrafter:
    def __init__(self, world: World):
        self.w = world

    def draft(self, case, thread):
        override = self.w.data.get("next_draft")
        if override:
            self.w.data["next_draft"] = None
            self.w.save()
            return override
        return (f"Hi,\n\nThanks for writing to us about \"{case['title']}\". "
                "We have looked into it and here is what we found.\n\nBest wishes,\nSupport")


class FakeDevRunner:
    def __init__(self, world: World):
        self.w = world

    def start(self, case_key, brief, idempotency_key):
        def make():
            rid = self.w.next_id("run")
            self.w.data["runs"][rid] = {"case_key": case_key, "brief": brief, "result": None}
            return rid
        rid = self.w.once("run:" + idempotency_key, make)
        self.w.save()
        return rid

    def finish(self, run_id: str, pr: int, head: str) -> None:
        self.w.data["runs"][run_id]["result"] = {"pr": pr, "summary": "Fix written and tested."}
        self.w.data["prs"][str(pr)] = {"state": "open", "head": head}
        self.w.save()

    def result(self, run_id):
        return self.w.data["runs"][run_id]["result"]


class FakeQaRunner:
    """QA on fakes. Tests set ``world["red_proof"]`` and finish runs with the QA text."""

    def __init__(self, world: World):
        self.w = world

    def fails_on_old_code(self, pr, head):
        self.w.data.setdefault("red_proof_calls", []).append([pr, head])
        self.w.save()
        got = self.w.data.get("red_proof", {"state": "red-proved"})
        if got == "raise":
            raise FaultInjected("red proof crashed")
        return dict(got)

    def start(self, case_key, brief, idempotency_key):
        def make():
            rid = self.w.next_id("qa")
            self.w.data["qa_runs"][rid] = {"case_key": case_key, "brief": brief, "result": None}
            return rid
        rid = self.w.once("qa:" + idempotency_key, make)
        self.w.save()
        return rid

    def finish(self, run_id: str, text: str, usage: dict | None = None) -> None:
        self.w.data["qa_runs"][run_id]["result"] = {"text": text, **({"usage": usage} if usage else {})}
        self.w.save()

    def result(self, run_id):
        return self.w.data["qa_runs"][run_id]["result"]


def dump(world: World) -> str:
    return json.dumps(world.data, indent=1, sort_keys=True)
