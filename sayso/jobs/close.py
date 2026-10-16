"""Close: a card quiet for N days after our verified reply gets ONE close vote.

- Quiet is measured from the helpdesk's own record of our last sent reply,
  and voided if the customer has written since.
- One close vote per case per reply; never re-asked for the same reply.
- An approved close drops Waiting on customer, leaving Done alone.

No-reply closes (opened by the draft job when the drafter says no reply is
owed) are handled here too. They are voided if the customer has written since
the message they answer, or the card left Awaiting approval. Approved, the
card goes to Done with nothing sent. Declined, the card returns to In support
for a real draft; the same message is never put to a no-reply vote twice.
"""
from __future__ import annotations

from datetime import timedelta

from sayso import cases, stages, votes
from sayso.adapters import events
from sayso.store import parse_iso


def _no_reply_close(ctx, vkey: str, v: dict) -> str | None:
    if v["status"] not in votes.LIVE and not (v["status"] == votes.DECLINED and not v.get("followed_up")):
        return None
    rec = cases.load(ctx)[v["case_key"]]
    card = rec["card_id"]
    if v["status"] == votes.DECLINED:
        votes.set_status(ctx, vkey, votes.DECLINED, followed_up=True)
        cases.update(ctx, v["case_key"], needs_draft=True)
        if stages.stage_of(ctx.board.get_tags(card)) == stages.AWAITING:
            cases.move(ctx, card, stages.IN_SUPPORT)
        ctx.board.post(card, f"No-reply close declined by {v.get('decided_by')}; a real reply will be drafted.",
                       idempotency_key=f"noreply-declined:{vkey}")
        return "declined"
    marker = parse_iso(v["spec"]["last_inbound"])
    try:
        inbound = parse_iso(ctx.helpdesk.last_inbound_at(rec["ticket"], rec["customer"])) if rec.get("ticket") \
            else None
    except Exception:  # noqa: BLE001 - unreadable is never "nothing new"
        return "unreadable"
    if marker is None or (inbound is not None and inbound > marker) \
            or stages.stage_of(ctx.board.get_tags(card)) != stages.AWAITING:
        votes.set_status(ctx, vkey, votes.VOID, void_reason="the customer wrote again or the card moved")
        return "void"
    if v["status"] != votes.APPROVED:
        return None
    cases.move(ctx, card, stages.DONE)
    votes.set_status(ctx, vkey, votes.DONE)
    ctx.board.post(card, f"Closed with no reply by {v.get('decided_by')} ({v['spec']['reason']}). Nothing was "
                   "sent. A new email reopens it.", idempotency_key=f"closed:{vkey}")
    events.publish(ctx, "case.closed", {"case_key": v["case_key"], "vote_key": vkey, "how": "no_reply",
                                        "closed_by": v.get("decided_by")}, f"closed:{vkey}", card_id=card,
                   content=lambda: events.content_for_case(ctx, v["case_key"], reason=v["spec"].get("reason")))
    return "closed"


def run(ctx) -> dict:
    ctx.require_running()
    out = {"opened": [], "closed": [], "voided": []}
    quiet = timedelta(days=ctx.config.policy.quiet_close_days)
    all_votes = votes.load(ctx)
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or not rec.get("ticket"):
            continue
        tags = ctx.board.get_tags(rec["card_id"])
        if not stages.is_waiting(tags):
            continue
        sent = parse_iso(ctx.helpdesk.last_sent_at(rec["ticket"]))
        inbound = parse_iso(ctx.helpdesk.last_inbound_at(rec["ticket"], rec["customer"]))
        if sent is None or (inbound and inbound > sent):
            continue
        vkey = f"close:{key}:{sent.isoformat()}"
        if vkey in all_votes or votes.live_for_case(ctx, key):
            continue
        if ctx.clock.now() - sent < quiet:
            continue
        days = (ctx.clock.now() - sent).days
        votes.open_vote(ctx, key=vkey, kind="close", case_key=key, card_id=rec["card_id"],
                        subject=f"{rec['customer'].split('@')[0]} - {rec['title']}",
                        question=f"no reply for {days} days since our answer; close this case?",
                        identity={"case_key": key}, material={"last_sent": sent.isoformat()},
                        spec={"last_sent": sent.isoformat()})
        out["opened"].append(key)
    for vkey, v in sorted(votes.load(ctx).items()):
        if v["kind"] == "close" and (v.get("spec") or {}).get("no_reply"):
            result = _no_reply_close(ctx, vkey, v)
            if result:
                out.setdefault("no_reply", {})[vkey] = result
            continue
        if v["kind"] != "close" or v["status"] not in votes.LIVE:
            continue
        rec = cases.load(ctx)[v["case_key"]]
        sent_at = parse_iso(ctx.helpdesk.last_sent_at(rec["ticket"]))
        inbound = parse_iso(ctx.helpdesk.last_inbound_at(rec["ticket"], rec["customer"]))
        still = stages.is_waiting(ctx.board.get_tags(rec["card_id"]))
        moved = (sent_at is None or sent_at != parse_iso(v["spec"]["last_sent"])
                 or (inbound is not None and inbound > sent_at))
        if not still or moved:
            votes.set_status(ctx, vkey, votes.VOID, void_reason="the customer wrote again or the card moved")
            out["voided"].append(vkey)
            continue
        if v["status"] == votes.APPROVED:
            cases.set_tags_verified(ctx, rec["card_id"], stages.close_quiet(ctx.board.get_tags(rec["card_id"])))
            votes.set_status(ctx, vkey, votes.DONE)
            ctx.board.post(rec["card_id"], f"Closed by {v.get('decided_by')}. A new email reopens it.",
                           idempotency_key=f"closed:{vkey}")
            events.publish(ctx, "case.closed", {"case_key": v["case_key"], "vote_key": vkey, "how": "quiet",
                                                "closed_by": v.get("decided_by")}, f"closed:{vkey}",
                           card_id=rec["card_id"], content=lambda: events.content_for_case(ctx, v["case_key"]))
            out["closed"].append(v["case_key"])
    ctx.beat("close", str(out))
    return out
