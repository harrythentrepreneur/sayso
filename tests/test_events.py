"""Outgoing webhooks (sayso.adapters.events) against a RECORDING transport. No network, no accounts.

The receiver fixture is Zapier's own documented Catch Hook answer
(help.zapier.com/hc/en-us/articles/8496215655437): a 200 with
{"attempt":..., "id":..., "request_id":..., "status":"success"}.
A socket guard fails any connection that is not to localhost.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sayso import config as config_mod
from sayso import demo, runtime, votes
from sayso.adapters import events
from sayso.config import ConfigError, Section
from tests.helpers import LoopTest

ZAPIER_OK = json.dumps({"attempt": "an_id_value", "id": "another_id_value", "request_id": "another_id_value",
                        "status": "success"}).encode()
SECRET = "whsec_" + "ab" * 32
URL = "https://hooks.zapier.com/hooks/catch/123456/abcde/"
T0 = datetime(2030, 1, 6, 9, 0, tzinfo=timezone.utc)

# -- socket guard: any connection beyond localhost fails the test ------------------------------
_real_connect = socket.socket.connect
_real_create = socket.create_connection
_LOCAL = ("127.0.0.1", "::1", "localhost")


class OutsideConnection(AssertionError):
    pass


def _host_of(address):
    return address[0] if isinstance(address, tuple) else address


def _guard_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and _host_of(address) not in _LOCAL:
        raise OutsideConnection(f"test tried to reach {address!r}")
    return _real_connect(self, address)


def _guard_create(address, *a, **k):
    if _host_of(address) not in _LOCAL:
        raise OutsideConnection(f"test tried to reach {address!r}")
    return _real_create(address, *a, **k)


def setUpModule():
    socket.socket.connect = _guard_connect
    socket.create_connection = _guard_create


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


class Recorder:
    """A transport that records each request and answers from a script of (status, headers, body)."""

    def __init__(self, *answers, default=(200, {}, ZAPIER_OK)):
        self.answers = list(answers)
        self.default = default
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Box(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-events-")
        self.dir = Path(self._tmp.name)
        self.clock = runtime.FixedClock(T0)
        self.alerts = []

    def tearDown(self):
        self._tmp.cleanup()

    def adapter(self, transport, *, events_=events.EVENTS, include_content=False, endpoints=None):
        eps = endpoints or [events.Endpoint("ep1", URL, SECRET, tuple(events_))]
        return events.WebhookEvents(eps, self.dir / "outbox.json", product="demo", clock=self.clock,
                                    transport=transport, include_content=include_content,
                                    alert=lambda key, text: self.alerts.append((key, text)))

    def rows(self):
        return json.loads((self.dir / "outbox.json").read_text())["rows"]


class GuardTests(unittest.TestCase):
    def test_socket_guard_bites_on_an_outside_call(self):
        """Seeded outside call: proves the guard is live for this module."""
        with self.assertRaises(OutsideConnection):
            socket.create_connection(("93.184.216.34", 443), timeout=1)
        s = socket.socket()
        try:
            with self.assertRaises(OutsideConnection):
                s.connect(("hooks.zapier.com", 443))
        finally:
            s.close()


class SigningTests(unittest.TestCase):
    def test_signature_is_stripe_shaped_hmac_of_t_dot_body(self):
        body = b'{"id":"evt_1"}'
        header = events.sign(SECRET, 1492774577, body)
        want = hmac.new(SECRET.encode(), b"1492774577." + body, hashlib.sha256).hexdigest()
        self.assertEqual(header, f"t=1492774577,v1={want}")

    def test_verify_accepts_good_and_refuses_tamper_old_and_downgrade(self):
        body = b'{"id":"evt_1"}'
        header = events.sign(SECRET, 1000, body)
        self.assertTrue(events.verify(SECRET, header, body, now=1000 + 299))
        self.assertFalse(events.verify(SECRET, header, body + b" ", now=1000))
        self.assertFalse(events.verify("whsec_other", header, body, now=1000))
        self.assertFalse(events.verify(SECRET, header, body, now=1000 + 301))
        self.assertFalse(events.verify(SECRET, header.replace("v1=", "v0="), body, now=1000))
        self.assertFalse(events.verify(SECRET, "", body, now=1000))
        # several v1 values (secret roll): any match passes
        self.assertTrue(events.verify(SECRET, f"t=1000,v1={'0' * 64},{header.split(',')[1]}", body, now=1000))

    def test_event_id_is_stable_and_scoped(self):
        a = events.event_id("demo", "reply.sent", "sent:reply:x")
        self.assertEqual(a, events.event_id("demo", "reply.sent", "sent:reply:x"))
        self.assertNotEqual(a, events.event_id("other", "reply.sent", "sent:reply:x"))
        self.assertNotEqual(a, events.event_id("demo", "case.closed", "sent:reply:x"))
        self.assertRegex(a, r"^evt_[0-9a-f]{24}$")

    def test_empty_secret_refused(self):
        with self.assertRaises(events.EventsError):
            events.sign("", 1, b"")


class EndpointTests(unittest.TestCase):
    def test_refuses_http_userinfo_spaces_and_unknown_events(self):
        for url in ("http://hooks.zapier.com/x", "ftp://x", "https://", "https://u:p@hooks.zapier.com/x",
                    "https://hooks.zapier.com/a b", ""):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                events.Endpoint("ep1", url, SECRET)
        with self.assertRaises(ConfigError):
            events.Endpoint("ep1", URL, SECRET, ("payment.made",))
        with self.assertRaises(ConfigError):
            events.Endpoint("ep1", URL, SECRET, ())
        with self.assertRaises(ConfigError):
            events.Endpoint("ep1", URL, "")
        with self.assertRaises(ConfigError):
            events.Endpoint("../x", URL, SECRET)

    def test_secret_not_in_repr(self):
        self.assertNotIn(SECRET, repr(events.Endpoint("ep1", URL, SECRET)))


class EmitTests(Box):
    def test_emit_queues_one_row_and_never_touches_the_network(self):
        rec = Recorder()
        a = self.adapter(rec)
        eid = a.emit("reply.sent", {"case_key": "c1"}, "sent:k1")
        self.assertEqual(rec.calls, [])
        rows = self.rows()
        self.assertEqual(list(rows), [f"ep1:{eid}"])
        self.assertEqual(rows[f"ep1:{eid}"]["status"], "pending")

    def test_same_key_twice_is_one_delivery(self):
        rec = Recorder()
        a = self.adapter(rec)
        e1 = a.emit("reply.sent", {"case_key": "c1"}, "sent:k1")
        a.flush()
        e2 = a.emit("reply.sent", {"case_key": "c1", "changed": True}, "sent:k1")
        a.flush()
        self.assertEqual(e1, e2)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_unknown_event_and_empty_key_refused(self):
        a = self.adapter(Recorder())
        with self.assertRaises(events.EventsError):
            a.emit("payment.made", {}, "k")
        with self.assertRaises(events.EventsError):
            a.emit("reply.sent", {}, "  ")

    def test_endpoint_gets_only_events_it_picked(self):
        rec = Recorder()
        a = self.adapter(rec, events_=("case.closed",))
        a.emit("reply.sent", {}, "k1")
        a.emit("case.closed", {}, "k2")
        a.flush()
        self.assertEqual([c["headers"][events.EVENT_HEADER] for c in rec.calls], ["case.closed"])

    def test_content_only_with_the_option(self):
        for include in (False, True):
            with self.subTest(include=include):
                a = events.WebhookEvents([events.Endpoint("ep1", URL, SECRET)], self.dir / f"o{include}.json",
                                         product="demo", clock=self.clock, transport=Recorder(),
                                         include_content=include)
                a.emit("reply.sent", {"case_key": "c1"}, "k", content={"body": "Hi Pat, refunded."})
                body = json.loads(next(iter(json.loads((self.dir / f"o{include}.json").read_text())["rows"]
                                            .values()))["body"])
                self.assertEqual("content" in body["data"], include)

    def test_outbox_is_bounded(self):
        a = self.adapter(Recorder())
        with mock.patch.object(events, "MAX_PENDING", 3):
            for i in range(3):
                a.emit("reply.sent", {}, f"k{i}")
            with self.assertRaises(events.EventsError):
                a.emit("reply.sent", {}, "k-over")
        self.assertEqual(len(self.rows()), 3)


class DeliveryTests(Box):
    def test_delivered_request_is_signed_json_with_stable_delivery_id(self):
        rec = Recorder()
        a = self.adapter(rec)
        eid = a.emit("vote.decided", {"vote_key": "v1", "decision": "approved"}, "decided:v1")
        self.assertEqual(a.flush()["delivered"], 1)
        call = rec.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", URL))
        h = call["headers"]
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h[events.DELIVERY_HEADER], eid)
        self.assertEqual(h[events.EVENT_HEADER], "vote.decided")
        self.assertTrue(events.verify(SECRET, h[events.SIGNATURE_HEADER], call["body"], now=T0.timestamp()))
        body = json.loads(call["body"])
        self.assertEqual((body["id"], body["type"], body["product"]), (eid, "vote.decided", "demo"))
        row = self.rows()[f"ep1:{eid}"]
        self.assertEqual((row["status"], row["last_status"], row["attempts"]), ("delivered", 200, 1))

    def test_retry_is_resigned_with_a_fresh_timestamp_and_same_id(self):
        rec = Recorder((503, {}, b""), (200, {}, ZAPIER_OK))
        a = self.adapter(rec)
        eid = a.emit("case.closed", {}, "closed:1")
        self.assertEqual(a.flush()["retry"], 1)
        self.assertEqual(a.flush()["delivered"], 0, "not due yet")
        self.clock.advance(seconds=events.DELAYS[0])
        self.assertEqual(a.flush()["delivered"], 1)
        s1, s2 = (c["headers"][events.SIGNATURE_HEADER] for c in rec.calls)
        self.assertNotEqual(s1.split(",")[0], s2.split(",")[0])
        self.assertEqual({c["headers"][events.DELIVERY_HEADER] for c in rec.calls}, {eid})
        self.assertEqual(rec.calls[0]["body"], rec.calls[1]["body"])
        self.assertEqual([c["headers"]["X-Sayso-Attempt"] for c in rec.calls], ["1", "2"])

    def test_status_classes(self):
        cases = [((200, {}, b""), "delivered"), ((204, {}, b""), "delivered"), ((410, {}, b""), "gone"),
                 ((302, {"Location": "http://169.254.169.254/"}, b""), "failed"), ((400, {}, b""), "failed"),
                 ((404, {}, b""), "failed"), ((500, {}, b""), "pending"), ((502, {}, b""), "pending"),
                 ((429, {}, b""), "pending"), ((408, {}, b""), "pending"),
                 (ConnectionResetError("reset"), "pending"), (TimeoutError("slow"), "pending")]
        for i, (answer, want) in enumerate(cases):
            with self.subTest(answer=answer):
                a = events.WebhookEvents([events.Endpoint("ep1", URL, SECRET)], self.dir / f"s{i}.json",
                                         product="demo", clock=self.clock, transport=Recorder(answer))
                eid = a.emit("reply.sent", {}, "k")
                a.flush()
                row = json.loads((self.dir / f"s{i}.json").read_text())["rows"][f"ep1:{eid}"]
                self.assertEqual(row["status"], want)

    def test_redirect_never_followed(self):
        rec = Recorder((307, {"Location": "https://internal.example/"}, b""))
        a = self.adapter(rec)
        a.emit("reply.sent", {}, "k")
        a.flush()
        self.clock.advance(days=2)
        a.flush()
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["url"], URL)

    def test_retry_after_honoured_and_capped(self):
        a = self.adapter(Recorder((429, {"Retry-After": "900"}, b"")))
        eid = a.emit("reply.sent", {}, "k")
        a.flush()
        self.assertEqual(self.rows()[f"ep1:{eid}"]["next_at"], (T0 + timedelta(seconds=900)).isoformat())
        b = events.WebhookEvents([events.Endpoint("ep1", URL, SECRET)], self.dir / "cap.json", product="demo",
                                 clock=self.clock, transport=Recorder((503, {"retry-after": "999999"}, b"")))
        eid = b.emit("reply.sent", {}, "k")
        b.flush()
        row = json.loads((self.dir / "cap.json").read_text())["rows"][f"ep1:{eid}"]
        self.assertEqual(row["next_at"], (T0 + timedelta(seconds=events.MAX_RETRY_AFTER)).isoformat())

    def test_bounded_attempts_then_one_alert(self):
        rec = Recorder(default=(500, {}, b""))
        a = self.adapter(rec)
        eid = a.emit("money.verified", {}, "money-done:1")
        for _ in range(events.MAX_ATTEMPTS + 3):
            a.flush()
            self.clock.advance(days=1)
        self.assertEqual(len(rec.calls), events.MAX_ATTEMPTS)
        row = self.rows()[f"ep1:{eid}"]
        self.assertEqual((row["status"], row["attempts"]), ("failed", events.MAX_ATTEMPTS))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("not delivered", self.alerts[0][1])
        self.assertNotIn(SECRET, self.alerts[0][1])

    def test_backoff_grows(self):
        a = self.adapter(Recorder(default=(500, {}, b"")))
        eid = a.emit("reply.sent", {}, "k")
        waits = []
        for _ in range(4):
            a.flush()
            row = self.rows()[f"ep1:{eid}"]
            nxt = datetime.fromisoformat(row["next_at"])
            waits.append((nxt - self.clock.now()).total_seconds())
            self.clock._now = nxt
        self.assertEqual(waits, list(events.DELAYS[:4]))

    def test_one_failure_stops_that_endpoint_for_this_run_only(self):
        rec = Recorder(ConnectionRefusedError("down"))
        good = events.Endpoint("ep2", "https://example.com/hook", SECRET)
        a = self.adapter(rec, endpoints=[events.Endpoint("ep1", URL, SECRET), good])
        for i in range(3):
            a.emit("reply.sent", {}, f"k{i}")
        out = a.flush()
        hit = [c["url"] for c in rec.calls]
        self.assertEqual(hit.count(URL), 1, "a dead receiver is tried once per run, not once per row")
        self.assertEqual(hit.count("https://example.com/hook"), 3)
        self.assertEqual((out["retry"], out["delivered"]), (1, 3))

    def test_flush_is_single_flight(self):
        rec = Recorder()
        a = self.adapter(rec)
        a.emit("reply.sent", {}, "k")
        from sayso.store import single_flight
        with single_flight(self.dir / "outbox.json.flush") as held:
            self.assertTrue(held)
            self.assertTrue(a.flush()["busy"])
        self.assertEqual(rec.calls, [])

    def test_limit_per_run(self):
        rec = Recorder()
        a = self.adapter(rec)
        for i in range(5):
            a.emit("reply.sent", {}, f"k{i}")
        a.flush(limit=2)
        self.assertEqual(len(rec.calls), 2)

    def test_cancel_and_log_have_no_bodies(self):
        a = self.adapter(Recorder((200, {}, b""), (500, {}, b"")))
        a.emit("reply.sent", {"case_key": "c1"}, "k1")
        self.clock.advance(seconds=1)
        a.emit("case.closed", {"case_key": "c1"}, "k2")
        a.flush()
        log = a.log("ep1")
        self.assertEqual([r["event"] for r in log], ["case.closed", "reply.sent"])
        self.assertEqual([r["status"] for r in log], ["pending", "delivered"])
        self.assertTrue(all("body" not in r for r in log))
        self.assertEqual(a.cancel("ep1"), 1)
        self.assertEqual(events.log(self.dir / "outbox.json", "ep1")[0]["status"], "canceled")

    def test_finished_rows_pruned(self):
        a = self.adapter(Recorder())
        with mock.patch.object(events, "KEEP_DONE", 3):
            for i in range(6):
                a.emit("reply.sent", {}, f"k{i}")
                self.clock.advance(seconds=1)
            a.flush()
        self.assertEqual(len(self.rows()), 3)

    def test_unknown_endpoint_rows_are_left_alone(self):
        a = self.adapter(Recorder())
        a.emit("reply.sent", {}, "k")
        b = events.WebhookEvents([], self.dir / "outbox.json", product="demo", clock=self.clock,
                                 transport=Recorder())
        self.assertEqual(b.flush()["delivered"], 0)


class UrllibTransportTests(unittest.TestCase):
    """The default transport against a localhost server: redirects come back, never followed."""

    def test_redirect_is_returned_not_followed(self):
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/hook":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            status, _, _ = events.urllib_transport("POST", f"http://127.0.0.1:{srv.server_address[1]}/hook",
                                                   {"Content-Type": "application/json"}, b"{}")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(status, 302)
        self.assertEqual(hits, ["/hook"])


class ConfigTests(unittest.TestCase):
    def base(self, events_table):
        raw = {"product": {"slug": "demo", "support_address": "s@example.com", "mode": "live"},
               "operators": {"1": "One"}, "state": {"dir": "/tmp/sayso-events-cfg"},
               "board": {"adapter": "fake"}, "helpdesk": {"adapter": "fake"}, "mailbox": {"adapter": "fake"},
               "drafter": {"adapter": "fake"}}
        if events_table is not None:
            raw["events"] = events_table
        return raw

    def test_default_is_none(self):
        cfg = config_mod.parse(self.base(None), Path("/tmp/x.toml"))
        self.assertEqual(cfg.section("events").adapter, "none")
        self.assertIn("events", config_mod.DEFAULT_JOBS)

    def test_webhook_needs_live_mode(self):
        raw = self.base({"adapter": "webhook", "url": URL, "secret_env": "SAYSO_WEBHOOK_SECRET"})
        raw["product"]["mode"] = "dry-run"
        with self.assertRaises(ConfigError):
            config_mod.parse(raw, Path("/tmp/x.toml"))

    def test_secret_must_be_an_env_name(self):
        with self.assertRaises(ConfigError):
            config_mod.parse(self.base({"adapter": "webhook", "url": URL, "secret_env": "whsec_abc"}),
                             Path("/tmp/x.toml"))

    @mock.patch.dict(os.environ, {"SAYSO_WEBHOOK_SECRET": SECRET})
    def test_from_section_reads_env_and_refuses_unknown(self):
        cfg = config_mod.parse(self.base({"adapter": "webhook", "url": URL, "secret_env": "SAYSO_WEBHOOK_SECRET",
                                          "events": ["reply.sent"]}), Path("/tmp/x.toml"))
        from sayso.adapters import reference
        a = reference.build("events", "webhook", cfg.section("events"), cfg)
        self.assertIsInstance(a, events.WebhookEvents)
        self.assertEqual(a.endpoints["default"].events, ("reply.sent",))
        for bad in ({"url": URL, "secret_env": "SAYSO_WEBHOOK_SECRET", "retries": 3},
                    {"url": URL, "secret_env": "SAYSO_WEBHOOK_SECRET", "include_content": "yes"},
                    {"url": "http://x.example/", "secret_env": "SAYSO_WEBHOOK_SECRET"},
                    {"url": URL, "secret_env": "SAYSO_WEBHOOK_SECRET", "events": ["nope"]}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                events.from_section(Section("webhook", bad), cfg)

    def test_missing_secret_env_refused(self):
        cfg = config_mod.parse(self.base({"adapter": "webhook", "url": URL, "secret_env": "SAYSO_NOPE_SECRET"}),
                               Path("/tmp/x.toml"))
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
            events.from_section(cfg.section("events"), cfg)


class LoopEventTests(LoopTest):
    """The real jobs emit at: vote decided, reply sent (after read-back), money verified, case closed."""

    def attach(self, transport, include_content=False):
        a = events.WebhookEvents([events.Endpoint("ep1", URL, SECRET)], self.ctx.paths.root / "events-outbox.json",
                                 product="demo", clock=self.ctx.clock, transport=transport,
                                 include_content=include_content)
        a.bind(self.ctx)
        self.ctx.events = a
        return a

    def bodies(self, rec):
        return [json.loads(c["body"]) for c in rec.calls]

    def test_full_scenario_emits_each_event_once_without_customer_text(self):
        rec = Recorder()
        self.attach(rec)
        demo.scenario(self.ctx, say=lambda *a: None)
        self.tick()
        self.tick()
        got = self.bodies(rec)
        types = [b["type"] for b in got]
        self.assertEqual(types.count("reply.sent"), 1)
        self.assertEqual(types.count("money.verified"), 1)
        self.assertEqual(types.count("case.closed"), 1)
        decided = [b for b in got if b["type"] == "vote.decided"]
        self.assertEqual(sorted(b["data"]["kind"] for b in decided), ["close", "merge", "money", "reply"])
        self.assertEqual(len({b["id"] for b in got}), len(got), "every delivery id is unique")
        money_ev = next(b for b in got if b["type"] == "money.verified")["data"]
        self.assertEqual((money_ev["money"]["amount"], money_ev["money"]["currency"]), (1900, "usd"))
        self.assertTrue(money_ev["verified"])
        sent = next(b for b in got if b["type"] == "reply.sent")["data"]
        self.assertTrue(sent["verified"])
        self.assertTrue(sent["message_id"])
        self.assertIn("stage", sent)
        raw = b"".join(c["body"] for c in rec.calls).lower()
        for private in (b"pat@customer.test", b"export button", b"hi pat", b"refunded the duplicate"):
            self.assertNotIn(private, raw, private)
        for c in rec.calls:   # each signed at its own send time
            stamp = int(c["headers"][events.SIGNATURE_HEADER].split(",")[0][2:])
            self.assertTrue(events.verify(SECRET, c["headers"][events.SIGNATURE_HEADER], c["body"], now=stamp))

    def test_reply_sent_is_emitted_only_after_the_read_back(self):
        rec = Recorder()
        self.attach(rec)
        self.write_in()
        self.tick()
        self.vote("reply")
        self.w.data["fail_readback"] = True
        self.w.save()
        with mock.patch("sayso.jobs.sender.check_receipt", return_value=None):
            self.tick()
        self.assertNotIn("reply.sent", [b["type"] for b in self.bodies(rec)])
        self.assertIn("vote.decided", [b["type"] for b in self.bodies(rec)])

    def test_include_content_adds_the_reply_text(self):
        rec = Recorder()
        self.attach(rec, include_content=True)
        self.write_in()
        self.tick()
        self.vote("reply")
        self.tick()
        sent = next(b for b in self.bodies(rec) if b["type"] == "reply.sent")["data"]
        self.assertEqual(sent["content"]["recipient"], "pat@customer.test")
        self.assertIn("body", sent["content"])

    def test_dead_receiver_never_blocks_the_loop(self):
        rec = Recorder(default=ConnectionRefusedError("down"))
        self.attach(rec)
        self.write_in()
        self.tick()
        self.vote("reply")
        out = self.tick()
        self.assertEqual(list(out["sender"].values()), ["SENT_AND_VERIFIED"])
        self.assertEqual(out["events"]["retry"], 1)

    def test_broken_adapter_never_blocks_a_job(self):
        class Broken:
            include_content = False

            def emit(self, *a, **k):
                raise RuntimeError("disk on fire")

            def flush(self):
                return {}
        self.ctx.events = Broken()
        self.write_in()
        self.tick()
        self.vote("reply")
        out = self.tick()
        self.assertEqual(list(out["sender"].values()), ["SENT_AND_VERIFIED"])
        journal = (self.ctx.paths.journal).read_text()
        self.assertIn('"event_error"', journal)

    def test_rerun_does_not_emit_again(self):
        rec = Recorder()
        self.attach(rec)
        self.write_in()
        self.tick()
        self.vote("reply")
        self.tick()
        n = len(rec.calls)
        key = next(k for k, v in votes.load(self.ctx).items() if v["kind"] == "reply")
        # a replayed emission (e.g. a crash between the post and the save) is the same delivery
        events.publish(self.ctx, "reply.sent", {"x": 1}, f"sent:{key}")
        self.tick()
        self.assertEqual(len(rec.calls), n)

    def test_events_off_by_default_and_job_beats_off(self):
        self.assertIsNone(self.ctx.events)
        self.assertEqual(self.tick("events")["events"], {"off": True})
        self.assertIsNone(events.publish(self.ctx, "reply.sent", {}, "k"))

    def test_paused_loop_sends_nothing(self):
        rec = Recorder()
        a = self.attach(rec)
        a.emit("case.closed", {}, "k")
        self.ctx.paths.pause_flag.write_text("x")
        with self.assertRaises(runtime.Paused):
            self.tick("events")
        self.assertEqual(rec.calls, [])


if __name__ == "__main__":
    unittest.main()
