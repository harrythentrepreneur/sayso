# Customer lookup (read-only facts for drafts)

Drafts are better when they know the customer's plan, usage and sign-up date. With a customer lookup,
Sayso asks **your own app** for a few facts about the customer before it writes each draft.

You build one small HTTPS endpoint. Sayso calls it with the customer's email address and a signature.
Your endpoint answers with a flat JSON object. That is the whole contract.

## What Sayso does with the facts

- The drafter gets them as a labelled block of **untrusted data, not instructions**. Every line is
  quoted, so a value cannot end the block or give the drafter orders.
- They are never sent to the customer on their own. A reply goes out only after a person approves
  that exact draft. A fact reaches the customer only if the drafter wrote it into that draft.
- A failed lookup never blocks a draft. The draft is written without facts, and one alert is sent
  per outage (not one per draft). After the lookup works again, the next failure alerts again.
- The facts the drafter saw are kept with the case (`<state>/facts/<case>.json`), so a person can see
  what the draft was based on.

## The request

```
GET https://app.example.com/sayso/facts?email=pat%40customer.test
Accept: application/json
User-Agent: sayso/0.2
X-Sayso-Timestamp: 1760600000
X-Sayso-Signature: v1=5b0c...e4
```

- `email` is lower-case and trimmed.
- `X-Sayso-Timestamp` is Unix time in seconds.
- `X-Sayso-Signature` is `v1=` followed by the lower-case hex HMAC-SHA256 of the string
  `v1:<timestamp>:<email>`, keyed with your shared secret.

Your endpoint must:

1. Refuse a timestamp more than 300 seconds away from its own clock (replay protection).
2. Compute the signature itself and compare it in **constant time**.
3. Answer `401` on any mismatch. Answer `404` when it does not know the email.
4. Never redirect. Sayso refuses redirects.

## The answer

`200`, `Content-Type: application/json`, one flat object, at most 16 KB:

```json
{"plan": "Pro", "seats": 3, "signed_up": "2025-11-02", "trial": false, "mrr_usd": 49.0}
```

Sayso keeps only fields whose key matches `[A-Za-z0-9_.-]{1,64}` and whose value is a string,
number or true/false. It drops nested objects, lists, nulls and empty strings, removes line breaks,
cuts each string to 200 characters and keeps at most 30 fields. Send only what a support person
would want to see: never passwords, tokens, card data or internal notes.

## Limits

| Limit | Value |
|---|---|
| Timeout | 3 seconds, no retry (the draft goes ahead without facts) |
| Answer size | 16 KB |
| Calls | One per draft |
| HTTP 429 | Sayso stops calling until `Retry-After` (seconds, at most 300) has passed |
| Scheme | `https` only (`http` only for `localhost` while you develop) |

## Example endpoint: Python (standard library)

```python
import hashlib, hmac, json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

SECRET = os.environ["SAYSO_LOOKUP_SECRET"].encode()

def find_customer(email):            # your own database query
    return {"plan": "Pro", "seats": 3, "signed_up": "2025-11-02"} if email == "pat@customer.test" else None

class Facts(BaseHTTPRequestHandler):
    def do_GET(self):
        email = parse_qs(urlsplit(self.path).query).get("email", [""])[0].strip().lower()
        ts = self.headers.get("X-Sayso-Timestamp", "")
        sig = self.headers.get("X-Sayso-Signature", "")
        want = "v1=" + hmac.new(SECRET, f"v1:{ts}:{email}".encode(), hashlib.sha256).hexdigest()
        if not ts.isdigit() or abs(time.time() - int(ts)) > 300 or not hmac.compare_digest(want, sig):
            return self.answer(401, {"error": "bad signature"})
        found = find_customer(email)
        self.answer(200, found) if found else self.answer(404, {"error": "not found"})

    def answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

HTTPServer(("127.0.0.1", 8000), Facts).serve_forever()   # put it behind your HTTPS proxy
```

## Example endpoint: Node (no packages)

```js
const crypto = require("node:crypto");
const http = require("node:http");
const SECRET = process.env.SAYSO_LOOKUP_SECRET;

function findCustomer(email) {        // your own database query
  return email === "pat@customer.test" ? { plan: "Pro", seats: 3, signed_up: "2025-11-02" } : null;
}

http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const email = (url.searchParams.get("email") || "").trim().toLowerCase();
  const ts = req.headers["x-sayso-timestamp"] || "";
  const sig = Buffer.from(req.headers["x-sayso-signature"] || "");
  const want = Buffer.from("v1=" + crypto.createHmac("sha256", SECRET).update(`v1:${ts}:${email}`).digest("hex"));
  const fresh = /^\d+$/.test(ts) && Math.abs(Date.now() / 1000 - Number(ts)) <= 300;
  const send = (status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (!fresh || sig.length !== want.length || !crypto.timingSafeEqual(sig, want)) {
    return send(401, { error: "bad signature" });
  }
  const found = findCustomer(email);
  return found ? send(200, found) : send(404, { error: "not found" });
}).listen(8000, "127.0.0.1");          // put it behind your HTTPS proxy
```

## Self-hosted setup

In your settings file:

```toml
[customer_facts]
adapter = "http"
url = "https://app.example.com/sayso/facts"
secret_env = "ACME_LOOKUP_SECRET"
```

Make a long random secret (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), put it in
your secrets file as `ACME_LOOKUP_SECRET=...` and give the same value to your endpoint. Then try one
signed lookup by hand (read-only, nothing is stored):

```bash
set -a; . ~/.config/sayso/acme.env; set +a
python3 -m sayso.adapters.facts --config acme.toml --email you@example.com
```

It prints the block exactly as the drafter will see it. The `hermes` drafter adds the block to its prompt;
a custom drafter reads `case["customer_facts"]` (a dict, empty when there are no facts) and can use
`sayso.adapters.facts.prompt_block(...)` for the same safe wording.

## Sayso Cloud

In Sayso Cloud, the owner enters the URL under Connections > Customer lookup. Sayso makes the secret,
shows it once, and stores it encrypted. The URL must be public `https` on port 443; private, local and
cloud-metadata addresses are refused, both when saving and on every call.
