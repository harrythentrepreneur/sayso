"""Outgoing events: signed webhooks with a durable outbox (adapter kind ``events``).

One event stream (a vote decided, a reply sent and verified, a money action verified,
a case closed) lets a user connect any other tool, for example a Zapier "Catch Hook".

How it works:
  * The jobs call ``publish(ctx, event, payload, idempotency_key)``. It only WRITES the
    event to an outbox file in the state dir (no network). Any failure is journaled and
    alerted once; it never raises, so a webhook problem never blocks the loop.
  * The ``events`` job calls ``flush()``, which POSTs due rows as JSON, one request per
    (endpoint, event). It runs single-flight and sends at most ``limit`` rows per run.
  * The event id is derived from (product, event, idempotency key). A job that emits the
    same thing twice gets the same id, and the outbox keeps ONE row per (endpoint, id).
    The id goes out in the body and in ``X-Sayso-Delivery`` and never changes across
    retries, so a receiver dedupes on it.
  * Every request is signed: ``X-Sayso-Signature: t=<unix>,v1=<hex>``, where v1 is
    HMAC-SHA256(secret, "<t>.<raw body>") - the same shape as Stripe's signature header.
    Each attempt is signed again with a fresh ``t``.
  * 2xx = delivered. 410 = the receiver said it is gone: stop. 3xx is never followed
    (and counts as failed). 408/425/429/5xx and network errors are retried with backoff
    (Retry-After honoured, capped) up to MAX_ATTEMPTS, then the row fails with one alert.
    Any other 4xx fails at once: the receiver refused it and a repeat will not help.
  * Payloads carry ids, stage, kind, amounts and names. No customer address, subject or
    email body unless ``include_content = true``.

Self-hosted settings (secret read only from the environment variable it names):

    [events]
    adapter = "webhook"
    url = "https://hooks.zapier.com/hooks/catch/123456/abcde/"
    secret_env = "SAYSO_WEBHOOK_SECRET"
    events = ["vote.decided", "reply.sent", "money.verified", "case.closed"]   # optional
    include_content = false                                                     # optional
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sayso.config import ConfigError
from sayso.store import load_json, locked, parse_iso, save_json, single_flight

EVENTS = ("vote.decided", "reply.sent", "money.verified", "case.closed")
TEST_EVENT = "sayso.test"
SIGNATURE_HEADER = "X-Sayso-Signature"
DELIVERY_HEADER = "X-Sayso-Delivery"
EVENT_HEADER = "X-Sayso-Event"
TOLERANCE = 300                      # seconds a receiver should accept (Stripe's default)
TIMEOUT = 10                         # seconds per request
DELAYS = (30, 120, 600, 1800, 7200, 21600, 43200)   # wait after failed attempt n (1-based)
MAX_ATTEMPTS = len(DELAYS) + 1       # 8 attempts over about 21 hours
MAX_RETRY_AFTER = 21600              # a Retry-After is honoured up to 6 hours
MAX_PENDING = 1000                   # waiting rows across all endpoints; more is refused
KEEP_DONE = 200                      # finished rows kept per endpoint (the delivery log)
MAX_CONTENT = 64 * 1024              # characters of reply text in a content payload
RETRY_STATUSES = frozenset({408, 425, 429})
PENDING, DELIVERED, FAILED, GONE, CANCELED = "pending", "delivered", "failed", "gone", "canceled"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_OPTIONS = {"url", "secret_env", "events", "include_content"}

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class EventsError(RuntimeError):
    """An event could not be queued (bad name, outbox full). The loop goes on."""


# -- signing ---------------------------------------------------------------------------------
def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The ``X-Sayso-Signature`` value for one request."""
    if not secret:
        raise EventsError("a webhook secret is required")
    mac = hmac.new(secret.encode(), str(int(timestamp)).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={mac}"


def verify(secret: str, header: str, body: bytes, *, now: float, tolerance: int = TOLERANCE) -> bool:
    """Receiver-side check (also used by tests and docs): any v1 matches, and t is recent.
    Schemes other than v1 are ignored, so a downgrade cannot pass."""
    stamp, sigs = None, []
    for part in str(header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            stamp = int(value)
        elif key == "v1" and value:
            sigs.append(value)
    if stamp is None or not sigs or abs(now - stamp) > tolerance:
        return False
    want = sign(secret, stamp, body).split("v1=", 1)[1]
    return any(hmac.compare_digest(want, s) for s in sigs)


def event_id(product: str, event: str, idempotency_key: str) -> str:
    """Stable id: the same (product, event, key) always gives the same id."""
    digest = hashlib.sha256(f"{product}\x1f{event}\x1f{idempotency_key}".encode()).hexdigest()
    return "evt_" + digest[:24]


# -- transport -------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - a 3xx is a failure, never followed
        return None


def urllib_transport(method, url, headers, body):
    """POST with a timeout and NO redirects (a 3xx comes back as its own status)."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b""


# -- endpoints -------------------------------------------------------------------------------
def check_url(url: str) -> str:
    """https only, a host, no user:password@, no spaces. Hosts add their own SSRF check."""
    url = str(url or "").strip()
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname:
        raise ConfigError("[events] url must be an https:// address")
    if p.username or p.password or any(c in url for c in " \t\r\n\x00"):
        raise ConfigError("[events] url must not contain a user name, password or spaces")
    return url


def check_events(names) -> tuple[str, ...]:
    names = tuple(str(n) for n in (names if names is not None else EVENTS))
    unknown = [n for n in names if n not in EVENTS]
    if unknown or not names:
        raise ConfigError(f"[events] events must be a non-empty list from {EVENTS}; got {unknown or names}")
    return names


@dataclass(frozen=True)
class Endpoint:
    id: str
    url: str
    secret: str = field(repr=False)
    events: tuple[str, ...] = EVENTS

    def __post_init__(self):
        if not _ID.match(self.id):
            raise ConfigError("[events] endpoint id must be letters, digits, '-' or '_'")
        check_url(self.url)
        check_events(self.events)
        if not self.secret:
            raise ConfigError("[events] the endpoint has no signing secret")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# -- the adapter -----------------------------------------------------------------------------
class WebhookEvents:
    """Outbox + delivery for one product (or one Cloud workspace)."""

    def __init__(self, endpoints, outbox_path, *, product: str, clock, transport: Transport | None = None,
                 include_content: bool = False, journal=None, alert=None):
        self.endpoints = {ep.id: ep for ep in endpoints}
        self.path = Path(outbox_path)
        self.product = str(product)
        self.clock = clock
        self.transport = transport or urllib_transport
        self.include_content = bool(include_content)
        self.journal = journal
        self.alert = alert

    def bind(self, ctx) -> None:
        """Use the loop's clock, journal and alerts (runtime.build calls this)."""
        self.clock = ctx.clock
        self.journal = ctx.journal
        self.alert = ctx.alert

    # -- queue -------------------------------------------------------------------------
    def emit(self, event: str, payload: dict, idempotency_key: str, *, content: dict | None = None,
             only: str | None = None) -> str:
        """Queue ``event`` for every endpoint that wants it. No network. Returns the event id.
        The same (event, idempotency_key) is queued once per endpoint, however often it is emitted."""
        if event not in EVENTS and event != TEST_EVENT:
            raise EventsError(f"unknown event {event!r}")
        if not str(idempotency_key or "").strip():
            raise EventsError("an idempotency key is required")
        eid = event_id(self.product, event, idempotency_key)
        targets = [ep for ep in self.endpoints.values()
                   if (only is None or ep.id == only) and (event == TEST_EVENT or event in ep.events)]
        if not targets:
            return eid
        data = dict(payload or {})
        if self.include_content and content:
            data["content"] = content
        now = self.clock.now()
        body = json.dumps({"id": eid, "type": event, "created": _iso(now), "product": self.product,
                           "data": data}, sort_keys=True, default=str)
        with locked(self.path):
            box = load_json(self.path, {"rows": {}})
            rows = box.setdefault("rows", {})
            waiting = sum(1 for r in rows.values() if r["status"] == PENDING)
            for ep in targets:
                rid = f"{ep.id}:{eid}"
                if rid in rows:
                    continue
                if waiting >= MAX_PENDING:
                    save_json(self.path, box)
                    raise EventsError("the webhook outbox is full; events are not being delivered")
                rows[rid] = {"endpoint": ep.id, "id": eid, "event": event, "created": _iso(now), "body": body,
                             "status": PENDING, "attempts": 0, "next_at": _iso(now), "last_status": None,
                             "last_error": None, "last_at": None, "delivered_at": None}
                waiting += 1
            save_json(self.path, box)
        return eid

    # -- deliver ------------------------------------------------------------------------
    def flush(self, *, only: str | None = None, limit: int = 50) -> dict:
        """Deliver due rows. Single-flight; never raises for a delivery problem."""
        counts = {"delivered": 0, "retry": 0, "failed": 0, "gone": 0, "busy": False}
        with single_flight(self.path.with_name(self.path.name + ".flush")) as held:
            if not held:
                counts["busy"] = True
                return counts
            now = self.clock.now()
            with locked(self.path):
                rows = load_json(self.path, {"rows": {}}).get("rows", {})
            due = sorted((rid for rid, r in rows.items()
                          if r["status"] == PENDING and r["endpoint"] in self.endpoints
                          and (only is None or r["endpoint"] == only)
                          and parse_iso(r["next_at"]) <= now),
                         key=lambda rid: (rows[rid]["created"], rid))
            stalled: set[str] = set()
            for rid in due[:max(0, int(limit))]:
                row = rows[rid]
                if row["endpoint"] in stalled:
                    continue
                outcome, status, error, retry_after = self._send(self.endpoints[row["endpoint"]], row)
                result = self._record(rid, outcome, status, error, retry_after)
                counts[result] += 1
                if result == "retry":
                    stalled.add(row["endpoint"])   # one failure per endpoint per run: do not hammer it
            self._prune()
        return counts

    def _send(self, ep: Endpoint, row: dict):
        body = row["body"].encode()
        headers = {"Content-Type": "application/json", "User-Agent": "sayso-webhooks/1",
                   SIGNATURE_HEADER: sign(ep.secret, int(self.clock.now().timestamp()), body),
                   DELIVERY_HEADER: row["id"], EVENT_HEADER: row["event"],
                   "X-Sayso-Attempt": str(row["attempts"] + 1)}
        try:
            status, resp_headers, _ = self.transport("POST", ep.url, headers, body)
        except Exception as exc:  # noqa: BLE001 - network trouble: retry later, never raise into the loop
            return "retry", None, _error_name(exc), None
        status = int(status)
        if 200 <= status < 300:
            return "delivered", status, None, None
        if status == 410:
            return "gone", status, "gone", None
        if 300 <= status < 400:
            return "failed", status, "redirect", None
        if status in RETRY_STATUSES or status >= 500:
            return "retry", status, "http", _retry_after(resp_headers)
        return "failed", status, "refused", None

    def _record(self, rid: str, outcome: str, status, error, retry_after) -> str:
        now = self.clock.now()
        with locked(self.path):
            box = load_json(self.path, {"rows": {}})
            row = box["rows"][rid]
            row["attempts"] += 1
            row.update(last_status=status, last_error=error, last_at=_iso(now))
            if outcome == "retry" and row["attempts"] >= MAX_ATTEMPTS:
                outcome = "failed"
            if outcome == "delivered":
                row.update(status=DELIVERED, delivered_at=_iso(now), next_at=None)
            elif outcome == "retry":
                wait = DELAYS[row["attempts"] - 1]
                if retry_after is not None:
                    wait = min(max(wait, retry_after), MAX_RETRY_AFTER)
                row["next_at"] = _iso(now + timedelta(seconds=wait))
            else:
                row.update(status=GONE if outcome == "gone" else FAILED, next_at=None)
            save_json(self.path, box)
        if outcome in ("failed", "gone"):
            self._journal("event_undelivered", endpoint=row["endpoint"], type=row["event"], id=row["id"],
                          status=status, error=error, attempts=row["attempts"])
            if self.alert:
                why = "the receiver answered 410 Gone" if outcome == "gone" else \
                    f"gave up after {row['attempts']} attempt(s) (last: {status or error})"
                try:
                    self.alert(f"webhook-failed:{row['endpoint']}:{row['id']}",
                               f"webhook {row['event']} {row['id']} to endpoint {row['endpoint']} not delivered: {why}")
                except Exception:  # noqa: BLE001 - an alert problem must not stop delivery
                    pass
        return outcome

    def _prune(self) -> None:
        with locked(self.path):
            box = load_json(self.path, {"rows": {}})
            rows = box.get("rows", {})
            done: dict[str, list[str]] = {}
            for rid, r in rows.items():
                if r["status"] != PENDING:
                    done.setdefault(r["endpoint"], []).append(rid)
            dropped = 0
            for ids in done.values():
                ids.sort(key=lambda rid: (rows[rid]["created"], rid))
                for rid in ids[:-KEEP_DONE] if len(ids) > KEEP_DONE else []:
                    del rows[rid]
                    dropped += 1
            if dropped:
                save_json(self.path, box)

    # -- host helpers ---------------------------------------------------------------------
    def cancel(self, endpoint_id: str) -> int:
        """Stop every waiting row for an endpoint (disabled or deleted). Returns how many."""
        n = 0
        with locked(self.path):
            box = load_json(self.path, {"rows": {}})
            for r in box.get("rows", {}).values():
                if r["endpoint"] == endpoint_id and r["status"] == PENDING:
                    r.update(status=CANCELED, next_at=None)
                    n += 1
            if n:
                save_json(self.path, box)
        return n

    def log(self, endpoint_id: str | None = None, limit: int = 50) -> list[dict]:
        """Newest first, without bodies: id, event, status, last HTTP status, attempts, times."""
        rows = load_json(self.path, {"rows": {}}).get("rows", {})
        keep = ("endpoint", "id", "event", "created", "status", "attempts", "last_status", "last_error",
                "last_at", "next_at", "delivered_at")
        out = [{k: r.get(k) for k in keep} for r in rows.values()
               if endpoint_id is None or r["endpoint"] == endpoint_id]
        out.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
        return out[:max(0, int(limit))]

    def _journal(self, name: str, **fields) -> None:
        if self.journal is not None:
            try:
                self.journal.append(name, **fields)
            except Exception:  # noqa: BLE001
                pass


def log(outbox_path, endpoint_id: str | None = None, limit: int = 50) -> list[dict]:
    """Read the delivery log without building an adapter (no secrets needed)."""
    return WebhookEvents([], outbox_path, product="", clock=None).log(endpoint_id, limit)


def _error_name(exc: BaseException) -> str:
    text = type(exc).__name__.lower()
    if "timeout" in text or isinstance(exc, TimeoutError):
        return "timeout"
    return "network"


def _retry_after(headers) -> int | None:
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            v = str(v).strip()
            return int(v) if v.isdigit() else None
    return None


# -- loop wiring -----------------------------------------------------------------------------
def outbox_path(ctx) -> Path:
    return ctx.paths.root / "events-outbox.json"


def publish(ctx, event: str, payload: dict, idempotency_key: str, *, card_id: str | None = None,
            content: Callable[[], dict] | None = None) -> str | None:
    """Called by the jobs. A no-op when events are off. NEVER raises: delivery problems
    are journaled and alerted, and the job that emitted carries on."""
    adapter = getattr(ctx, "events", None)
    if adapter is None:
        return None
    try:
        data = dict(payload)
        if card_id:
            try:
                from sayso import stages
                data["stage"] = stages.stage_of(ctx.board.get_tags(card_id))
            except Exception:  # noqa: BLE001 - an unreadable stage is left out, never guessed
                data["stage"] = None
        extra = content() if (content and getattr(adapter, "include_content", False)) else None
        eid = adapter.emit(event, data, idempotency_key, content=extra)
        ctx.journal.append("event_queued", type=event, id=eid)
        return eid
    except Exception as exc:  # noqa: BLE001 - a webhook must never block the loop
        try:
            ctx.journal.append("event_error", type=event, error=type(exc).__name__)
            ctx.alert(f"events:{event}:{type(exc).__name__}", f"an outgoing {event} event was not queued: "
                      f"{exc if isinstance(exc, EventsError) else type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass
        return None


def from_section(section, config, *, transport: Transport | None = None, clock=None, journal=None,
                 alert=None) -> WebhookEvents:
    """Build the self-hosted adapter from ``[events] adapter = "webhook"``."""
    unknown = set(section.options) - _OPTIONS
    if unknown:
        raise ConfigError(f"[events] unknown keys: {sorted(unknown)}")
    include = section.options.get("include_content", False)
    if not isinstance(include, bool):
        raise ConfigError("[events] include_content must be true or false")
    ep = Endpoint("default", check_url(section.options.get("url", "")), section.secret("secret"),
                  check_events(section.options.get("events")))
    from sayso.runtime import Clock
    return WebhookEvents([ep], Path(config.state_dir) / "events-outbox.json", product=config.slug,
                         clock=clock or Clock(), transport=transport, include_content=include,
                         journal=journal, alert=alert)


def vote_payload(rec: dict, status: str, by, via: str) -> dict:
    """vote.decided: ids, kind, outcome, who, and money amounts. No customer text."""
    out = {"vote_key": rec["key"], "case_key": rec["case_key"], "kind": rec["kind"],
           "decision": "approved" if status == "approved" else "declined", "decided_by": by, "via": via}
    if rec["kind"] == "money":
        out["money"] = _money_fields(rec.get("spec") or {})
    return out


def money_payload(rec: dict, spec: dict, receipt: dict) -> dict:
    """money.verified: the provider's own receipt (ids and amounts only)."""
    return {"vote_key": rec["key"], "case_key": rec["case_key"], "approved_by": rec.get("decided_by"),
            "money": _money_fields(spec), "provider_id": receipt.get("provider_id"),
            "provider_status": receipt.get("status") or "succeeded", "verified": True}


def _money_fields(spec: dict) -> dict:
    keep = ("action", "charge", "subscription", "amount", "currency")
    return {k: spec[k] for k in keep if k in spec}


def content_for_case(ctx, case_key: str, **more) -> dict:
    """Customer-identifying content, only sent when include_content is on."""
    from sayso import cases
    rec = cases.load(ctx).get(case_key) or {}
    out = {"customer": rec.get("customer"), "title": rec.get("title")}
    for k, v in more.items():
        out[k] = v[:MAX_CONTENT] if isinstance(v, str) else v
    return out


def emit_test(adapter: WebhookEvents, endpoint_id: str, nonce: str) -> str:
    """Queue one ``sayso.test`` event for ONE endpoint (a host's "Send test event" button)."""
    sample = {"message": "Test event from Sayso. No customer data.", "sample": True}
    return adapter.emit(TEST_EVENT, sample, f"test:{endpoint_id}:{nonce}", only=endpoint_id)
