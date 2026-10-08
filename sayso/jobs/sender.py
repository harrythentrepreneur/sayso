"""Sender: send an APPROVED reply exactly once, then prove it left.

Order of operations (each step fails closed):
 1. The vote is approved, its fingerprint still matches the draft file, and
    the card still sits in Awaiting approval.
 2. Safety checks: no payment identifiers; no money claim without a verified
    money receipt for this case.
 3. An attempt record is written BEFORE the send. If one already exists and is
    unresolved, the sender does NOT send again: it only re-reads for proof.
 4. Send with an idempotency key.
 5. Receipt = exactly one Sent record in the helpdesk to exactly this recipient
    from our support address, AND exactly one copy in the mailbox's own Sent
    folder. A local row is never the receipt.
 6. If proof does not arrive in the read-back window, the attempt is marked
    UNVERIFIED, an alert fires once, and nothing retries automatically.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sayso import cases, safety, stages, votes
from sayso.store import load_json, locked, save_json

PENDING, VERIFIED, UNVERIFIED = "pending", "SENT_AND_VERIFIED", "UNVERIFIED"


def verified_money(ctx, case_key: str) -> set[str]:
    out = set()
    for v in votes.load(ctx).values():
        if v["case_key"] == case_key and v["kind"] == "money" and v["status"] == votes.DONE:
            out.add("refund" if v["spec"]["action"] == "refund" else "cancel")
    return out


def check_sendable(ctx, case_key: str, case: dict, recipient: str, body: str) -> None:
    """Every text rule the sender applies. The draft job calls this SAME function
    before it opens a vote, so a vote is never opened on a reply the sender would
    refuse after the operator said Yes."""
    if cases.normalise(recipient) in ctx.config.own_addresses:
        raise safety.Refused("recipient is one of our own addresses")
    safety.check_reply_text(body)
    safety.check_claims_backed(body, verified_money(ctx, case_key))
    safety.check_plugins(ctx, case, body)


def check_receipt(ctx, ticket: str, recipient: str, body_sha: str) -> dict | None:
    rows = ctx.helpdesk.sent_readback(ticket, body_sha)
    if len(rows) != 1:
        return None
    row = rows[0]
    if row.status != "Sent" or cases.normalise(row.recipient) != recipient \
            or cases.normalise(row.sender) != ctx.config.support_address:
        return None
    if ctx.mailbox.sent_copies(row.message_id) != 1:
        return None
    return {"message_id": row.message_id, "status": VERIFIED}


def _await_receipt(ctx, ticket, recipient, body_sha, sleep):
    waited = 0.0
    policy = ctx.config.policy
    while True:
        try:
            got = check_receipt(ctx, ticket, recipient, body_sha)
        except Exception:  # noqa: BLE001 - unreadable is unproven
            got = None
        if got or waited >= policy.readback_seconds:
            return got
        sleep(policy.readback_interval)
        waited += policy.readback_interval


def send_one(ctx, rec: dict, *, sleep) -> str:
    key, spec, case_key = rec["key"], rec["spec"], rec["case_key"]
    case = cases.load(ctx)[case_key]
    body = Path(spec["path"]).read_text(encoding="utf-8")
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    if not votes.still_bound(rec, {"sha256": body_sha}):
        votes.set_status(ctx, key, votes.VOID, void_reason="draft changed after approval")
        return "void-changed"
    tags = ctx.board.get_tags(rec["card_id"])
    if stages.stage_of(tags) != stages.AWAITING:
        return "skip-stage"
    recipient = cases.normalise(rec["identity"]["recipient"])
    check_sendable(ctx, case_key, case, recipient, body)

    with locked(ctx.paths.attempts):
        attempts = load_json(ctx.paths.attempts)
        att = attempts.get(key)
        if att and att["status"] in (VERIFIED, UNVERIFIED):
            return "already-" + att["status"]
        resend = att is None
        if resend:
            attempts[key] = att = {"status": PENDING, "recipient_sha": hashlib.sha256(recipient.encode()).hexdigest(),
                                   "body_sha": body_sha, "started_at": ctx.clock.now().isoformat()}
            save_json(ctx.paths.attempts, attempts)
    ctx.journal.append("send_attempt", key=key, resend=resend)
    why = ""
    if resend:
        try:
            ctx.helpdesk.send_reply(case["ticket"], recipient, safety.reply_subject(case["title"]), body, idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - the write may have happened; prove, never re-send
            ctx.journal.append("send_error", key=key, error=type(exc).__name__)
            why = str(exc)[:300]
    receipt = _await_receipt(ctx, case["ticket"], recipient, body_sha, sleep)
    with locked(ctx.paths.attempts):
        attempts = load_json(ctx.paths.attempts)
        attempts[key]["status"] = VERIFIED if receipt else UNVERIFIED
        attempts[key]["receipt"] = receipt
        save_json(ctx.paths.attempts, attempts)
    if not receipt:
        ctx.alert(f"unverified:{key}", f"reply for {case['title']!r} was started but not proven sent. "
                  "Not retried. Check the helpdesk and mailbox Sent folder by hand.")
        ctx.board.post(rec["card_id"], "Reply send started but NOT verified. Not retried automatically."
                       + (f"\nThe helpdesk said: {why}" if why else ""), idempotency_key=f"unverified:{key}")
        return UNVERIFIED
    receipt_path = ctx.paths.receipts / case_key / f"{key.replace(':', '_')}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(receipt_path, {"vote": key, **receipt, "at": ctx.clock.now().isoformat()})
    votes.set_status(ctx, key, votes.DONE, receipt=receipt)
    ctx.board.post(rec["card_id"], "Reply sent and verified in the helpdesk and the mailbox Sent folder.",
                   idempotency_key=f"sent:{key}")
    cases.move(ctx, rec["card_id"], stages.DONE, waiting=True)
    cases.update(ctx, case_key, last_reply_at=ctx.clock.now().isoformat())
    return VERIFIED


def run(ctx, *, sleep=None) -> dict:
    ctx.require_running()
    import time
    sleep = sleep or time.sleep
    out = {}
    for key, rec in sorted(votes.load(ctx).items()):
        if rec["kind"] != "reply" or rec["status"] != votes.APPROVED:
            continue
        try:
            out[key] = send_one(ctx, rec, sleep=sleep)
        except safety.Refused as exc:
            out[key] = "refused"
            ctx.alert(f"refused:{key}", f"reply refused: {exc}")
            ctx.board.post(rec["card_id"], f"Approved reply NOT sent: {exc}", idempotency_key=f"refused:{key}")
    ctx.beat("sender", json.dumps(out))
    return out
