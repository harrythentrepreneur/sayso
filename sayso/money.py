"""Money specs: the exact provider action a money vote approves.

A money vote's spec IS its material, so any change to charge, amount,
currency or subscription voids the vote. Only the money job acts on it.
"""
from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(r"^[A-Za-z0-9_\-]{3,80}$")
REFUND_FIELDS = frozenset({"action", "charge", "amount", "currency", "customer"})
CANCEL_FIELDS = frozenset({"action", "subscription", "customer"})
# Optional on a cancel: when it takes effect. Absent = the provider adapter's own default (Stripe: at once;
# Paddle: end of the billing period). "immediately" only when the approved spec says so.
CANCEL_EFFECTIVE = ("next_billing_period", "immediately")


class MoneySpecError(ValueError):
    pass


def validate(spec: Any, *, max_minor: int, currencies: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise MoneySpecError("money spec must be an object")
    action = spec.get("action")
    if action == "refund":
        if set(spec) != REFUND_FIELDS:
            raise MoneySpecError(f"refund spec needs exactly {sorted(REFUND_FIELDS)}")
        amount = spec["amount"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise MoneySpecError("amount must be a positive whole number of minor units")
        if amount > max_minor:
            raise MoneySpecError(f"amount {amount} is over the configured cap {max_minor}")
        if str(spec["currency"]).lower() not in currencies:
            raise MoneySpecError(f"currency {spec['currency']!r} is not allowed")
        if not _KEY.match(str(spec["charge"])):
            raise MoneySpecError("charge id is malformed")
    elif action == "cancel_subscription":
        if set(spec) - {"effective"} != CANCEL_FIELDS:
            raise MoneySpecError(f"cancel spec needs exactly {sorted(CANCEL_FIELDS)} (and optionally effective)")
        if "effective" in spec and spec["effective"] not in CANCEL_EFFECTIVE:
            raise MoneySpecError(f"effective must be one of {list(CANCEL_EFFECTIVE)}")
        if not _KEY.match(str(spec["subscription"])):
            raise MoneySpecError("subscription id is malformed")
    else:
        raise MoneySpecError("action must be refund or cancel_subscription")
    if not _KEY.match(str(spec["customer"])):
        raise MoneySpecError("customer id is malformed")
    return spec


def describe(spec: dict[str, Any]) -> str:
    if spec["action"] == "refund":
        return f"refund {spec['amount'] / 100:.2f} {spec['currency'].upper()}"
    when = {"immediately": " immediately", "next_billing_period": " at the end of the billing period"}
    return "cancel the subscription" + when.get(spec.get("effective"), "")
