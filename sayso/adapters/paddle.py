"""Paddle Billing (API v1) payments: refunds through adjustments, and subscription cancels.

Paddle is a merchant of record, so a refund is Paddle's decision. Most live refunds are created as
``pending_approval`` and approved (or rejected) later by Paddle. This adapter reports that honestly:
``read_refunds`` maps ``pending_approval`` to ``pending``, and only ``approved`` to ``succeeded``,
which is the one status the money job accepts as done.

Paddle has no idempotency header, so writes are made detectable instead:
- every refund carries ``sayso-ref:<hash of the idempotency key>`` in its ``reason``; before it creates a
  refund the adapter lists the transaction's refunds and returns the one with that marker;
- a cancel first reads the subscription; already canceled (or a cancel already scheduled) means no write.

Rate limits: Paddle blocks an IP for 60 s after a 429. The adapter never retries inside a tick; it raises,
and nothing is written. Anything not understood (status, shape, id, amount) raises: a guessed value turns
into a wrong money action.

Docs: https://developer.paddle.com/api-reference/adjustments/create-adjustment (and the pages named in
docs/ADAPTERS.md).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sayso.adapters.base import MoneyReceipt
from sayso.adapters.http import Client, HttpError
from sayso.config import ConfigError, Section

BASES = {"live": "https://api.paddle.com", "sandbox": "https://sandbox-api.paddle.com"}
# Paddle's documented key pattern (api-reference/about/authentication). Legacy keys (before May 2025)
# cannot carry permissions, so they are refused: Sayso only works with a least-privilege key.
KEY = re.compile(r"pdl_(live|sdbx)_apikey_[a-z\d]{26}_[a-zA-Z\d]{22}_[a-zA-Z\d]{3}")
TXN = re.compile(r"txn_[a-z\d]{26}")
SUB = re.compile(r"sub_[a-z\d]{26}")
CTM = re.compile(r"ctm_[a-z\d]{26}")
DIGITS = re.compile(r"\d{1,15}")
EFFECTIVE = ("next_billing_period", "immediately")
MARKER = "sayso-ref:"
# Paddle adjustment status -> the receipt status the core understands. Only "succeeded" is done.
REFUND_STATUS = {"approved": "succeeded", "pending_approval": "pending", "rejected": "rejected",
                 "reversed": "reversed"}
SUB_STATUS = ("active", "canceled", "past_due", "paused", "trialing")
MAX_PAGES = 20


class PaddleError(RuntimeError):
    """A Paddle call failed or answered something this adapter does not understand. Nothing is guessed."""


def environment_of(api_key: str) -> str:
    """"live" or "sandbox" from a key's own prefix. Raises ConfigError for anything else (legacy keys too)."""
    m = KEY.fullmatch(api_key or "")
    if not m:
        raise ConfigError("paddle: the API key does not match Paddle's key format (pdl_live_apikey_... or "
                          "pdl_sdbx_apikey_...); legacy keys are refused because they cannot be limited")
    return "live" if m.group(1) == "live" else "sandbox"


def marker(idempotency_key: str) -> str:
    """The text put in a refund's reason so a retry can find the refund it already made."""
    return MARKER + hashlib.sha256(str(idempotency_key).encode()).hexdigest()[:16]


def _id(value: Any, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PaddleError(f"paddle: {what} id is not a Paddle {what} id")
    return value


def _minor(value: Any, what: str) -> int:
    if not isinstance(value, str) or not DIGITS.fullmatch(value):
        raise PaddleError(f"paddle: {what} is not a whole amount in the lowest denomination")
    return int(value)


def _data(out: Any, kind: type) -> Any:
    if not isinstance(out, dict) or not isinstance(out.get("data"), kind):
        raise PaddleError("paddle: the response has an unexpected shape")
    return out["data"]


class PaddlePayments:
    """Satisfies sayso.adapters.base.Payments. ``charge`` is a Paddle transaction id (``txn_...``)."""

    provider = "Paddle"

    def __init__(self, section: Section, transport=None):
        env = str(section.options.get("environment") or "live")
        self._setup(section.secret("api_key"), env, transport)

    @classmethod
    def from_key(cls, api_key: str, environment: str | None = None, transport=None) -> "PaddlePayments":
        """For hosts that keep the key somewhere other than an env var. ``environment`` defaults to the key's own."""
        self = cls.__new__(cls)
        self._setup(api_key, environment or environment_of(api_key), transport)
        return self

    def _setup(self, api_key: str, environment: str, transport) -> None:
        if environment not in BASES:
            raise ConfigError("paddle: environment must be \"live\" or \"sandbox\"")
        if environment_of(api_key) != environment:
            raise ConfigError(f"paddle: the API key is not a {environment} key; it must match the environment")
        self.environment = environment
        self.base = BASES[environment]
        self.http = Client(self.base, {"Authorization": "Bearer " + api_key, "Paddle-Version": "1"}, transport,
                           max_retries=0)

    def __repr__(self) -> str:
        return f"PaddlePayments({self.environment})"

    def _call(self, method: str, path: str, **kw) -> Any:
        try:
            return self.http.request(method, path, **kw)
        except HttpError as exc:
            if exc.status == 429:
                raise PaddleError("paddle: rate limited (HTTP 429). Paddle blocks this address for 60 seconds; "
                                  "nothing was written, the next run reads again") from None
            raise PaddleError(f"paddle: {method} {path.split('?')[0]} failed with HTTP {exc.status}") from exc

    # -- reads ------------------------------------------------------------------------------------
    def transaction(self, txn: str) -> dict:
        t = _data(self._call("GET", f"/transactions/{_id(txn, TXN, 'transaction')}"), dict)
        if t.get("id") != txn:
            raise PaddleError("paddle: the transaction read back is a different transaction")
        return t

    def subscription(self, sub: str) -> dict:
        s = _data(self._call("GET", f"/subscriptions/{_id(sub, SUB, 'subscription')}"), dict)
        if s.get("id") != sub or s.get("status") not in SUB_STATUS:
            raise PaddleError("paddle: the subscription read back is not understood")
        return s

    def refund_adjustments(self, txn: str) -> list[dict]:
        """Every refund adjustment on one transaction, oldest page last; all pages."""
        _id(txn, TXN, "transaction")
        rows: list[dict] = []
        query = {"transaction_id": txn, "action": "refund", "per_page": 50}
        for _ in range(MAX_PAGES):
            out = self._call("GET", "/adjustments", query=query)
            page = _data(out, list)
            for a in page:
                if not isinstance(a, dict) or a.get("transaction_id") != txn or a.get("action") != "refund":
                    raise PaddleError("paddle: an adjustment read back is not a refund on this transaction")
            rows.extend(page)
            pag = (out.get("meta") or {}).get("pagination") or {}
            if not pag.get("has_more") or not page:
                return rows
            query = dict(query, after=page[-1]["id"])
        raise PaddleError("paddle: too many refund pages on one transaction")

    def read_refunds(self, charge):
        out = []
        for a in self.refund_adjustments(charge):
            status = REFUND_STATUS.get(str(a.get("status")))
            totals = a.get("totals") or {}
            if status is None or not isinstance(a.get("currency_code"), str):
                raise PaddleError("paddle: a refund has a status or currency this adapter does not understand")
            out.append(MoneyReceipt("refund", str(a["id"]), _minor(totals.get("total"), "refund total"),
                                    a["currency_code"].lower(), status,
                                    {"paddle_status": a["status"], "type": a.get("type")}))
        return out

    def read_subscription(self, subscription):
        s = self.subscription(subscription)
        change = s.get("scheduled_change") or {}
        if s["status"] == "canceled":
            return MoneyReceipt("cancel_subscription", s["id"], None, None, "canceled",
                                {"canceled_at": s.get("canceled_at")})
        if change.get("action") == "cancel":
            return MoneyReceipt("cancel_subscription", s["id"], None, None, "cancel_scheduled",
                                {"effective_at": change.get("effective_at"), "paddle_status": s["status"]})
        return MoneyReceipt("cancel_subscription", s["id"], None, None, s["status"])

    # ownership reads, used by hosts before a money vote opens. They move no money.
    def charge_customer(self, charge):
        return self.transaction(charge).get("customer_id")

    def subscription_customer(self, subscription):
        return self.subscription(subscription).get("customer_id")

    def customer_email(self, customer):
        c = _data(self._call("GET", f"/customers/{_id(customer, CTM, 'customer')}"), dict)
        return c.get("email") if c.get("id") == customer else None

    def check(self) -> None:
        """Safe connection test: list at most one transaction. Moves no money."""
        _data(self._call("GET", "/transactions", query={"per_page": 1}), list)

    # -- writes -----------------------------------------------------------------------------------
    def refund(self, charge, amount, currency, idempotency_key):
        txn = _id(charge, TXN, "transaction")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise PaddleError("paddle: the refund amount must be a positive whole number")
        t = self.transaction(txn)
        if t.get("status") != "completed":
            raise PaddleError("paddle: only a completed transaction can be refunded")
        if str(t.get("currency_code", "")).lower() != str(currency).lower():
            raise PaddleError("paddle: the transaction's currency differs from the approved currency")
        totals = (t.get("details") or {}).get("totals") or {}
        grand = _minor(totals.get("grand_total"), "transaction total")
        tag = marker(idempotency_key)
        existing = self.refund_adjustments(txn)
        for a in existing:
            if tag in str(a.get("reason", "")):
                return str(a["id"])                      # this exact approval already made its refund
        if any(a.get("status") == "pending_approval" for a in existing):
            raise PaddleError("paddle: this transaction already has a refund waiting for Paddle's approval; "
                              "Paddle refuses a second one until it is decided")
        live = [a for a in existing if a.get("status") in ("approved", "pending_approval")]
        refunded = sum(_minor((a.get("totals") or {}).get("total"), "refund total") for a in live)
        if amount > grand - refunded:
            raise PaddleError("paddle: the refund is more than what is left to refund on this transaction")
        body: dict[str, Any] = {"action": "refund", "transaction_id": txn,
                                "reason": f"Approved in Sayso ({tag})"}
        if amount == grand and not live:
            body["type"] = "full"
        else:
            body.update(type="partial", items=self._items(t, existing, amount))
        a = _data(self._call("POST", "/adjustments", json_body=body), dict)
        if a.get("transaction_id") != txn or a.get("action") != "refund" or a.get("status") not in REFUND_STATUS:
            raise PaddleError("paddle: the refund Paddle created is not the one that was asked for")
        if str(a.get("currency_code", "")).lower() != str(currency).lower():
            raise PaddleError("paddle: refund currency differs from the approved currency")
        if _minor((a.get("totals") or {}).get("total"), "refund total") != amount:
            raise PaddleError("paddle: the refund total differs from the approved amount")
        return str(a["id"])

    @staticmethod
    def _items(t: dict, existing: list[dict], amount: int) -> list[dict]:
        """Spread a partial refund over the line items in order, never past what is left on each one."""
        done: dict[str, int] = {}
        for a in existing:
            if a.get("status") in ("approved", "pending_approval"):
                for it in a.get("items") or []:
                    done[it.get("item_id")] = done.get(it.get("item_id"), 0) + _minor(
                        (it.get("totals") or {}).get("total"), "refunded item")
        items, left = [], amount
        for li in (t.get("details") or {}).get("line_items") or []:
            if left == 0:
                break
            if not isinstance(li, dict) or not str(li.get("id", "")).startswith("txnitm_"):
                raise PaddleError("paddle: a transaction line item is not understood")
            room = _minor((li.get("totals") or {}).get("total"), "line item total") - done.get(li["id"], 0)
            take = min(room, left)
            if take <= 0:
                continue
            items.append({"item_id": li["id"], "type": "partial", "amount": str(take)})
            left -= take
        if left:
            raise PaddleError("paddle: the line items do not hold enough to refund this amount")
        return items

    def cancel_subscription(self, subscription, idempotency_key, effective: str = "next_billing_period"):
        if effective not in EFFECTIVE:
            raise PaddleError("paddle: a cancel takes effect next_billing_period or immediately")
        s = self.subscription(subscription)
        scheduled = (s.get("scheduled_change") or {}).get("action") == "cancel"
        if s["status"] == "canceled" or (scheduled and effective == "next_billing_period"):
            return s["id"]                               # already done: no second write
        if s["status"] == "paused" and effective != "immediately":
            raise PaddleError("paddle: a paused subscription can only be canceled immediately; the approval "
                              "did not say immediately")
        out = _data(self._call("POST", f"/subscriptions/{subscription}/cancel",
                               json_body={"effective_from": effective}), dict)
        ok = out.get("status") == "canceled" if effective == "immediately" else (
            out.get("status") == "canceled" or (out.get("scheduled_change") or {}).get("action") == "cancel")
        if out.get("id") != subscription or not ok:
            raise PaddleError("paddle: the subscription read back after the cancel is not canceled")
        return out["id"]
