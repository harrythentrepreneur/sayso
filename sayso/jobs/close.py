"""Close: a card quiet for N days after our verified reply gets ONE close vote.

- Quiet is measured from the helpdesk's own record of our last sent reply,
  and voided if the customer has written since.
- One close vote per case per reply; never re-asked for the same reply.
- An approved close drops Waiting on customer, leaving Done alone.
"""
from __future__ import annotations

from datetime import timedelta

from sayso import cases, stages, votes
from sayso.store import parse_iso


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
                        subject=cases.subject(rec),
                        question=f"no reply for {days} days since our answer; close this case?",
                        identity={"case_key": key}, material={"last_sent": sent.isoformat()},
                        spec={"last_sent": sent.isoformat()})
        out["opened"].append(key)
    for vkey, v in sorted(votes.load(ctx).items()):
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
            out["closed"].append(v["case_key"])
    ctx.beat("close", str(out))
    return out
