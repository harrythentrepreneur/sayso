"""Lemon Squeezy payments adapter against recorded fixtures from the vendor's own docs.

No network, no account: a socket guard fails any non-localhost connection for this module,
and one seeded outside call proves the guard bites. Fixtures live in
tests/fixtures/lemonsqueezy/ and name their source page in "_source".
"""
from __future__ import annotations

import copy
import json
import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from sayso import money
from sayso.adapters import lemonsqueezy as ls
from sayso.adapters.http import HttpError
from sayso.config import ConfigError, Section

FIX = Path(__file__).parent / "fixtures" / "lemonsqueezy"
ENV = {"T_LS_KEY": "ls-test-key-not-real"}


def fixture(name):
    doc = json.loads((FIX / f"{name}.json").read_text())
    doc.pop("_source", None)
    doc.pop("_derived", None)
    return doc


# ----------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect
_real_create = socket.create_connection
_LOCAL = ("127.0.0.1", "::1", "localhost")


class OutsideCall(AssertionError):
    pass


def _guard_addr(addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    if isinstance(host, str) and host not in _LOCAL and not host.startswith("/"):
        raise OutsideCall(f"test tried to reach {host!r}: no network in tests")


def _guarded_connect(self, addr):
    _guard_addr(addr)
    return _real_connect(self, addr)


def _guarded_create(addr, *a, **k):
    _guard_addr(addr)
    return _real_create(addr, *a, **k)


def setUpModule():
    socket.socket.connect = _guarded_connect
    socket.create_connection = _guarded_create


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


# ----------------------------------------------------------------------------- fake Lemon Squeezy
class FakeLemon:
    """A stateful fake built from the doc examples. Refund/cancel change state like the docs say."""

    def __init__(self, *, store_id=1, order_status="paid", refunded=0, sub_status="active", test_mode=False):
        self.order = fixture("order_paid_derived")
        a = self.order["data"]["attributes"]
        a.update(store_id=store_id, status=order_status, refunded_amount=refunded, test_mode=test_mode)
        self.sub = fixture("subscription")
        self.sub["data"]["attributes"].update(store_id=store_id, status=sub_status,
                                               cancelled=sub_status in ("cancelled", "expired"),
                                               test_mode=test_mode)
        self.customer = fixture("customer")
        self.customer["data"]["attributes"].update(store_id=store_id, test_mode=test_mode)
        self.calls = []
        self.queue = []          # (status, body) responses served before the normal logic
        self.lose_reply = False  # perform the write but answer 502 (a lost reply)

    def writes(self):
        return [c for c in self.calls if c["method"] in ("POST", "DELETE", "PATCH")]

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        if self.queue:
            status, payload = self.queue.pop(0)
            return status, {}, json.dumps(payload).encode()
        assert url.startswith("https://api.lemonsqueezy.com/v1/"), url
        path = url[len("https://api.lemonsqueezy.com/v1"):]
        if method == "GET" and path == "/orders/1":
            return 200, {}, json.dumps(self.order).encode()
        if method == "POST" and path == "/orders/1/refund":
            a = self.order["data"]["attributes"]
            amount = (json.loads(body)["data"]["attributes"] or {}).get("amount", a["total"])
            a["refunded_amount"] += amount
            a["status"] = "refunded" if a["refunded_amount"] >= a["total"] else "partial_refund"
            a["refunded"] = a["status"] == "refunded"
            if self.lose_reply:
                return 502, {}, b"bad gateway"
            return 200, {}, json.dumps(self.order).encode()
        if method == "GET" and path == "/subscriptions/1":
            return 200, {}, json.dumps(self.sub).encode()
        if method == "DELETE" and path == "/subscriptions/1":
            self.sub["data"]["attributes"].update(status="cancelled", cancelled=True,
                                                  ends_at="2022-11-12T00:00:00.000000Z")
            if self.lose_reply:
                return 502, {}, b"bad gateway"
            return 200, {}, json.dumps(self.sub).encode()
        if method == "GET" and path == "/customers/1":
            return 200, {}, json.dumps(self.customer).encode()
        if method == "GET" and path == "/users/me":
            return 200, {}, json.dumps(fixture("user")).encode()
        if method == "GET" and path == "/stores/1":
            return 200, {}, json.dumps(fixture("store")).encode()
        return 404, {}, json.dumps({"errors": [{"status": "404"}]}).encode()


def adapter(fake, **opts):
    options = {"api_key_env": "T_LS_KEY", "store_id": "1", **opts}
    return ls.LemonSqueezyPayments(Section("lemonsqueezy", options), transport=fake, sleep=lambda s: None)


@mock.patch.dict(os.environ, ENV)
class SocketGuardTests(unittest.TestCase):
    def test_guard_bites_on_an_outside_call(self):
        """Seeded outside call: the real adapter with NO fake transport must be stopped by the guard."""
        a = ls.LemonSqueezyPayments(Section("lemonsqueezy", {"api_key_env": "T_LS_KEY", "store_id": "1"}))
        with self.assertRaises(OutsideCall):
            a.read_order("ls_order_1")


class IdTests(unittest.TestCase):
    def test_prefixed_ids_strip(self):
        self.assertEqual(ls.order_id("ls_order_123"), "123")
        self.assertEqual(ls.subscription_id("ls_sub_456"), "456")
        self.assertEqual(ls.customer_id("ls_cust_789"), "789")

    def test_other_shapes_refused(self):
        for bad in ("123", "ch_123", "pi_123", "sub_123", "ls_order_", "ls_order_abc", "ls_order_01",
                    "ls_sub_1", "ls_order_1/../2", "ls_order_1 ", 123, None):
            with self.assertRaises(ValueError, msg=bad):
                ls.order_id(bad)
        with self.assertRaises(ValueError):
            ls.subscription_id("ls_order_1")
        with self.assertRaises(ValueError):
            ls.customer_id("cus_1")

    def test_money_validate_accepts_prefixed_ids(self):
        spec = {"action": "refund", "charge": "ls_order_1", "amount": 500, "currency": "usd",
                "customer": "ls_cust_1"}
        self.assertEqual(money.validate(spec, max_minor=5000, currencies=("usd",)), spec)
        cancel = {"action": "cancel_subscription", "subscription": "ls_sub_1", "customer": "ls_cust_1"}
        self.assertEqual(money.validate(cancel, max_minor=5000, currencies=("usd",)), cancel)


class OutgoingTextTests(unittest.TestCase):
    """A Lemon Squeezy id must never reach a customer: the send-time check and the masker both catch it."""

    def test_send_check_refuses_ls_ids(self):
        from sayso import safety
        for text in ("Your order ls_order_12 is refunded.", "ls_sub_9 is cancelled", "customer ls_cust_77"):
            with self.assertRaises(safety.Refused, msg=text):
                safety.check_no_payment_identifiers(text)
        safety.check_no_payment_identifiers("Your order 12 is on its way.")

    def test_masker_finds_ls_ids(self):
        from sayso import privacy
        self.assertTrue(privacy.PAYMENT_ID.search("see ls_order_5"))
        self.assertTrue(privacy.PAYMENT_ID.search("see ls_sub_5 and ls_cust_6"))


@mock.patch.dict(os.environ, ENV)
class ConfigTests(unittest.TestCase):
    def test_store_id_required(self):
        for bad in ({}, {"store_id": ""}, {"store_id": "abc"}, {"store_id": "0"}):
            with self.assertRaises(ConfigError):
                ls.LemonSqueezyPayments(Section("lemonsqueezy", {"api_key_env": "T_LS_KEY", **bad}),
                                        transport=FakeLemon())

    def test_key_from_env_only(self):
        with self.assertRaises(ConfigError):
            ls.LemonSqueezyPayments(Section("lemonsqueezy", {"store_id": "1"}), transport=FakeLemon())

    def test_choice_and_build(self):
        from sayso.adapters import reference
        from sayso.config import ADAPTER_CHOICES
        self.assertIn("lemonsqueezy", ADAPTER_CHOICES["payments"])
        built = reference.build("payments", "lemonsqueezy",
                                Section("lemonsqueezy", {"api_key_env": "T_LS_KEY", "store_id": "7"}), None)
        self.assertIsInstance(built, ls.LemonSqueezyPayments)
        self.assertEqual(built.store_id, 7)

    def test_settings_file_loads_lemonsqueezy(self):
        import tempfile
        from sayso import config
        from sayso.demo import DEMO_TOML
        text = DEMO_TOML.format(state="/tmp/sayso-ls-config-test").replace(
            '[payments]\nadapter = "fake"',
            '[payments]\nadapter = "lemonsqueezy"\napi_key_env = "T_LS_KEY"\nstore_id = "1"\ntest_mode = true')
        self.assertIn('adapter = "lemonsqueezy"', text)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.toml"
            p.write_text(text)
            with self.assertRaisesRegex(ConfigError, "live"):
                config.load(p)                          # a real adapter needs mode = "live"
            p.write_text(text.replace('mode = "dry-run"', 'mode = "live"'))
            cfg = config.load(p)
            self.assertEqual(cfg.section("payments").adapter, "lemonsqueezy")
            p.write_text(text.replace('api_key_env = "T_LS_KEY"', 'api_key_env = "lsk_secret_value"'))
            with self.assertRaisesRegex(ConfigError, "environment variable"):
                config.load(p)                          # a secret in the file is refused

    def test_repr_has_no_key(self):
        self.assertNotIn("ls-test-key", repr(adapter(FakeLemon())))


@mock.patch.dict(os.environ, ENV)
class RequestShapeTests(unittest.TestCase):
    def test_headers_jsonapi_and_bearer(self):
        fake = FakeLemon()
        adapter(fake).read_order("ls_order_1")
        h = fake.calls[0]["headers"]
        self.assertEqual(h["Accept"], "application/vnd.api+json")
        self.assertEqual(h["Authorization"], "Bearer ls-test-key-not-real")
        self.assertEqual(fake.calls[0]["url"], "https://api.lemonsqueezy.com/v1/orders/1")

    def test_refund_body_is_jsonapi_with_exact_amount(self):
        fake = FakeLemon()
        self.assertEqual(adapter(fake).refund("ls_order_1", 500, "usd", idempotency_key="k"), "ls_order_1")
        post = fake.writes()
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["url"], "https://api.lemonsqueezy.com/v1/orders/1/refund")
        self.assertEqual(post[0]["headers"]["Content-Type"], "application/vnd.api+json")
        self.assertEqual(post[0]["body"], {"data": {"type": "orders", "id": "1", "attributes": {"amount": 500}}})

    def test_cancel_is_delete(self):
        fake = FakeLemon()
        self.assertEqual(adapter(fake).cancel_subscription("ls_sub_1", idempotency_key="k"), "ls_sub_1")
        self.assertEqual([(c["method"], c["url"]) for c in fake.writes()],
                         [("DELETE", "https://api.lemonsqueezy.com/v1/subscriptions/1")])

    def test_bad_id_makes_no_call(self):
        fake = FakeLemon()
        a = adapter(fake)
        for call in (lambda: a.refund("ch_1", 500, "usd", idempotency_key="k"),
                     lambda: a.refund("1", 500, "usd", idempotency_key="k"),
                     lambda: a.cancel_subscription("sub_1", idempotency_key="k"),
                     lambda: a.read_refunds("pi_1"),
                     lambda: a.read_subscription("ls_order_1")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(fake.calls, [])


@mock.patch.dict(os.environ, ENV)
class NoDoubleWriteTests(unittest.TestCase):
    """No idempotency header exists, so every write reads first."""

    def test_already_partly_refunded_is_refused(self):
        fake = FakeLemon(order_status="partial_refund", refunded=100)
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).refund("ls_order_1", 500, "usd", idempotency_key="k")
        self.assertEqual(fake.writes(), [])

    def test_refunded_amount_with_status_paid_is_refused(self):
        """The refund doc example shows refunded_amount 100 with status "paid": still no second refund."""
        fake = FakeLemon(order_status="paid", refunded=100)
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).refund("ls_order_1", 500, "usd", idempotency_key="k")
        self.assertEqual(fake.writes(), [])

    def test_not_paid_statuses_refused(self):
        for status in ("pending", "failed", "fraudulent", "refunded"):
            fake = FakeLemon(order_status=status)
            with self.assertRaises(ls.LemonSqueezyError, msg=status):
                adapter(fake).refund("ls_order_1", 500, "usd", idempotency_key="k")
            self.assertEqual(fake.writes(), [], status)

    def test_retry_after_lost_reply_never_refunds_twice(self):
        fake = FakeLemon()
        fake.lose_reply = True
        a = adapter(fake)
        with self.assertRaises(HttpError):
            a.refund("ls_order_1", 500, "usd", idempotency_key="k")
        with self.assertRaises(ls.LemonSqueezyError):
            a.refund("ls_order_1", 500, "usd", idempotency_key="k")
        self.assertEqual(len(fake.writes()), 1)
        self.assertEqual(fake.order["data"]["attributes"]["refunded_amount"], 500)

    def test_currency_mismatch_refused(self):
        fake = FakeLemon()
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).refund("ls_order_1", 500, "eur", idempotency_key="k")
        self.assertEqual(fake.writes(), [])

    def test_amount_over_total_refused(self):
        fake = FakeLemon()
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).refund("ls_order_1", 1200, "usd", idempotency_key="k")
        self.assertEqual(fake.writes(), [])

    def test_bad_amount_refused(self):
        fake = FakeLemon()
        for bad in (0, -1, True, 5.0, "500"):
            with self.assertRaises(ValueError, msg=bad):
                adapter(fake).refund("ls_order_1", bad, "usd", idempotency_key="k")
        self.assertEqual(fake.calls, [])

    def test_already_cancelled_makes_no_delete(self):
        for status in ("cancelled", "expired"):
            fake = FakeLemon(sub_status=status)
            self.assertEqual(adapter(fake).cancel_subscription("ls_sub_1", idempotency_key="k"), "ls_sub_1")
            self.assertEqual(fake.writes(), [], status)

    def test_cancel_retry_after_lost_reply_deletes_once(self):
        fake = FakeLemon()
        fake.lose_reply = True
        a = adapter(fake)
        with self.assertRaises(HttpError):
            a.cancel_subscription("ls_sub_1", idempotency_key="k")
        a.cancel_subscription("ls_sub_1", idempotency_key="k")
        self.assertEqual(len(fake.writes()), 1)


@mock.patch.dict(os.environ, ENV)
class ReadBackTests(unittest.TestCase):
    def test_refund_doc_example_reads_as_one_receipt(self):
        fake = FakeLemon()
        fake.order = fixture("refund")                       # the vendor's refund response, verbatim
        fake.order = {"data": fake.order}                    # see RESEARCH.md: refund example is unwrapped
        rows = adapter(fake).read_refunds("ls_order_1")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.kind, r.provider_id, r.amount, r.currency, r.status),
                         ("refund", "ls_order_1", 100, "usd", "succeeded"))

    def test_no_refund_reads_empty(self):
        self.assertEqual(adapter(FakeLemon()).read_refunds("ls_order_1"), [])

    def test_missing_refunded_amount_is_not_understood(self):
        fake = FakeLemon()
        fake.order = fixture("order")                        # the retrieve example has no refunded_amount
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_refunds("ls_order_1")

    def test_other_store_refused(self):
        fake = FakeLemon(store_id=2)
        a = adapter(fake)
        for call in (lambda: a.read_refunds("ls_order_1"), lambda: a.read_subscription("ls_sub_1"),
                     lambda: a.refund("ls_order_1", 100, "usd", idempotency_key="k"),
                     lambda: a.cancel_subscription("ls_sub_1", idempotency_key="k"),
                     lambda: a.customer_email("ls_cust_1")):
            with self.assertRaises(ls.LemonSqueezyError):
                call()
        self.assertEqual(fake.writes(), [])

    def test_wrong_object_refused(self):
        fake = FakeLemon()
        fake.order["data"]["id"] = "2"
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_refunds("ls_order_1")
        fake = FakeLemon()
        fake.order["data"]["type"] = "subscriptions"
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_refunds("ls_order_1")

    def test_unknown_status_refused(self):
        fake = FakeLemon(order_status="chargeback")
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_refunds("ls_order_1")
        fake = FakeLemon(sub_status="paused_forever")
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_subscription("ls_sub_1")

    def test_more_refunded_than_paid_refused(self):
        fake = FakeLemon(order_status="refunded", refunded=5000)
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(fake).read_refunds("ls_order_1")

    def test_test_mode_binding(self):
        with self.assertRaises(ls.LemonSqueezyError):
            adapter(FakeLemon(test_mode=False), test_mode=True).read_refunds("ls_order_1")
        self.assertEqual(adapter(FakeLemon(test_mode=True), test_mode=True).read_refunds("ls_order_1"), [])

    def test_subscription_states(self):
        doc = FakeLemon()
        doc.sub = fixture("subscription_cancelled")          # the vendor's cancel response, verbatim
        r = adapter(doc).read_subscription("ls_sub_1")
        self.assertEqual((r.provider_id, r.status), ("ls_sub_1", "canceled"))
        self.assertEqual(r.details["ends_at"], "2022-11-12T00:00:00.000000Z")
        self.assertEqual(adapter(FakeLemon()).read_subscription("ls_sub_1").status, "active")
        self.assertEqual(adapter(FakeLemon(sub_status="expired")).read_subscription("ls_sub_1").status, "canceled")

    def test_cancelled_status_without_the_flag_is_not_canceled(self):
        fake = FakeLemon()
        fake.sub["data"]["attributes"].update(status="cancelled", cancelled=False)
        self.assertNotEqual(adapter(fake).read_subscription("ls_sub_1").status, "canceled")

    def test_ownership_reads(self):
        a = adapter(FakeLemon())
        self.assertEqual(a.order_owner("ls_order_1"), ("ls_cust_1", "customer@example.com"))
        self.assertEqual(a.subscription_owner("ls_sub_1"), ("ls_cust_1", "customer@example.com"))
        self.assertEqual(a.customer_email("ls_cust_1"), "customer@example.com")

    def test_check_moves_no_money(self):
        fake = FakeLemon()
        out = adapter(fake).check()
        self.assertEqual(out["store_id"], 1)
        self.assertEqual(out["store_name"], "My Store")
        self.assertTrue(out["test_mode"])
        self.assertEqual(fake.writes(), [])

    def test_parse_stores(self):
        doc = json.loads((FIX / "store.json").read_text())
        doc.pop("_source")
        rows = ls.parse_stores({"data": [doc["data"]]})
        self.assertEqual(rows, [{"id": "1", "name": "My Store", "currency": "USD"}])
        with self.assertRaises(ls.LemonSqueezyError):
            ls.parse_stores({"data": [{"type": "users", "id": "1"}]})


@mock.patch.dict(os.environ, ENV)
class RateLimitTests(unittest.TestCase):
    def test_429_then_ok(self):
        fake = FakeLemon()
        fake.queue = [(429, {"errors": [{"status": "429"}]})]
        self.assertEqual(adapter(fake).read_refunds("ls_order_1"), [])
        self.assertEqual(len(fake.calls), 2)

    def test_429_exhausted_raises(self):
        fake = FakeLemon()
        fake.queue = [(429, {})] * 5
        with self.assertRaises(HttpError):
            adapter(fake).read_refunds("ls_order_1")

    def test_401_raises(self):
        fake = FakeLemon()
        fake.queue = [(401, {"errors": [{"status": "401", "title": "Unauthenticated"}]})]
        with self.assertRaises(HttpError):
            adapter(fake).check()


@mock.patch.dict(os.environ, ENV)
class MoneyJobTests(unittest.TestCase):
    """The core money job, end to end, with this adapter on the fake: one refund, verified, never repeated."""

    def setUp(self):
        import tempfile
        from sayso import cases, demo
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-ls-")
        self.ctx = demo.build(Path(self._tmp.name))
        self.fake = FakeLemon()
        self.ctx.payments = adapter(self.fake)
        self.ctx.world.customer_writes(sender="customer@example.com", subject="Refund please", body="Charged twice.")
        from sayso.jobs import JOBS
        self.jobs = JOBS
        JOBS["intake"](self.ctx)
        self.key = next(iter(cases.load(self.ctx)))

    def tearDown(self):
        self._tmp.cleanup()

    def approve(self, spec):
        from sayso import demo, votes
        from sayso.jobs import money_job
        v = money_job.request(self.ctx, case_key=self.key, subject="Refund please", spec=copy.deepcopy(spec))
        self.ctx.world.operator_votes(v["poll_id"], demo.OPERATOR, "yes")
        self.jobs["votes"](self.ctx)
        return v, votes

    def test_refund_once_and_verified(self):
        v, votes = self.approve({"action": "refund", "charge": "ls_order_1", "amount": 500,
                                 "currency": "usd", "customer": "ls_cust_1"})
        self.jobs["money"](self.ctx)
        self.jobs["money"](self.ctx)
        self.assertEqual(len(self.fake.writes()), 1)
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)

    def test_cancel_once_and_verified(self):
        v, votes = self.approve({"action": "cancel_subscription", "subscription": "ls_sub_1",
                                 "customer": "ls_cust_1"})
        self.jobs["money"](self.ctx)
        self.jobs["money"](self.ctx)
        self.assertEqual(len(self.fake.writes()), 1)
        self.assertEqual(votes.load(self.ctx)[v["key"]]["status"], votes.DONE)


if __name__ == "__main__":
    unittest.main()
