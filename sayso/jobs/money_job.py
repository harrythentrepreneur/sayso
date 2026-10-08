"""Money: carry out an APPROVED refund or cancellation, once, and verify it.

- Opening a money vote validates the spec against the product's cap and
  currency list; the spec is the vote's material, so it cannot change later.
- The attempt is recorded before the provider call. An existing unresolved
  attempt is only re-read, never re-executed.
- The receipt is the provider's own record matching the exact spec (amount,
  currency, status). A mismatch is an alert, never "close enough".
"""
from __future__ import annotations

from sayso import money, votes
from sayso.store import load_json, locked, save_json


def request(ctx, *, case_key: str, spec: dict, subject: str) -> dict:
    """Open a money vote. Called by an agent or operator tool, never by a customer."""
    if ctx.payments is None:
        raise money.MoneySpecError("payments adapter is off for this product")
    p = ctx.config.policy
    money.validate(spec, max_minor=p.refund_max_minor, currencies=p.currencies)
    case = load_json(ctx.paths.cases)[case_key]
    ident = {"case_key": case_key, "action": spec["action"]}
    return votes.open_vote(ctx, key=f"money:{case_key}:{spec['action']}:{spec.get('charge') or spec.get('subscription')}",
                           kind="money", case_key=case_key, card_id=case["card_id"], subject=subject,
                           question=f"{money.describe(spec)}?", identity=ident, material=spec, spec=spec)


def _verify(ctx, spec) -> dict | None:
    if spec["action"] == "refund":
        rows = [r for r in ctx.payments.read_refunds(spec["charge"]) if r.status == "succeeded"]
        if len(rows) == 1 and rows[0].amount == spec["amount"] and (rows[0].currency or "").lower() == spec["currency"]:
            return {"provider_id": rows[0].provider_id, "amount": rows[0].amount, "currency": rows[0].currency}
        return None
    sub = ctx.payments.read_subscription(spec["subscription"])
    return {"provider_id": sub.provider_id, "status": sub.status} if sub.status == "canceled" else None


def run(ctx) -> dict:
    ctx.require_running()
    out = {}
    if ctx.payments is None:
        ctx.beat("money", "payments off")
        return out
    p = ctx.config.policy
    for key, rec in sorted(votes.load(ctx).items()):
        if rec["kind"] != "money" or rec["status"] != votes.APPROVED:
            continue
        spec = rec["spec"]
        if not votes.still_bound(rec, spec):
            votes.set_status(ctx, key, votes.VOID, void_reason="money spec changed")
            out[key] = "void"
            continue
        try:
            money.validate(spec, max_minor=p.refund_max_minor, currencies=p.currencies)
        except money.MoneySpecError as exc:
            ctx.alert(f"money-invalid:{key}", f"approved money action refused: {exc}")
            out[key] = "refused"
            continue
        with locked(ctx.paths.attempts):
            attempts = load_json(ctx.paths.attempts)
            att = attempts.get(key)
            if att and att["status"] != "pending":
                out[key] = "already-" + att["status"]
                continue
            first = att is None
            if first:
                attempts[key] = {"status": "pending", "started_at": ctx.clock.now().isoformat()}
                save_json(ctx.paths.attempts, attempts)
        if first:
            try:
                if spec["action"] == "refund":
                    ctx.payments.refund(spec["charge"], spec["amount"], spec["currency"], idempotency_key=key)
                else:
                    ctx.payments.cancel_subscription(spec["subscription"], idempotency_key=key)
            except Exception as exc:  # noqa: BLE001 - may have happened; verify, never repeat
                ctx.journal.append("money_error", key=key, error=type(exc).__name__)
        receipt = _verify(ctx, spec)
        status = "VERIFIED_WITH_PROVIDER" if receipt else "UNVERIFIED"
        with locked(ctx.paths.attempts):
            attempts = load_json(ctx.paths.attempts)
            attempts[key].update(status=status, receipt=receipt)
            save_json(ctx.paths.attempts, attempts)
        if receipt:
            votes.set_status(ctx, key, votes.DONE, receipt=receipt)
            ctx.board.post(rec["card_id"], f"Done and verified with the payment provider: {money.describe(spec)}.",
                           idempotency_key=f"money-done:{key}")
        else:
            ctx.alert(f"money-unverified:{key}", f"{money.describe(spec)} for {rec['subject']!r} could not be "
                      "verified with the provider. Not retried. Check by hand.")
        out[key] = status
    ctx.beat("money", str(out))
    return out
