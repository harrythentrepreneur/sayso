"""Baker adapter: a texting product (iMessage / WhatsApp) as the helpdesk.

Baker has no email helpdesk. Teachers text an AI agent, and Baker's backend records the moments a person should
look (``case signals``: the agent flagged the chat, something broke, or Stripe said something) and the teacher's
texts after them. This adapter reads those through Baker's ``/ops/cases/*`` API and sends a reply a person
approved back through Baker, which delivers it on the teacher's own channel exactly once.

Mapping onto the loop:

* customer   ``teacher:<uuid>`` - a stable id, never a phone number. Cards show only the last four digits.
* ticket     ``teacher:<uuid>`` too: one teacher is one conversation.
* kind       Baker decides it (``correspondence`` or ``failure:<reason>``) and sends it with the message, so a
             broken PDF never folds into a refund conversation.
* receipt    there is no mailbox Sent folder for a text. The loop's two independent proofs become:
               1. Baker's own team-reply record: claimed before the send, ``sent``, to exactly this teacher;
               2. the PROVIDER's own delivery status for that message (Sendblue's message status or WhatsApp's
                  delivery receipt), read separately by ``BakerDelivery.sent_copies``.
             A reply that Baker refused (STOP, outside WhatsApp's 24 h window) or that failed is never proven:
             the loop marks it UNVERIFIED, alerts once and never retries it.

Settings::

    [helpdesk]
    adapter = "baker"
    url = "https://api.textbaker.com"        # Baker's API; /ops/cases/* must be reachable from the loop
    token_env = "BAKER_CASES_TOKEN"           # Baker's CASES_TOKEN

    [mailbox]
    adapter = "baker"
    url = "https://api.textbaker.com"
    token_env = "BAKER_CASES_TOKEN"

``sending = false`` on [helpdesk] is a hard switch for a shadow run: cards and drafts appear, but ``send_reply``
raises and nothing reaches a teacher, whatever the votes say.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from sayso.adapters.base import InboundMessage, SentRecord
from sayso.adapters.http import Client
from sayso.config import ConfigError, Section

PREFIX = "teacher:"
PROVEN = {"sent", "delivered", "read", "logged"}   # Provider statuses that prove the text left Baker.


class SendingOff(RuntimeError):
    """The [helpdesk] sending switch is off: nothing is sent to a teacher."""


def teacher_id(customer_or_ticket: str) -> str:
    value = str(customer_or_ticket or "")
    if not value.startswith(PREFIX) or len(value) != len(PREFIX) + 36:
        raise ValueError(f"not a Baker teacher id: {value!r}")
    return value[len(PREFIX):]


def _client(section: Section, transport=None) -> Client:
    url = section.options.get("url")
    if not url or not str(url).startswith("https://") and not str(url).startswith("http://127.0.0.1"):
        raise ConfigError(f"[{section.adapter}] baker needs url (https, or http://127.0.0.1 for a tunnel)")
    return Client(str(url), {"Authorization": "Bearer " + section.secret("token"), "Accept": "application/json"},
                  transport)


def _teacher_line(t: dict[str, Any]) -> str:
    bits = [t.get("name") or "Name not known", f"phone {t.get('phone') or '…'}", t.get("channel") or "?",
            f"plan {t.get('plan') or '?'}", f"{t.get('resources', 0)} resources"]
    if t.get("year_group"):
        bits.append(t["year_group"])
    if t.get("opted_out"):
        bits.append("texted STOP")
    if t.get("qa"):
        bits.append("QA TEST TEACHER")
    return " · ".join(bits)


def signal_card(event: dict[str, Any]) -> str:
    """A new card: what happened, who, then the chat in the teacher's own words (Baker's transcript)."""
    t = event["teacher"]
    return (f"**Teacher's chat** · {event['label']}\n{_teacher_line(t)}\n"
            f"Why it's here: {event['summary']}\n\n{event.get('transcript') or '(no chat yet)'}")


def text_card(event: dict[str, Any]) -> str:
    """A teacher's later text on an open case, verbatim, with what Baker had just said for context."""
    before = (event.get("baker_before") or "").strip()
    quoted = ("\n> Baker had said: " + before[:300].replace("\n", " ")) if before else ""
    return f"**Teacher wrote** ({event['at'][:16].replace('T', ' ')} UTC){quoted}\n\n{event['body']}"


class BakerHelpdesk:
    def __init__(self, section: Section, support_address: str, transport=None):
        self.http = _client(section, transport)
        self.support = support_address  # Replies are "from" the product, as the sender job expects.
        sending = section.options.get("sending", True)
        if not isinstance(sending, bool):
            raise ConfigError("[helpdesk] baker sending must be true or false")
        self.sending = sending

    def fetch_inbound(self, cursor):
        query = {"cursor": cursor} if cursor else {}
        out = self.http.request("GET", "/ops/cases/feed", query=query)
        if not isinstance(out, dict) or "cursor" not in out or not isinstance(out.get("events"), list):
            raise RuntimeError("Baker case feed returned an unexpected shape")
        msgs = []
        for e in out["events"]:
            who = PREFIX + str(e["teacher"]["teacher_id"])
            teacher_id(who)  # Refuse anything that is not a teacher id: a guess would open a wrong card.
            if e["type"] == "signal":
                title = f"{e['teacher'].get('name') or 'Teacher ' + e['teacher'].get('phone', '')} - {e['label']}"
                body, card = e["summary"], signal_card(e)
            elif e["type"] == "text":
                name = e["teacher"].get("name") or "Teacher " + e["teacher"].get("phone", "")
                title, body, card = f"{name} - Follow-up", e["body"], text_card(e)
            else:
                raise RuntimeError(f"unknown Baker event type {e.get('type')!r}")
            msgs.append(InboundMessage(message_id=str(e["id"]), ticket=who, sender=who, recipients=("baker",),
                                       subject=title[:95], body=body, received_at=e["at"], kind=str(e["kind"]),
                                       labels=("QA test",) if e["teacher"].get("qa") else (), card=card))
        return msgs, str(out["cursor"])

    def send_reply(self, ticket, recipient, subject, body, idempotency_key):
        if not self.sending:
            raise SendingOff("sending is off for this product ([helpdesk] sending = false); nothing was sent")
        if recipient != ticket:
            raise ValueError("a Baker reply goes to the teacher who owns the case, nobody else")
        out = self.http.request("POST", "/ops/cases/reply", json_body={
            "key": idempotency_key, "teacher_id": teacher_id(ticket), "body": body})
        if out.get("status") in ("refused", "failed"):
            raise RuntimeError(f"Baker did not send it: {out.get('note') or out.get('status')}")
        return str(out.get("key"))

    def sent_readback(self, ticket, body_sha):
        out = self.http.request("GET", f"/ops/cases/teacher/{teacher_id(ticket)}", query={"sha": body_sha})
        rows = []
        for r in out.get("replies", []):
            if r.get("body_sha") != body_sha:
                continue  # Only the exact approved text counts.
            rows.append(SentRecord(ticket=ticket, recipient=ticket if r.get("teacher_id") == teacher_id(ticket) else "",
                                   sender=self.support, subject="", body_sha=body_sha, message_id=str(r["key"]),
                                   status="Sent" if r.get("status") == "sent" else "Not Sent"))
        return rows

    def last_sent_at(self, ticket):
        return self.http.request("GET", f"/ops/cases/teacher/{teacher_id(ticket)}").get("sent")

    def last_inbound_at(self, ticket, customer):
        return self.http.request("GET", f"/ops/cases/teacher/{teacher_id(customer)}").get("inbound")


class BakerDelivery:
    """The independent receipt: the messaging provider's own status for the text Baker sent."""

    def __init__(self, section: Section, transport=None):
        self.http = _client(section, transport)

    def sent_copies(self, message_id):
        key = urllib.parse.quote(str(message_id), safe="")
        row = self.http.request("GET", f"/ops/cases/reply/{key}")
        ok = row.get("status") == "sent" and str(row.get("delivery") or "").lower() in PROVEN
        return 1 if ok else 0
