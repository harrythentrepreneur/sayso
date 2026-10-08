"""Intake: new customer mail becomes a card, or reopens the customer's card.

Rules carried from the reference loop:
- The card opens with the customer's own email, verbatim, before any summary.
- Mail from our own addresses is never a customer.
- A new customer message voids every live vote on the card: the draft it
  approved may no longer answer what they said.
- The cursor moves only after every message in the batch is recorded.
- Plugins may claim a message first (for example a product-failure signature).
"""
from __future__ import annotations

from sayso import cases, stages, votes
from sayso.store import load_json, locked, save_json


def _mark_seen(path, message_id: str, ctx) -> None:
    """Recorded only AFTER the message's effect is on the card and in the case file."""
    with locked(path):
        seen = load_json(path)
        seen[message_id] = ctx.clock.now().isoformat()
        save_json(path, seen)


def run(ctx) -> dict:
    ctx.require_running()

    with locked(ctx.paths.cursors):
        cursor = load_json(ctx.paths.cursors).get("helpdesk")
    messages, new_cursor = ctx.helpdesk.fetch_inbound(cursor)
    opened = reopened = appended = skipped = 0
    limit = ctx.config.policy.max_new_cases_per_run
    seen_path = ctx.paths.root / "seen-messages.json"
    for msg in messages:
        with locked(seen_path):
            if msg.message_id in load_json(seen_path):
                continue  # a replayed batch (lost cursor, restart) must not act twice
        sender = cases.normalise(msg.sender)
        if sender in ctx.config.own_addresses:
            skipped += 1
            _mark_seen(seen_path, msg.message_id, ctx)
            continue
        kind, labels = cases.CORRESPONDENCE, []
        for plugin in ctx.plugins:
            claim = plugin.classify(ctx, msg) if hasattr(plugin, "classify") else None
            if claim:
                kind, labels = claim.get("kind", kind), list(claim.get("labels", []))
                break
        if opened >= limit:
            ctx.alert("intake-limit", f"intake stopped at {limit} new cards in one run; the rest wait")
            new_cursor = None  # do not move past unrecorded mail
            break
        key, rec, created = cases.ensure_case(ctx, customer=sender, kind=kind, title=msg.subject or "(no subject)",
                                              first_message=cases.email_card(msg), ticket=msg.ticket,
                                              labels=labels)
        if created:
            opened += 1
            cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
            _mark_seen(seen_path, msg.message_id, ctx)
            continue
        ctx.board.post(rec["card_id"], cases.email_card(msg), idempotency_key=f"mail:{msg.message_id}")
        appended += 1
        voided = votes.void_case(ctx, key, "the customer wrote again", kinds=frozenset({"reply", "close"}))
        before = ctx.board.get_tags(rec["card_id"])
        after = stages.reopen(before)
        if after != before:
            cases.set_tags_verified(ctx, rec["card_id"], after)
            reopened += 1
        cases.update(ctx, key, needs_draft=True, last_inbound=msg.received_at)
        _mark_seen(seen_path, msg.message_id, ctx)
        ctx.journal.append("mail_appended", case_key=key, voided=voided)
    if new_cursor is not None:
        with locked(ctx.paths.cursors):
            cur = load_json(ctx.paths.cursors)
            cur["helpdesk"] = new_cursor
            save_json(ctx.paths.cursors, cur)
    result = {"opened": opened, "appended": appended, "reopened": reopened, "skipped_own": skipped}
    ctx.beat("intake", str(result))
    return result
