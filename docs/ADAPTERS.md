# Writing an adapter

An adapter is one class that satisfies a Protocol in `sayso/adapters/base.py`.
The core never imports a vendor SDK.

## The contracts

| Protocol | Methods | Notes |
|---|---|---|
| Board | `ensure_card, get_tags, set_tags, post, messages, open_poll, read_poll, alert, notify, last_activity`; optional `message_authors` | `set_tags` receives the full list. `read_poll` returns `{user_id: "yes"/"no"}` and must skip bots. `notify(text, key, mention)` pings `mention` and nobody else. `message_authors(card)` returns `[(user_id, iso_time)]` oldest first, bots skipped; without it every vote is for anyone. `last_activity(card)` returns the ISO time of the card's NEWEST message (not its creation; a tag change is not activity) and raises when unreadable. |
| Helpdesk | `fetch_inbound, send_reply, sent_readback, last_sent_at, last_inbound_at` | `last_inbound_at(ticket, customer)` must include mail from that customer on ANY ticket. |
| Mailbox | `sent_copies(message_id)` | Counts copies in the mailbox's own Sent folder. It must be independent of the helpdesk. |
| Payments | `refund, cancel_subscription, read_refunds, read_subscription` | Reads return the provider's own records. A refund the provider has made but not yet approved reads back as status `pending` (the money job then waits, re-reads, and never re-executes); only `succeeded` is done. `cancel_subscription` may take `effective` (`"next_billing_period"` or `"immediately"`) when the spec has one; a cancel that is scheduled reads back as `cancel_scheduled`. |
| CodeHost | `pull_request, pr_facts(number, head), merge(number, expected_head, key)` and `for_ref(owner/repo#N)` | `merge` must refuse if the head is not `expected_head`. `pr_facts` reports unreadable as unreadable, never as empty-and-fine. Its `changed_lines` (added + deleted) is `None` when any file lacks counts; small-fix release then keeps the vote. |
| Drafter | `draft(case, thread) -> str` | Its output is data. It is never sent unapproved. It may answer `NO-REPLY: <reason>` as its first line (no reply is owed: one close vote, nothing sent), or end a reply with `MONEY: {json spec}` lines (one money vote each; the reply waits for every provider receipt, then gets its own vote as written). See `sayso/decisions.py`. |
| DevRunner | `start(case_key, brief, key) -> run_id, result(run_id), state(run_id)` | `result` returns one PR (`{"pr": int}`) or several (`{"prs": [int or "owner/repo#N"]}`); `state` is `running`, `dead` or `unknown` (unknown never retries). Measured `usage` is optional. |
| QaRunner | `fails_on_old_code(pr, head), start(case_key, brief, key) -> run_id, result(run_id)` | Must be a different agent from the dev runner. `result` returns `{"text": str}` or None. |

Rules for every adapter:
1. **Honour the idempotency key** on every write. Pass it to the vendor when
   the vendor supports one. Otherwise make the write detectable (a marker in
   the content), so a human can find a duplicate.
2. **Never report success from the write call alone.** The loop proves effects
   through read methods, so make those read the system of record.
3. **Timestamps** are ISO 8601 with a timezone. A naive time is refused.
4. **Secrets** come from `section.secret("<name>")`, which reads the env var
   named by `<name>_env`.
5. **Raise on anything you do not understand.** The loop turns exceptions into
   `UNVERIFIED` plus an alert. A guessed value turns into a wrong action.

## Plugging it in

1. Add the class to `sayso/adapters/reference.py`, or to its own module.
2. Add the choice to `ADAPTER_CHOICES` in `sayso/config.py`.
3. Add a branch to `reference.build()`.
4. Test it against a recording transport (see `tests/test_reference_adapters.py`).
   Use `sayso.adapters.http.Client(transport=...)` for HTTP APIs.
5. Prove it read-only against a real account (ONBOARDING.md, step 5).

## Vendor notes from the reference deployment

- **Discord:** `applied_tags` is a whole-list write. Editing a forum's
  `available_tags` without each tag's id deletes all tags and orphans every
  post, so the adapter never edits them. Renaming a post leaves an undeletable
  system message. Messages are limited to 2000 characters and lose trailing
  whitespace. A role mention cannot wake a bot. REST calls need an explicit
  User-Agent, and HTTP 429 carries `retry_after`.
- **Frappe Helpdesk:** it returns naive site-local time, so set the site to UTC.
  One ticket can hold mail from several people. A send is proven by exactly
  one Sent Communication **and** one Email Queue row with status Sent.
- **Gmail/IMAP:** the Sent folder is the independent receipt. Search by
  `Message-ID` with the angle brackets. Open the folder read-only.
- **Gmail with Google sign-in (`gmail` mailbox, `sayso/adapters/gmail.py`):** XOAUTH2 through the shared
  `sayso.adapters.oauth.TokenSource("google", ...)`; the SASL string is
  `user={address}^Aauth=Bearer {token}^A^A`, and an error challenge gets an empty reply. The only scope that
  works for IMAP and SMTP is `https://mail.google.com/`, which Google classes as restricted (a public app
  needs OAuth verification and a yearly security assessment). Ask with `access_type=offline` and
  `prompt=consent`, or Google returns no refresh token; refuse a grant whose `scope` lacks the mail scope
  (granular consent lets people untick it). The Sent folder is the one LIST flags `\Sent`
  (`[Gmail]/Sent Mail`, but `[Google Mail]/...` or localised in some accounts, and hidden when "Show in IMAP"
  is off): zero or two are refused, never guessed. A message counts only with `\Sent` in its `X-GM-LABELS`.
  Gmail files SMTP mail in Sent itself: never APPEND, or every send reads as two copies. `invalid_grant`
  (revoked, password changed, six months unused, 7-day Testing expiry) raises `SignInExpired` and is never
  retried; `[THROTTLED]`/`[ALERT]` replies raise `Throttled` and end the read for that run. Sending limit:
  2,000 messages a day per Workspace user (500 on trial accounts).
- **Stripe:** use a restricted key. Pass `Idempotency-Key`. Verify the refund
  by listing refunds for the charge and matching amount and currency exactly.
- **Paddle Billing** (`sayso/adapters/paddle.py`): a refund is `POST /adjustments` (`action: refund`,
  `type: full`, or `partial` with `items`) on a completed transaction `txn_...`; read back with
  `GET /adjustments?transaction_id=...&action=refund`. Most live refunds are created `pending_approval`
  and Paddle approves or rejects them later; the adapter reports `pending` until `approved`. Paddle refuses
  a new adjustment while one is pending. A cancel is `POST /subscriptions/{id}/cancel`; by default it
  schedules the cancel for the end of the billing period (status stays `active` with a `scheduled_change`).
  No idempotency header exists: the refund `reason` carries `sayso-ref:<hash of the key>` and the adapter
  lists the transaction's refunds first. Send `Paddle-Version: 1`. A 429 blocks the IP for 60 s
  (`Retry-After` header), so the adapter never retries in a run. Keys are `pdl_live_apikey_...` or
  `pdl_sdbx_apikey_...`; sandbox base is `https://sandbox-api.paddle.com`.
- **GitHub:** `PUT /pulls/{n}/merge` with `sha` fails with 409 when the head moved.
  On a private repo, some plans cannot enforce required checks, so a green CI
  run is a convention, not a gate.

## OAuth mailboxes (Microsoft 365, Google)

Microsoft has turned off Basic authentication for IMAP in every Exchange Online tenant, and turns
SMTP AUTH Basic off by default at the end of December 2026. App passwords therefore fail for most
business Outlook mailboxes. `sayso/adapters/oauth.py` signs in with OAuth 2.0 instead:

- `TokenSource(provider, client_id, client_secret, refresh_token, tenant=None, transport=None)`.
  `access_token()` uses the refresh-token grant and caches the token until 5 minutes before it expires.
  `on_rotate(new_refresh_token)` hands over a rotated refresh token; Microsoft rotates on every use,
  so a host that keeps tokens must store the new one and drop the old one.
- `xoauth2(user, token)` builds the SASL string `user={user}^Aauth=Bearer {token}^A^A`.
  `imap_auth` / `smtp_auth` wrap it for `IMAP4.authenticate("XOAUTH2", ...)` and `SMTP.auth("XOAUTH2", ...)`.
- `PROVIDERS` holds the only token endpoints that are ever called:
  `microsoft` (`login.microsoftonline.com/{tenant}`, scopes `https://outlook.office.com/IMAP.AccessAsUser.All`,
  `https://outlook.office.com/SMTP.Send`, `offline_access`) and `google` (`oauth2.googleapis.com`,
  scope `https://mail.google.com/`).
- `ReconnectNeeded` means the refresh token is dead (`invalid_grant`, `interaction_required`,
  AADSTS50173/70008/700082/65001/90094). The adapter never retries it. A person must sign in again,
  and `admin_consent` is True when an administrator must approve the app once.
- 429 and 5xx responses honour `Retry-After` (capped at 30 s, 2 retries) and then raise `OAuthError`.
  So does any token response the adapter does not understand. Error text carries only the error code.
- Hosts that run the sign-in themselves can use `pkce_pair`, `authorize_url`, `exchange_code`,
  `id_claims` and `mailbox_address`.

The `imap` mailbox takes `auth = "oauth"`:

```toml
[mailbox]
adapter = "imap"
host = "outlook.office365.com"
user = "help@contoso.com"
sent_folder = '"Sent Items"'
auth = "oauth"
provider = "microsoft"
tenant = "contoso.com"            # or the tenant id; "common" when unsure
client_id_env = "ACME_MS_CLIENT_ID"
client_secret_env = "ACME_MS_CLIENT_SECRET"
refresh_token_env = "ACME_MS_REFRESH_TOKEN"
```

Password auth (`password_env`) stays the default and is unchanged. The core has no SMTP send path
(the helpdesk sends), so OAuth only changes how the Sent-folder receipt is read.

## Help Scout (helpdesk + mailbox)

`sayso/adapters/helpscout.py`, Inbox (Mailbox) API 2.0, stdlib only. Settings:

```toml
[helpdesk]
adapter = "helpscout"
mailbox_id = 12345                     # the inbox number (Help Scout URL or GET /v2/mailboxes)
client_id_env = "ACME_HS_CLIENT_ID"
client_secret_env = "ACME_HS_CLIENT_SECRET"
# start_hours = 24                     # the first run reads this far back only

[mailbox]
adapter = "helpscout"                  # same four keys
```

How each method maps to the API (docs: https://developer.helpscout.com/mailbox-api/):
- **Auth:** OAuth2 Client Credentials (`POST /v2/oauth2/token`). The token lives 2 days and is
  kept in memory only; an HTTP 401 fetches one new token and retries once.
- **fetch_inbound:** `GET /v2/conversations?mailbox=&status=all&modifiedSince=&sortField=modifiedAt&sortOrder=asc`
  (all pages), then each conversation's threads. Only `type = customer`, `state = published`
  threads of `email` conversations count. Notes, agent replies, line items, chats and phone
  threads never become customer mail. The cursor is the newest customer thread time; each run
  re-reads 5 minutes before it, and intake drops the repeats by message id
  (`helpscout:<conversation>:<thread>`).
- **send_reply:** `POST /v2/conversations/{id}/reply`. Help Scout has **no idempotency key**, so:
  1. the reply carries a hidden marker `<!-- sayso:<hash of the vote key>:<sha256 of the body> -->`;
  2. the conversation's threads are read BEFORE every post, and a reply with this key's marker
     (or, with no marker at all, this exact text) is returned instead of posting again;
  3. the post is refused when the conversation is in another inbox, its primary customer is not the
     approved recipient, or it has cc/bcc (the email would reach more people than the vote approved);
  4. an unreadable conversation means no post.
- **sent_readback:** agent `message` threads whose marker carries the body's sha256 (or whose text
  hashes to it). `Sent` only when `state = published` and not scheduled; `draft`, `review`/
  `underreview`, `hidden` and `bounced` are `Not Sent`. The recipient is `to` plus any cc/bcc, so a
  widened send never matches the one approved recipient.
- **last_sent_at / last_inbound_at:** from the threads. `last_inbound_at` also reads the customer's
  newest 5 other conversations in the inbox (`query=(email:"...")`), so a customer who starts a new
  conversation still stops a close.
- **Rate limit:** 429 waits `X-RateLimit-Retry-After` seconds (max 60 s, 3 tries), then raises.
  The limit is per Help Scout account (200/400/800 calls a minute by plan) and a write costs 2.
- **Redirects** are never followed: a merged conversation answers 301 and is refused, so the bearer
  token never goes to another host.

**What the mailbox receipt is here, and what that weakens.** Help Scout sends the email itself and
has no Sent folder we can read. `HelpScoutMailbox.sent_copies` therefore reads the SAME system as
the helpdesk: it returns 1 when the reply thread exists, is published and is not scheduled. The
sender still needs both reads to agree, but they are not independent: a Help Scout fault that shows
a thread as published while the email never left would pass. Help Scout marks a failed delivery
`bounced`, which is refused, but a bounce can arrive after the read-back window. If you need an
independent receipt, keep `[mailbox] adapter = "imap"` on the inbox's own mail account instead.

Vendor facts to re-check on the first real call: whether `page` is 1-based (the docs' examples
disagree), whether Help Scout keeps the HTML comment marker in a reply body (the text match is the
fallback), and whether a reply thread's `to` lists the customer.
- **Zendesk** (`sayso/adapters/zendesk.py`, helpdesk + mailbox = `zendesk`):
  - Inbound reads the ticket-events export (`/api/v2/incremental/ticket_events?include=comment_events`).
    That export is **admin only** and allows 10 requests a minute, so the token must belong to an admin
    and a run reads at most 3 pages. Only PUBLIC comments written by END USERS become customer mail;
    agent replies, internal notes and unknown roles never do (an unknown role raises). The export can
    repeat events; message ids are `zendesk:<ticket>:<comment>`, so the intake de-duplicates them.
  - A reply is a public comment through `PUT /api/v2/tickets/{id}` with `html_body` (escaped, so
    Markdown and Zendesk's whitespace collapsing cannot change the approved words). It never changes
    the ticket status. It is refused when the requester is not the approved recipient, when the ticket
    has any CC (a public comment is emailed to every CC), when the ticket is closed, when the text has
    `{{` (Zendesk expands or strips placeholders) and when it is over 60,000 bytes (Zendesk truncates at
    64 kB without an error).
  - Idempotency: Zendesk documents `Idempotency-Key` for ticket CREATION, kept for two hours. The adapter
    sends it on the update too, but does not rely on it: every write first reads the ticket's audits for
    the idempotency key it stores in the audit's `metadata.custom.sayso_key`. A retry after a crash, or
    after two hours, finds the first comment and does not post again.
  - Read-back: the audit with our body hash in its metadata, then the comment it created, which must be
    public and whose text (whitespace-normalised) must match the approved text.
  - Mailbox receipt: there is no separate Sent folder; Zendesk emails the requester itself. `sent_copies`
    counts the `Notification` events to the requester in the SAME audit as our comment. This is the same
    system as the helpdesk, so it is weaker than an IMAP Sent folder: it proves Zendesk's trigger fired,
    not that the mail was delivered. If your account has no trigger that notifies the requester on a
    public comment, every send ends UNVERIFIED (that is the safe failure).
  - `last_inbound_at` also reads the customer's other tickets (`users/search` by exact email, then
    `users/{id}/tickets/requested` newest first; archived tickets are not listed by Zendesk).
  - 429: the wait is in the `Retry-After` header. Waits over a minute end the run.
  - API tokens are fine for your own (internal) use. A service used by many Zendesk customers must use
    a global OAuth client (`auth = "oauth"`); Zendesk's docs say customers may not share API tokens with
    a third party.
- **Intercom** (`sayso/adapters/intercom.py`, `adapter = "intercom"` for both
  `[helpdesk]` and `[mailbox]`): pin `Intercom-Version` on every request. A ticket is a
  conversation id (digits only). Reply with `POST /conversations/{id}/reply`,
  `type: admin`, `message_type: comment` and a real admin's id: a `note` never reaches
  the customer, and bot/Operator replies to email conversations are not emailed by
  default. The reply endpoint has NO idempotency key, so each reply ends with a
  `Ref: sayso-<key>.<body>` marker; `send_reply` reads the conversation first and does
  not post when that key's marker is already there. Search `>` on a date means
  "greater or equal" and has no sort order: read every page before moving the cursor.
  A 429 sends `X-RateLimit-Reset` as a header; the adapter turns it into a bounded
  wait. A retrieved conversation holds at most its 500 newest parts. Refuse group
  conversations (more than one contact). Hosts are fixed per region
  (`api.intercom.io`, `api.eu.intercom.io`, `api.au.intercom.io`); there is no URL option.
- **Crisp** (`sayso/adapters/crisp.py`, `adapter = "crisp"` for both `[helpdesk]` and `[mailbox]`):
  REST API v1 at `https://api.crisp.chat/v1/`, `Authorization: Basic base64(identifier:key)` plus
  `X-Crisp-Tier: plugin` (or `website` for a workspace token). One conversation (`session_id`) is one
  ticket; the customer is the conversation's `meta.email` (conversations without an email are skipped).
  Message times are epoch milliseconds. Crisp has no idempotency header: the reply carries a numeric
  message `fingerprint` derived from the idempotency key, and the adapter reads that fingerprint
  before it posts, so a retry never sends twice. A reply goes only to a conversation whose own email is
  the approved recipient. Read-back = operator `text` messages in the conversation with the same body
  hash; status `Sent` only when Crisp marks the message `stamped`. `last_inbound_at` also reads every
  other conversation of the same email (`/people/conversations/{email}/list/{page}`). 429 and 420 are
  retried; 423 (`quota_limit_exceeded`, the daily plugin quota) raises.
  **Weaker receipt:** Crisp is both the record and the receipt. `CrispMailbox.sent_copies` reads the
  message back from Crisp by fingerprint; there is no independent Sent folder. A message Crisp stored
  but never delivered (for example an email-origin conversation that bounced) still reads as sent.
- **Lemon Squeezy** (`sayso/adapters/lemonsqueezy.py`): JSON:API (`Accept` and
  `Content-Type: application/vnd.api+json`), Bearer API key, 300 calls a minute.
  Refund is `POST /v1/orders/{id}/refund` with `amount` in cents (partial allowed);
  cancel is `DELETE /v1/subscriptions/{id}`, which leaves the subscription
  `cancelled` with `ends_at` set (it runs to the end of the period). There is **no
  idempotency header**, so every write reads first: a refund is refused when the
  order already shows any `refunded_amount`, and a cancel that is already done is
  not sent again. The read-back is `refunded_amount` on `GET /v1/orders/{id}` (one
  receipt per order, not a list) and `cancelled` + `status` on
  `GET /v1/subscriptions/{id}`. Ids are bare numbers, so specs carry them prefixed
  (`ls_order_`, `ls_sub_`, `ls_cust_`) and the adapter refuses any other shape. One
  key reaches every store of the account, so each object's `store_id` must match
  the configured store.
