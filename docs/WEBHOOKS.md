# Outgoing webhooks

Sayso can tell another tool when something happens: a decision is made, a reply
is sent and verified, a refund or cancellation is verified with the payment
provider, or a case is closed. Point it at any HTTPS URL, for example a Zapier
"Catch Hook", and connect anything else from there.

Webhooks are **off by default** and never decide anything. They report what
already happened. Every customer reply and money action still needs a person's
Yes on the exact version.

## Events

| Event | Sent when | Main `data` fields |
|---|---|---|
| `vote.decided` | an operator approves or declines a vote | `vote_key`, `case_key`, `kind` (`reply`/`money`/`merge`/`close`), `decision`, `decided_by`, `via`, `stage`, `money` (money votes) |
| `reply.sent` | an approved reply is found in BOTH the helpdesk and the mailbox Sent folder | `vote_key`, `case_key`, `message_id`, `verified`, `approved_by`, `stage` |
| `money.verified` | the payment provider's own record matches the approved refund or cancellation | `vote_key`, `case_key`, `money` (`action`, `charge`/`subscription`, `amount`, `currency`), `provider_id`, `provider_status`, `approved_by` |
| `case.closed` | an approved close (quiet close or no-reply close) | `vote_key`, `case_key`, `how` (`quiet`/`no_reply`), `closed_by`, `stage` |
| `sayso.test` | a host's "Send test event" button | `message`, `sample` |

Amounts are in minor units (cents). By default an event carries **no customer
address, subject or email text**. Set `include_content = true` to add a
`content` object (customer address, case title, and for `reply.sent` the reply
text). Only turn it on for a receiver you trust with customer mail.

Example body:

```json
{
  "id": "evt_3f1c2b9a8e7d6c5b4a392817",
  "type": "money.verified",
  "created": "2030-01-06T09:00:00+00:00",
  "product": "acme",
  "data": {
    "vote_key": "money:billing-1a2b3c4d5e6f-1:refund:ch_123",
    "case_key": "billing-1a2b3c4d5e6f-1",
    "money": {"action": "refund", "charge": "ch_123", "amount": 1900, "currency": "usd"},
    "provider_id": "re_123",
    "provider_status": "succeeded",
    "approved_by": "Sam",
    "verified": true,
    "stage": "Awaiting approval"
  }
}
```

## Headers

- `X-Sayso-Signature: t=<unix seconds>,v1=<hex>`. `v1` is HMAC-SHA256 with your
  signing secret over `"<t>.<raw request body>"`. This is the same shape as
  Stripe's `Stripe-Signature`, so a Stripe-style verifier works unchanged.
- `X-Sayso-Delivery`: the event id. It is the same on every retry. Store the ids
  you have handled and skip repeats: a receiver can see an event more than once.
- `X-Sayso-Event`: the event type. `X-Sayso-Attempt`: 1, 2, ...

## Verify a request (receiver side)

```python
import hashlib, hmac, time

def verify(secret: str, header: str, raw_body: bytes, tolerance: int = 300) -> bool:
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    t = next((v for k, v in parts if k == "t"), "")
    sigs = [v for k, v in parts if k == "v1"]          # ignore any other scheme
    if not t.isdigit() or not sigs or abs(time.time() - int(t)) > tolerance:
        return False
    want = hmac.new(secret.encode(), t.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)
```

Use the raw body bytes, not re-serialised JSON. `sayso.adapters.events.verify`
does the same.

## Delivery

- Events are written to an outbox in the state dir (`events-outbox.json`) by the
  job that caused them, then sent by the `events` job (every 60 s by default,
  last in a `sayso tick`). Writing the outbox needs no network, and a failure
  there is journaled and alerted, never raised: **webhooks never block the loop.**
- `2xx` = delivered. `410 Gone` = the receiver asked to stop: the event is
  dropped. Redirects are **never followed** (a `3xx` is a failure). Any other
  `4xx` = refused, not retried.
- `408`, `425`, `429`, `5xx`, timeouts and network errors are retried after
  30 s, 2 min, 10 min, 30 min, 2 h, 6 h and 12 h (8 attempts, about 21 hours).
  A `Retry-After` header is honoured up to 6 hours. After the last attempt the
  event fails and one alert goes to the board.
- Each request times out after 10 s. A receiver that fails is tried once per run,
  so a dead receiver costs at most one timeout per pass.
- At most 1000 events wait at once. The newest 200 finished deliveries per
  endpoint are kept as the delivery log.

## Self-hosted setup

```toml
[product]
mode = "live"            # real adapters, including webhooks, need live mode

[events]
adapter = "webhook"
url = "https://hooks.zapier.com/hooks/catch/123456/abcde/"
secret_env = "SAYSO_WEBHOOK_SECRET"
events = ["vote.decided", "reply.sent", "money.verified", "case.closed"]   # optional
include_content = false                                                     # optional
```

Make a secret with `python3 -c "import secrets; print('whsec_' + secrets.token_hex(32))"`,
put it in the environment variable you named, and give the same value to the
receiver. Never put it in the settings file.

## Zapier (Webhooks by Zapier > Catch Hook)

1. In Zapier, create a Zap. Trigger: **Webhooks by Zapier**, event **Catch Hook**.
   Webhooks by Zapier is a premium app: it needs a paid Zapier plan (new accounts
   get a 14-day trial).
2. Copy the Catch Hook URL (`https://hooks.zapier.com/hooks/catch/<user>/<id>/`)
   into `url`. Zapier treats this URL as the secret: keep it private.
3. Run one pass that makes a decision (or Cloud's "Send test event"), then press
   **Test trigger** in Zapier. The fields appear as `data__case_key`,
   `data__money__amount` and so on.
4. Zapier answers every request with HTTP 200 and `{"status": "success", ...}`,
   even when no Zap is switched on. "Delivered" means Zapier received it, not
   that your Zap ran: check the Zap history for that.
5. Zapier does not check our signature. To check it, add a "Code by Zapier"
   step with the `verify` function above, using the **Catch Raw Hook** trigger
   (it keeps the raw body and headers).
