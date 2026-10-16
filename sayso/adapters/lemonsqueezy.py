"""Lemon Squeezy refunds and cancellations (Payments protocol). OFF unless chosen.

Lemon Squeezy is a merchant of record with a JSON:API REST API
(https://docs.lemonsqueezy.com/api). This adapter uses four calls only:

- ``POST /v1/orders/{id}/refund``      refund (``amount`` in cents; partial allowed)
- ``DELETE /v1/subscriptions/{id}``    cancel (the subscription runs to the end of the period:
                                       status ``cancelled``, ``ends_at`` set)
- ``GET /v1/orders/{id}``              read-back: ``refunded_amount``, ``status``, ``currency``
- ``GET /v1/subscriptions/{id}``       read-back: ``status``, ``cancelled``, ``ends_at``

Quirks the code is built around:
- **No idempotency header exists.** So every write READS FIRST: a refund is refused when the order
  already shows any refunded amount, and a cancel is skipped when the subscription is already
  cancelled. A retry after a lost reply therefore never refunds twice. (One consequence: a second,
  separate partial refund on the same order is refused here; do it by hand in Lemon Squeezy.)
- **Ids are bare numbers.** A bare number in a money spec could be mistaken for another provider's
  id, so specs carry prefixed ids: ``ls_order_123``, ``ls_sub_456``, ``ls_cust_789``. This adapter
  strips the prefix and refuses any other shape (a Stripe ``ch_``/``pi_``/``sub_`` id, a bare number).
- **One key can reach every store of the account,** so every object read is checked against the
  configured ``store_id`` and refused when it belongs to another store.
- Requests and responses are JSON:API: ``Accept`` and ``Content-Type`` are
  ``application/vnd.api+json``, data lives in ``data.attributes``.
- Rate limit: 300 calls/minute, HTTP 429 when exceeded (handled by ``http.Client``).

Settings::

    [payments]
    adapter = "lemonsqueezy"
    api_key_env = "ACME_LEMONSQUEEZY_KEY"
    store_id = "12345"
    test_mode = true            # optional: refuse objects whose test_mode differs
"""
from __future__ import annotations

import re
from typing import Any

from sayso.adapters.base import MoneyReceipt
from sayso.adapters.http import Client, urllib_transport
from sayso.config import ConfigError, Section

API = "https://api.lemonsqueezy.com/v1"
JSONAPI = "application/vnd.api+json"
ORDER_PREFIX = "ls_order_"
SUBSCRIPTION_PREFIX = "ls_sub_"
CUSTOMER_PREFIX = "ls_cust_"
_NUM = r"([1-9][0-9]{0,18})"
_ORDER = re.compile(r"ls_order_" + _NUM)
_SUB = re.compile(r"ls_sub_" + _NUM)
_CUST = re.compile(r"ls_cust_" + _NUM)
# Order statuses after which money may already have moved back to the customer.
REFUNDABLE_STATUS = "paid"
READABLE_ORDER_STATUSES = frozenset({"pending", "failed", "paid", "refunded", "partial_refund", "fraudulent"})
REFUNDED_STATUSES = frozenset({"paid", "partial_refund", "refunded"})
SUBSCRIPTION_STATUSES = frozenset({"on_trial", "active", "paused", "past_due", "unpaid", "cancelled", "expired"})
ENDED_STATUSES = frozenset({"cancelled", "expired"})


class LemonSqueezyError(RuntimeError):
    """Lemon Squeezy returned something this adapter does not understand, or a guard refused."""


def _strip(pattern: re.Pattern, value: Any, what: str) -> str:
    m = pattern.fullmatch(str(value)) if isinstance(value, str) else None
    if not m:
        raise ValueError(f"not a Lemon Squeezy {what} id (expected {pattern.pattern.split('(')[0]}<number>)")
    return m.group(1)


def order_id(spec_id: str) -> str:
    """``ls_order_123`` -> ``123``. Anything else raises ValueError before any call."""
    return _strip(_ORDER, spec_id, "order")


def subscription_id(spec_id: str) -> str:
    """``ls_sub_456`` -> ``456``."""
    return _strip(_SUB, spec_id, "subscription")


def customer_id(spec_id: str) -> str:
    """``ls_cust_789`` -> ``789``."""
    return _strip(_CUST, spec_id, "customer")


def jsonapi_transport(inner=None):
    """Wrap a transport so request bodies go out as JSON:API (``http.Client`` labels them JSON)."""
    inner = inner or urllib_transport

    def send(method, url, headers, body):
        headers = dict(headers)
        if body is not None:
            headers["Content-Type"] = JSONAPI
        return inner(method, url, headers, body)
    return send


class LemonSqueezyPayments:
    """Refunds and cancellations for ONE Lemon Squeezy store."""

    def __init__(self, section: Section, transport=None, *, api_key: str | None = None, sleep=None):
        store = str(section.options.get("store_id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", store):
            raise ConfigError("[payments] lemonsqueezy needs store_id (the store's number)")
        self.store_id = int(store)
        tm = section.options.get("test_mode")
        if tm is not None and not isinstance(tm, bool):
            raise ConfigError("[payments] lemonsqueezy test_mode must be true or false")
        self.test_mode = tm
        key = api_key if api_key is not None else section.secret("api_key")
        if not key:
            raise ConfigError("lemonsqueezy: the API key is empty")
        extra = {} if sleep is None else {"sleep": sleep}
        self.http = Client(API, {"Authorization": "Bearer " + key, "Accept": JSONAPI},
                           jsonapi_transport(transport), **extra)

    def __repr__(self) -> str:
        return f"LemonSqueezyPayments(store={self.store_id})"

    # ------------------------------------------------------------------ reads
    def _object(self, kind: str, num: str) -> dict:
        """GET /v1/{kind}/{num} -> attributes, after checking type, id, store and test mode."""
        doc = self.http.request("GET", f"/{kind}/{num}")
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict) or data.get("type") != kind or str(data.get("id")) != num:
            raise LemonSqueezyError(f"Lemon Squeezy answered with a different object than {kind} {num}")
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            raise LemonSqueezyError(f"{kind} {num} has no attributes")
        store = attrs.get("store_id")
        if isinstance(store, bool) or not isinstance(store, int) or store != self.store_id:
            raise LemonSqueezyError(f"{kind} {num} belongs to another store, not store {self.store_id}")
        if self.test_mode is not None and attrs.get("test_mode") is not self.test_mode:
            raise LemonSqueezyError(f"{kind} {num} test_mode differs from this connection's test_mode")
        return attrs

    def read_order(self, charge: str) -> dict:
        """The order's money fields, checked. Raises on anything not understood."""
        num = order_id(charge)
        a = self._object("orders", num)
        status, total, refunded = a.get("status"), a.get("total"), a.get("refunded_amount")
        cur = a.get("currency")
        if status not in READABLE_ORDER_STATUSES:
            raise LemonSqueezyError(f"order {num} has an unknown status")
        for name, v in (("total", total), ("refunded_amount", refunded)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise LemonSqueezyError(f"order {num} has no readable {name}")
        if not isinstance(cur, str) or not re.fullmatch(r"[A-Za-z]{3}", cur):
            raise LemonSqueezyError(f"order {num} has no readable currency")
        if refunded > total:
            raise LemonSqueezyError(f"order {num} shows more refunded than paid")
        return {"id": num, "status": status, "total": total, "refunded_amount": refunded,
                "currency": cur.lower(), "refunded": a.get("refunded") is True,
                "customer_id": a.get("customer_id"), "user_email": a.get("user_email"),
                "test_mode": a.get("test_mode")}

    def read_sub(self, subscription: str) -> dict:
        num = subscription_id(subscription)
        a = self._object("subscriptions", num)
        status = a.get("status")
        if status not in SUBSCRIPTION_STATUSES:
            raise LemonSqueezyError(f"subscription {num} has an unknown status")
        if not isinstance(a.get("cancelled"), bool):
            raise LemonSqueezyError(f"subscription {num} has no readable cancelled flag")
        return {"id": num, "status": status, "cancelled": a["cancelled"], "ends_at": a.get("ends_at"),
                "customer_id": a.get("customer_id"), "user_email": a.get("user_email"),
                "test_mode": a.get("test_mode")}

    def read_refunds(self, charge):
        """The order's refunds as ONE receipt (Lemon Squeezy keeps a running refunded_amount, not a list)."""
        o = self.read_order(charge)
        if o["refunded_amount"] == 0:
            return []
        if o["status"] not in REFUNDED_STATUSES:
            raise LemonSqueezyError(f"order {o['id']} shows a refunded amount with status {o['status']!r}")
        return [MoneyReceipt("refund", ORDER_PREFIX + o["id"], o["refunded_amount"], o["currency"], "succeeded",
                             {"order_status": o["status"], "fully_refunded": o["refunded"]})]

    def read_subscription(self, subscription):
        """status is "canceled" (the core's word) only when Lemon Squeezy says cancelled=true AND the
        status is cancelled (grace period to ends_at) or expired."""
        s = self.read_sub(subscription)
        done = s["cancelled"] is True and s["status"] in ENDED_STATUSES
        return MoneyReceipt("cancel_subscription", SUBSCRIPTION_PREFIX + s["id"], None, None,
                            "canceled" if done else s["status"],
                            {"provider_status": s["status"], "ends_at": s["ends_at"]})

    # ------------------------------------------------------------------ writes
    def refund(self, charge, amount, currency, idempotency_key):
        """Refund ``amount`` cents of the order, once. Reads the order first (no idempotency header)."""
        num = order_id(charge)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive whole number of cents")
        o = self.read_order(charge)
        if o["refunded_amount"] != 0 or o["status"] != REFUNDABLE_STATUS:
            raise LemonSqueezyError(f"order {num} is not refundable now (status {o['status']!r}, "
                                    f"already refunded {o['refunded_amount']}); not sent again")
        if o["currency"] != str(currency).lower():
            raise LemonSqueezyError("order currency differs from the approved currency")
        if amount > o["total"]:
            raise LemonSqueezyError("refund amount is more than the order total")
        self.http.request("POST", f"/orders/{num}/refund", json_body={
            "data": {"type": "orders", "id": num, "attributes": {"amount": amount}}})
        return ORDER_PREFIX + num

    def cancel_subscription(self, subscription, idempotency_key):
        """Cancel at the end of the period, once. Already cancelled/expired -> no DELETE."""
        s = self.read_sub(subscription)
        if s["status"] in ENDED_STATUSES or s["cancelled"]:
            return SUBSCRIPTION_PREFIX + s["id"]
        self.http.request("DELETE", f"/subscriptions/{s['id']}")
        return SUBSCRIPTION_PREFIX + s["id"]

    # ------------------------------------------------------------------ ownership + connection test
    def order_owner(self, charge) -> tuple[str | None, str | None]:
        """(``ls_cust_<id>``, the order's email) for an ownership check. Moves no money."""
        o = self.read_order(charge)
        return _cust(o["customer_id"]), o["user_email"] if isinstance(o["user_email"], str) else None

    def subscription_owner(self, subscription) -> tuple[str | None, str | None]:
        s = self.read_sub(subscription)
        return _cust(s["customer_id"]), s["user_email"] if isinstance(s["user_email"], str) else None

    def customer_email(self, customer) -> str | None:
        a = self._object("customers", customer_id(customer))
        return a.get("email") if isinstance(a.get("email"), str) else None

    def check(self) -> dict:
        """Safe connection test: who am I, and is the store mine. Moves no money."""
        me = self.http.request("GET", "/users/me")
        doc = self.http.request("GET", f"/stores/{self.store_id}")
        data = (doc or {}).get("data") or {}
        if data.get("type") != "stores" or str(data.get("id")) != str(self.store_id):
            raise LemonSqueezyError("the key cannot read this store")
        attrs = data.get("attributes") or {}
        return {"store_id": self.store_id, "store_name": str(attrs.get("name") or ""),
                "currency": str(attrs.get("currency") or "").lower(),
                "test_mode": ((me or {}).get("meta") or {}).get("test_mode") is True}


def _cust(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return CUSTOMER_PREFIX + str(value)


def parse_stores(doc: Any) -> list[dict]:
    """``GET /v1/stores`` -> [{"id", "name", "currency"}] (for a store picker). Raises when not understood."""
    rows = (doc or {}).get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise LemonSqueezyError("store list not understood")
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("type") != "stores" or not str(r.get("id", "")).isdigit():
            raise LemonSqueezyError("store list not understood")
        a = r.get("attributes") or {}
        out.append({"id": str(r["id"]), "name": str(a.get("name") or ""), "currency": str(a.get("currency") or "")})
    return out


__all__ = ["API", "CUSTOMER_PREFIX", "LemonSqueezyError", "LemonSqueezyPayments", "ORDER_PREFIX",
           "SUBSCRIPTION_PREFIX", "customer_id", "jsonapi_transport", "order_id", "parse_stores",
           "subscription_id"]
