"""Paddle Billing payments adapter against Paddle's OWN documented examples. No network, no account.

Every response starts from the JSON "Response example" blocks in Paddle's API reference
(tests/fixtures/paddle/, sources in SOURCES.md). ``FakePaddle`` serves them through the injectable
transport and keeps state, so a refund made now is read back by the next call. A socket guard fails any
connection beyond localhost while this module runs; ``GuardTests`` proves the guard bites.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from sayso import money, votes
from sayso.adapters import paddle, reference
from sayso.adapters.paddle import PaddleError, PaddlePayments
from sayso.config import ConfigError, Section
from sayso.jobs import money_job
from tests.helpers import LoopTest

FIX = Path(__file__).parent / "fixtures" / "paddle"


def _fake_key(env: str) -> str:
    """A made-up key in Paddle's documented shape, built at run time: no key-shaped text is ever in git
    (GitHub push protection rightly refuses even Paddle's own doc example)."""
    return "_".join(("pdl", env, "apikey", "0" * 26, "Z" * 22, "ZZZ"))


LIVE_KEY = _fake_key("live")
SDBX_KEY = _fake_key("sdbx")
LEGACY_KEY = "0f" * 25                       # 50 lowercase hex characters: the legacy shape


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# -- socket guard -------------------------------------------------------------------------------
class OutsideConnection(AssertionError):
    pass


_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise OutsideConnection(f"test tried to reach {host!r}; tests must use recorded fixtures only")
    return _real_connect(self, address)


_patch = mock.patch.object(socket.socket, "connect", _guarded_connect)


def setUpModule():
    _patch.start()


def tearDownModule():
    _patch.stop()


class GuardTests(unittest.TestCase):
    def test_guard_bites_on_an_outside_connection(self):
        # the one seeded outside call: if the guard were off, this would really try to reach Paddle
        with self.assertRaises((OutsideConnection, OSError)) as cm:
            urllib.request.urlopen("https://sandbox-api.paddle.com/event-types", timeout=5)
        self.assertIn("recorded fixtures only", repr(cm.exception) + repr(getattr(cm.exception, "reason", "")))


# -- a stateful fake Paddle built from the docs' examples ---------------------------------------------
TXN = fixture("get-transaction.response.json")["data"]["id"]          # completed, USD, grand_total 65215
SUB = fixture("get-subscription.response.json")["data"]["id"]         # active
CTM = fixture("get-transaction.response.json")["data"]["customer_id"]


class FakePaddle:
    def __init__(self):
        t = fixture("get-transaction.response.json")["data"]
        s = fixture("get-subscription.response.json")["data"]
        c = fixture("get-customer.response.json")["data"]
        c["id"] = CTM                                    # edit: the customer is the transaction's customer
        self.transactions = {t["id"]: t}
        self.subs = {s["id"]: s}
        self.customers = {CTM: c}
        self.adjustments: list[dict] = []
        self.calls: list[dict] = []
        self.fail_next: list[tuple[int, dict]] = []
        self.created_status = "pending_approval"         # Paddle's default for most live refunds
        self.page_size = 50
        self.cancel_fails = False
        self._n = 0

    def _err(self, status, code):
        return status, {"Retry-After": "60"} if status == 429 else {}, json.dumps(
            {"error": {"type": "request_error", "code": code, "detail": code}}).encode()

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "path": u.path, "query": q, "headers": dict(headers),
                           "body": payload})
        if self.fail_next:
            status, err = self.fail_next.pop(0)
            return status, {"Retry-After": "60"}, json.dumps(err).encode()
        ok = lambda data, meta=None: (200, {}, json.dumps({"data": data, "meta": meta or {"request_id": "r"}}).encode())
        parts = u.path.strip("/").split("/")
        if method == "GET" and parts[0] == "transactions" and len(parts) == 2:
            t = self.transactions.get(parts[1])
            return ok(t) if t else self._err(404, "entity_not_found")
        if method == "GET" and parts == ["transactions"]:
            return ok(list(self.transactions.values())[: int(q.get("per_page", 30))])
        if method == "GET" and parts[0] == "subscriptions" and len(parts) == 2:
            s = self.subs.get(parts[1])
            return ok(s) if s else self._err(404, "entity_not_found")
        if method == "GET" and parts[0] == "customers" and len(parts) == 2:
            c = self.customers.get(parts[1])
            return ok(c) if c else self._err(404, "entity_not_found")
        if method == "GET" and parts == ["adjustments"]:
            rows = [a for a in self.adjustments if a["transaction_id"] == q.get("transaction_id")
                    and a["action"] == q.get("action", a["action"])]
            if "after" in q:
                ids = [a["id"] for a in rows]
                rows = rows[ids.index(q["after"]) + 1:]
            page = rows[: min(int(q.get("per_page", 10)), self.page_size)]
            return ok(page, {"request_id": "r", "pagination": {"per_page": len(page), "next": "",
                                                                 "has_more": len(rows) > len(page)}})
        if method == "POST" and parts == ["adjustments"]:
            return self._create_adjustment(payload)
        if method == "POST" and parts[0] == "subscriptions" and parts[2:] == ["cancel"]:
            s = self.subs.get(parts[1])
            if not s:
                return self._err(404, "entity_not_found")
            if self.cancel_fails:
                return self._err(500, "internal_error")
            if (payload or {}).get("effective_from") == "immediately":
                s.update(status="canceled", scheduled_change=None, canceled_at="2024-04-12T11:24:54.868Z")
            else:
                s["scheduled_change"] = {"action": "cancel", "effective_at": s["next_billed_at"], "resume_at": None}
            return ok(s)
        return self._err(404, "not_found")

    def _create_adjustment(self, p):
        t = self.transactions.get(p.get("transaction_id"))
        if not t or t["status"] != "completed":
            return self._err(400, "transaction_not_completed")
        if any(a["status"] == "pending_approval" for a in self.adjustments if a["transaction_id"] == t["id"]):
            return self._err(409, "transaction_adjustment_pending")   # documented: Paddle refuses this
        a = copy.deepcopy(fixture("create-adjustment.response.json")["data"])
        self._n += 1
        a["id"] = "adj_" + f"{self._n:026d}"
        lines = {li["id"]: li for li in t["details"]["line_items"]}
        if p.get("type") == "full":
            total = int(t["details"]["totals"]["grand_total"])
            a["items"] = [{"id": "adjitm_x", "item_id": i, "type": "full", "amount": li["totals"]["total"],
                           "proration": None, "totals": dict(li["totals"])} for i, li in lines.items()]
        else:
            total = 0
            a["items"] = []
            for it in p["items"]:
                assert it["item_id"] in lines, it
                amt = int(it["amount"])
                total += amt
                a["items"].append({"id": "adjitm_x", "item_id": it["item_id"], "type": "partial", "amount": str(amt),
                                   "proration": None, "totals": {"subtotal": "0", "tax": "0", "total": str(amt)}})
        a.update(type=p.get("type", "partial"), transaction_id=t["id"], subscription_id=t["subscription_id"],
                 customer_id=t["customer_id"], reason=p["reason"], currency_code=t["currency_code"],
                 status=self.created_status)
        a["totals"]["total"] = str(total)
        self.adjustments.append(a)
        return 201, {}, json.dumps({"data": a, "meta": {"request_id": "r"}}).encode()

    def writes(self):
        return [c for c in self.calls if c["method"] == "POST"]


def adapter(fake, key=LIVE_KEY, env=None):
    return PaddlePayments.from_key(key, env, transport=fake)


# -- adapter: requests, refusals ------------------------------------------------------------------
class PaddleSetupTests(unittest.TestCase):
    def test_section_reads_secret_from_env_and_uses_live_base(self):
        fake = FakePaddle()
        with mock.patch.dict(os.environ, {"T_PADDLE": LIVE_KEY}):
            p = reference.build("payments", "paddle", Section("paddle", {"api_key_env": "T_PADDLE"}), None)  # type: ignore[arg-type]
        p.http.transport = fake
        p.check()
        c = fake.calls[0]
        self.assertEqual(c["url"], "https://api.paddle.com/transactions?per_page=1")
        self.assertEqual(c["headers"]["Authorization"], "Bearer " + LIVE_KEY)
        self.assertEqual(c["headers"]["Paddle-Version"], "1")

    def test_sandbox_base_url(self):
        fake = FakePaddle()
        with mock.patch.dict(os.environ, {"T_PADDLE": SDBX_KEY}):
            p = PaddlePayments(Section("paddle", {"api_key_env": "T_PADDLE", "environment": "sandbox"}), transport=fake)
        p.check()
        self.assertTrue(fake.calls[0]["url"].startswith("https://sandbox-api.paddle.com/"))

    def test_key_must_match_environment(self):
        with mock.patch.dict(os.environ, {"T_PADDLE": SDBX_KEY}):
            with self.assertRaises(ConfigError):
                PaddlePayments(Section("paddle", {"api_key_env": "T_PADDLE"}))      # default env is live
        with self.assertRaises(ConfigError):
            PaddlePayments.from_key(LIVE_KEY, "sandbox")

    def test_legacy_and_malformed_keys_refused(self):
        for bad in (LEGACY_KEY, "", "sk_live_123", LIVE_KEY + "x", "Bearer " + LIVE_KEY):
            with self.assertRaises(ConfigError, msg=bad):
                PaddlePayments.from_key(bad)

    def test_unknown_environment_refused(self):
        with self.assertRaises(ConfigError):
            PaddlePayments.from_key(LIVE_KEY, "staging")

    def test_config_accepts_paddle_choice(self):
        from sayso.config import ADAPTER_CHOICES
        self.assertIn("paddle", ADAPTER_CHOICES["payments"])

    def test_key_never_in_repr_or_error(self):
        fake = FakePaddle()
        fake.fail_next = [(403, {"error": {"code": "forbidden", "detail": "no permission"}})]
        p = adapter(fake)
        self.assertNotIn(LIVE_KEY, repr(p))
        with self.assertRaises(PaddleError) as cm:
            p.check()
        self.assertNotIn(LIVE_KEY, str(cm.exception))


class PaddleRefundTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaddle()
        self.p = adapter(self.fake)

    def test_full_refund_sends_type_full_with_marker(self):
        rid = self.p.refund(TXN, 65215, "usd", "money:c1:refund:" + TXN)
        (w,) = self.fake.writes()
        self.assertEqual(w["path"], "/adjustments")
        self.assertEqual(w["body"]["action"], "refund")
        self.assertEqual(w["body"]["type"], "full")
        self.assertEqual(w["body"]["transaction_id"], TXN)
        self.assertNotIn("items", w["body"])
        self.assertIn(paddle.marker("money:c1:refund:" + TXN), w["body"]["reason"])
        self.assertTrue(rid.startswith("adj_"))

    def test_partial_refund_spreads_items_in_order(self):
        self.p.refund(TXN, 40000, "usd", "k-partial")
        (w,) = self.fake.writes()
        self.assertEqual(w["body"]["type"], "partial")
        # the docs' transaction has line items worth 32662, 10887, 21666
        self.assertEqual([(i["item_id"][-6:], i["amount"]) for i in w["body"]["items"]],
                         [("r06z6n", "32662"), ("sd067y", "7338")])
        self.assertTrue(all(i["type"] == "partial" for i in w["body"]["items"]))

    def test_retry_with_same_key_finds_the_marker_and_does_not_write_again(self):
        first = self.p.refund(TXN, 1900, "usd", "money:c1:refund")
        again = self.p.refund(TXN, 1900, "usd", "money:c1:refund")
        self.assertEqual(first, again)
        self.assertEqual(len(self.fake.writes()), 1, "a retried refund must not create a second adjustment")

    def test_marker_found_on_a_later_page(self):
        self.fake.created_status = "approved"
        for i in range(3):
            self.p.refund(TXN, 100, "usd", f"other-{i}")
        self.fake.page_size = 1
        target = self.fake.adjustments[2]["id"]
        self.assertEqual(self.p.refund(TXN, 100, "usd", "other-2"), target)
        self.assertEqual(len(self.fake.writes()), 3)

    def test_other_pending_refund_blocks_a_new_one(self):
        self.p.refund(TXN, 1900, "usd", "first")
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 500, "usd", "second")
        self.assertEqual(len(self.fake.writes()), 1)

    def test_wrong_currency_refused_before_any_write(self):
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 1900, "eur", "k")
        self.assertEqual(self.fake.writes(), [])

    def test_more_than_the_total_refused(self):
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 65216, "usd", "k")
        self.assertEqual(self.fake.writes(), [])

    def test_more_than_what_is_left_refused(self):
        self.fake.created_status = "approved"
        self.p.refund(TXN, 60000, "usd", "k1")
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 6000, "usd", "k2")
        self.assertEqual(len(self.fake.writes()), 1)

    def test_second_partial_skips_what_is_already_refunded(self):
        self.fake.created_status = "approved"
        self.p.refund(TXN, 32662, "usd", "k1")
        self.p.refund(TXN, 100, "usd", "k2")
        second = self.fake.writes()[1]["body"]
        self.assertEqual(second["type"], "partial")
        self.assertEqual(second["items"], [{"item_id": "txnitm_01hv8wt98jahpbm1t1v1sd067y", "type": "partial",
                                            "amount": "100"}])

    def test_full_amount_after_a_partial_is_refused(self):
        self.fake.created_status = "approved"
        self.p.refund(TXN, 100, "usd", "k1")
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 65215, "usd", "k2")

    def test_not_completed_transaction_refused(self):
        self.fake.transactions[TXN]["status"] = "billed"
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 1900, "usd", "k")
        self.assertEqual(self.fake.writes(), [])

    def test_ids_checked_against_paddle_patterns_before_any_call(self):
        for bad in ("ch_123", "txn_../../customers", "txn_01HV8WPTQ8987QEEP44CYREWP9", TXN + "/x", ""):
            with self.assertRaises(PaddleError, msg=bad):
                self.p.refund(bad, 100, "usd", "k")
        self.assertEqual(self.fake.calls, [])

    def test_bad_amounts_refused(self):
        for bad in (0, -5, 1.5, True, "100"):
            with self.assertRaises(PaddleError, msg=repr(bad)):
                self.p.refund(TXN, bad, "usd", "k")
        self.assertEqual(self.fake.writes(), [])

    def test_created_refund_with_wrong_total_is_an_error(self):
        orig = self.fake._create_adjustment

        def wrong(p):
            status, h, raw = orig(p)
            d = json.loads(raw)
            d["data"]["totals"]["total"] = "1"
            return status, h, json.dumps(d).encode()
        self.fake._create_adjustment = wrong
        with self.assertRaises(PaddleError):
            self.p.refund(TXN, 1900, "usd", "k")

    def test_unexpected_shape_refused(self):
        p = adapter(lambda m, u, h, b: (200, {}, b'{"data": "nope"}'))
        with self.assertRaises(PaddleError):
            p.read_refunds(TXN)
        with self.assertRaises(PaddleError):
            p.refund(TXN, 100, "usd", "k")

    def test_rate_limit_makes_one_call_and_writes_nothing(self):
        self.fake.fail_next = [(429, {"error": {"code": "too_many_requests"}})]
        with self.assertRaises(PaddleError) as cm:
            self.p.refund(TXN, 1900, "usd", "k")
        self.assertIn("429", str(cm.exception))
        self.assertEqual(len(self.fake.calls), 1, "no retry inside a run: Paddle blocks the IP for 60 s")
        self.assertEqual(self.fake.writes(), [])


class PaddleReadTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaddle()
        self.p = adapter(self.fake)

    def test_read_refunds_maps_pending_honestly(self):
        self.p.refund(TXN, 1900, "usd", "k")
        (r,) = self.p.read_refunds(TXN)
        self.assertEqual((r.status, r.amount, r.currency), ("pending", 1900, "usd"))
        self.assertEqual(r.details["paddle_status"], "pending_approval")
        q = [c for c in self.fake.calls if c["path"] == "/adjustments" and c["method"] == "GET"][-1]["query"]
        self.assertEqual((q["transaction_id"], q["action"]), (TXN, "refund"))

    def test_status_map(self):
        self.p.refund(TXN, 1900, "usd", "k")
        for paddle_status, ours in (("approved", "succeeded"), ("rejected", "rejected"), ("reversed", "reversed")):
            self.fake.adjustments[0]["status"] = paddle_status
            self.assertEqual(self.p.read_refunds(TXN)[0].status, ours)

    def test_unknown_status_refused(self):
        self.p.refund(TXN, 1900, "usd", "k")
        self.fake.adjustments[0]["status"] = "approved_soon"
        with self.assertRaises(PaddleError):
            self.p.read_refunds(TXN)

    def test_the_docs_list_example_parses(self):
        rows = fixture("list-adjustments.response.json")["data"]
        refunds = [a for a in rows if a["action"] == "refund"]
        txn = refunds[0]["transaction_id"]
        p = adapter(lambda m, u, h, b: (200, {}, json.dumps({"data": refunds, "meta": {"pagination": {
            "has_more": False}}}).encode()))
        (r,) = p.read_refunds(txn)
        self.assertEqual((r.provider_id, r.amount, r.currency, r.status),
                         ("adj_01hvgf2s84dr6reszzg29zbvcm", 100, "usd", "pending"))

    def test_foreign_rows_in_a_list_refused(self):
        rows = fixture("list-adjustments.response.json")["data"]          # mixes credits and other transactions
        p = adapter(lambda m, u, h, b: (200, {}, json.dumps({"data": rows, "meta": {}}).encode()))
        with self.assertRaises(PaddleError):
            p.read_refunds(rows[0]["transaction_id"])

    def test_ownership_reads(self):
        self.assertEqual(self.p.charge_customer(TXN), CTM)
        self.assertEqual(self.p.subscription_customer(SUB), fixture("get-subscription.response.json")["data"]["customer_id"])
        self.assertEqual(self.p.customer_email(CTM), "sam@example.com")
        self.assertEqual(self.fake.writes(), [])

    def test_read_subscription_states(self):
        self.assertEqual(self.p.read_subscription(SUB).status, "active")
        self.fake.subs[SUB]["scheduled_change"] = {"action": "cancel", "effective_at": "2024-05-12T10:37:59.556997Z",
                                                   "resume_at": None}
        r = self.p.read_subscription(SUB)
        self.assertEqual((r.status, r.details["effective_at"]), ("cancel_scheduled", "2024-05-12T10:37:59.556997Z"))
        self.fake.subs[SUB]["scheduled_change"] = {"action": "pause", "effective_at": "x", "resume_at": None}
        self.assertEqual(self.p.read_subscription(SUB).status, "active")
        self.fake.subs[SUB].update(status="canceled", scheduled_change=None)
        self.assertEqual(self.p.read_subscription(SUB).status, "canceled")

    def test_docs_cancel_example_reads_as_canceled(self):
        c = fixture("cancel-subscription.response.json")
        p = adapter(lambda m, u, h, b: (200, {}, json.dumps(c).encode()))
        self.assertEqual(p.read_subscription(c["data"]["id"]).status, "canceled")

    def test_unknown_subscription_status_refused(self):
        self.fake.subs[SUB]["status"] = "zombie"
        with self.assertRaises(PaddleError):
            self.p.read_subscription(SUB)


class PaddleCancelTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaddle()
        self.p = adapter(self.fake)

    def test_default_cancels_at_period_end(self):
        self.p.cancel_subscription(SUB, "k")
        (w,) = self.fake.writes()
        self.assertEqual((w["path"], w["body"]), (f"/subscriptions/{SUB}/cancel", {"effective_from": "next_billing_period"}))
        self.assertEqual(self.p.read_subscription(SUB).status, "cancel_scheduled")

    def test_immediately_only_when_asked(self):
        self.p.cancel_subscription(SUB, "k", effective="immediately")
        self.assertEqual(self.fake.writes()[0]["body"], {"effective_from": "immediately"})
        self.assertEqual(self.p.read_subscription(SUB).status, "canceled")

    def test_retry_does_not_cancel_twice(self):
        self.p.cancel_subscription(SUB, "k")
        self.p.cancel_subscription(SUB, "k")
        self.assertEqual(len(self.fake.writes()), 1)
        self.fake.subs[SUB]["status"] = "canceled"
        self.p.cancel_subscription(SUB, "k", effective="immediately")
        self.assertEqual(len(self.fake.writes()), 1)

    def test_scheduled_cancel_then_immediately_writes_once_more(self):
        self.p.cancel_subscription(SUB, "k")
        self.p.cancel_subscription(SUB, "k2", effective="immediately")
        self.assertEqual(len(self.fake.writes()), 2)

    def test_paused_needs_immediately(self):
        self.fake.subs[SUB]["status"] = "paused"
        with self.assertRaises(PaddleError):
            self.p.cancel_subscription(SUB, "k")
        self.assertEqual(self.fake.writes(), [])

    def test_unknown_effective_refused(self):
        with self.assertRaises(PaddleError):
            self.p.cancel_subscription(SUB, "k", effective="tomorrow")
        self.assertEqual(self.fake.calls, [])

    def test_response_not_canceled_is_an_error(self):
        p = adapter(lambda m, u, h, b: (200, {}, json.dumps(fixture("get-subscription.response.json")).encode()))
        with self.assertRaises(PaddleError):
            p.cancel_subscription(SUB, "k")


class MoneySpecTests(unittest.TestCase):
    CAP = {"max_minor": 100000, "currencies": ("usd",)}

    def test_paddle_ids_pass_the_spec_check(self):
        money.validate({"action": "refund", "charge": TXN, "amount": 1900, "currency": "usd", "customer": CTM}, **self.CAP)
        money.validate({"action": "cancel_subscription", "subscription": SUB, "customer": CTM}, **self.CAP)

    def test_effective_is_optional_and_checked(self):
        spec = {"action": "cancel_subscription", "subscription": SUB, "customer": CTM}
        for ok in ("immediately", "next_billing_period"):
            money.validate(dict(spec, effective=ok), **self.CAP)
        with self.assertRaises(money.MoneySpecError):
            money.validate(dict(spec, effective="tomorrow"), **self.CAP)
        with self.assertRaises(money.MoneySpecError):
            money.validate(dict(spec, extra=1), **self.CAP)

    def test_describe_names_when(self):
        spec = {"action": "cancel_subscription", "subscription": SUB, "customer": CTM}
        self.assertEqual(money.describe(spec), "cancel the subscription")
        self.assertEqual(money.describe(dict(spec, effective="immediately")), "cancel the subscription immediately")


# -- the loop, with Paddle as the payments provider ---------------------------------------------------
REFUND = {"action": "refund", "charge": TXN, "amount": 1900, "currency": "usd", "customer": CTM}
CANCEL = {"action": "cancel_subscription", "subscription": SUB, "customer": CTM}


class PaddleLoopTests(LoopTest):
    def setUp(self):
        super().setUp()
        self.fake = FakePaddle()
        self.ctx.payments = adapter(self.fake)

    def approve(self, spec):
        self.write_in()
        self.tick("intake")
        key, _ = self.only_case()
        v = money_job.request(self.ctx, case_key=key, subject="Pat - charged twice", spec=spec)
        self.vote("money")
        self.tick("votes")
        return v["key"]

    def status(self, key):
        return votes.load(self.ctx)[key]["status"]

    def attempt(self, key):
        from sayso.store import load_json
        return load_json(self.ctx.paths.attempts)[key]

    def test_pending_refund_is_not_done_and_not_alerted(self):
        key = self.approve(REFUND)
        out = self.tick("money")["money"]
        self.assertEqual(out[key], money_job.PENDING_WITH_PROVIDER)
        self.assertNotEqual(self.status(key), votes.DONE)
        self.assertEqual(self.attempt(key)["status"], money_job.PENDING_WITH_PROVIDER)
        self.assertFalse(any("could not be verified" in a for a in self.w.data["alerts"]))

    def test_pending_refund_is_reread_never_reexecuted_then_done_when_approved(self):
        key = self.approve(REFUND)
        for _ in range(4):
            self.tick("money")
        self.assertEqual(len(self.fake.writes()), 1, "a pending refund must never be created again")
        self.assertNotEqual(self.status(key), votes.DONE)
        self.fake.adjustments[0]["status"] = "approved"                 # Paddle approves it
        self.tick("money")
        self.assertEqual(self.status(key), votes.DONE)
        self.assertEqual(self.attempt(key)["status"], "VERIFIED_WITH_PROVIDER")
        self.assertEqual(self.attempt(key)["receipt"]["provider_id"], self.fake.adjustments[0]["id"])
        self.assertEqual(len(self.fake.writes()), 1)
        card = votes.load(self.ctx)[key]["card_id"]
        msgs = self.ctx.board.messages(card)
        self.assertEqual(sum("Waiting for the payment provider" in m for m in msgs), 1, "one pending note, not one a run")

    def test_rejected_refund_is_unverified_and_alerted(self):
        key = self.approve(REFUND)
        self.tick("money")
        self.fake.adjustments[0]["status"] = "rejected"
        self.tick("money")
        self.assertNotEqual(self.status(key), votes.DONE)
        self.assertEqual(self.attempt(key)["status"], "UNVERIFIED")
        self.assertTrue(any("could not be verified" in a for a in self.w.data["alerts"]))
        self.tick("money")
        self.assertEqual(len(self.fake.writes()), 1)

    def test_auto_approved_refund_is_done_at_once(self):
        self.fake.created_status = "approved"
        key = self.approve(REFUND)
        self.tick("money")
        self.assertEqual(self.status(key), votes.DONE)

    def test_pending_refund_for_a_different_amount_is_not_waiting(self):
        key = self.approve(REFUND)
        self.tick("money")
        self.fake.adjustments[0]["totals"]["total"] = "1800"
        self.tick("money")
        self.assertEqual(self.attempt(key)["status"], "UNVERIFIED")

    def test_cancel_default_is_period_end_and_counts_as_done(self):
        key = self.approve(CANCEL)
        self.tick("money")
        (w,) = self.fake.writes()
        self.assertEqual(w["body"], {"effective_from": "next_billing_period"})
        self.assertEqual(self.status(key), votes.DONE)
        self.assertEqual(self.attempt(key)["receipt"]["status"], "cancel_scheduled")

    def test_cancel_immediately_only_done_when_canceled(self):
        key = self.approve(dict(CANCEL, effective="immediately"))
        self.tick("money")
        self.assertEqual(self.fake.writes()[0]["body"], {"effective_from": "immediately"})
        self.assertEqual(self.status(key), votes.DONE)

    def test_immediately_spec_with_only_a_scheduled_cancel_is_not_done(self):
        self.fake.subs[SUB]["scheduled_change"] = {"action": "cancel", "effective_at": "2024-05-12T10:37:59Z",
                                                   "resume_at": None}
        self.fake.cancel_fails = True                   # the immediate cancel call itself fails
        key = self.approve(dict(CANCEL, effective="immediately"))
        self.tick("money")
        self.assertEqual(self.fake.writes()[0]["body"], {"effective_from": "immediately"})
        self.assertNotEqual(self.status(key), votes.DONE, "a scheduled cancel is not an immediate one")
        self.assertEqual(self.attempt(key)["status"], "UNVERIFIED")

    def test_rate_limited_refund_is_never_retried(self):
        key = self.approve(REFUND)
        self.fake.fail_next = [(429, {"error": {"code": "too_many_requests"}})] * 3
        for _ in range(2):
            with self.assertRaises(PaddleError):        # the read-back is rate limited too: the run stops
                self.tick("money")
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.attempt(key)["status"], "pending", "only re-read later, never re-executed")
        self.tick("money")                              # the limit has passed: read again, nothing was made
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.attempt(key)["status"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
