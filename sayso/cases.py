"""Case records: one card per customer per KIND of problem.

A customer's second email about the same kind of thing lands in their existing
card, not a new one. A failure signature from a plugin is its own kind, so a
product failure never folds into an unrelated billing conversation.

The board is the human surface; this file is the loop's memory of which card
belongs to which (customer, kind) and which helpdesk ticket.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sayso import stages
from sayso.store import load_json, locked, save_json

CORRESPONDENCE = "correspondence"
UNGROUPABLE = "unmatched"


class CaseError(RuntimeError):
    pass


def normalise(address: str) -> str:
    return str(address or "").strip().lower()


def case_key_for(customer: str, kind: str, n: int) -> str:
    digest = hashlib.sha256(f"{normalise(customer)}|{kind}".encode()).hexdigest()[:12]
    return f"{kind}-{digest}-{n}"


def find(cases: dict[str, Any], customer: str, kind: str) -> tuple[str, dict] | None:
    """The live card for this (customer, kind), newest first. Unmatched never groups."""
    if kind == UNGROUPABLE:
        return None
    who = normalise(customer)
    hits = [(k, r) for k, r in cases.items()
            if r.get("customer") == who and r.get("kind") == kind and not r.get("folded_into")]
    if not hits:
        return None
    return max(hits, key=lambda kr: kr[1].get("created_at", ""))


def ensure_case(ctx, *, customer: str, kind: str, title: str, first_message: str,
                ticket: str | None, labels: list[str] | None = None) -> tuple[str, dict, bool]:
    """Find or create the card. Returns (case_key, record, created).

    Lookup and record happen under one lock, so a caller cannot forget a step
    and two intakes cannot race each other into two cards.
    """
    customer = normalise(customer)
    if not customer:
        raise CaseError("a case needs a customer address")
    with locked(ctx.paths.cases):
        cases = load_json(ctx.paths.cases)
        hit = find(cases, customer, kind)
        if hit:
            key, rec = hit
            if ticket and not rec.get("ticket"):
                rec["ticket"] = ticket
                save_json(ctx.paths.cases, cases)
            return key, rec, False
        n = 1 + sum(1 for r in cases.values() if r.get("customer") == customer and r.get("kind") == kind)
        key = case_key_for(customer, kind, n)
        tags = [stages.NEW] + list(labels or [])
        card_id = ctx.board.ensure_card(key, title, first_message, tags)
        rec = {"card_id": card_id, "customer": customer, "kind": kind, "ticket": ticket, "title": title,
               "created_at": ctx.clock.now().isoformat()}
        cases[key] = rec
        save_json(ctx.paths.cases, cases)
    ctx.journal.append("case_opened", case_key=key, card_id=card_id, kind=kind)
    return key, rec, True


def load(ctx) -> dict[str, Any]:
    return load_json(ctx.paths.cases)


def update(ctx, case_key: str, **fields: Any) -> dict:
    with locked(ctx.paths.cases):
        cases = load_json(ctx.paths.cases)
        if case_key not in cases:
            raise CaseError(f"unknown case {case_key}")
        cases[case_key].update(fields)
        save_json(ctx.paths.cases, cases)
        return cases[case_key]


def set_tags_verified(ctx, card_id: str, tags: list[str]) -> list[str]:
    """Write tags, then READ THEM BACK. A 200 response is not proof."""
    stages.validate(tags)
    ctx.board.set_tags(card_id, tags)
    live = ctx.board.get_tags(card_id)
    if sorted(live) != sorted(tags):
        raise CaseError(f"tag read-back mismatch on {card_id}: wanted {tags}, board has {live}")
    return live


def move(ctx, card_id: str, to: str, *, waiting: bool = False) -> list[str]:
    return set_tags_verified(ctx, card_id, stages.move(ctx.board.get_tags(card_id), to, waiting=waiting))


def subject(rec: dict) -> str:
    """A poll subject a person can read cold: who, then the problem. A customer id that is not an email
    address (``teacher:<uuid>``) means nothing to a human, so only the title is shown for those."""
    customer = str(rec.get("customer") or "")
    who = customer.split("@")[0] if "@" in customer else ""
    return f"{who} - {rec['title']}" if who else str(rec["title"])


def email_card(msg) -> str:
    """The customer's own words first, verbatim. Never a summary."""
    if getattr(msg, "card", ""):
        return msg.card
    return (f"**Customer wrote**\nFrom: {msg.sender}\nTo: {', '.join(msg.recipients)}\n"
            f"Date: {msg.received_at}\nSubject: {msg.subject}\n\n{msg.body}")
