# Writing an adapter

An adapter is one class that satisfies a Protocol in `sayso/adapters/base.py`.
The core never imports a vendor SDK.

## The contracts

| Protocol | Methods | Notes |
|---|---|---|
| Board | `ensure_card, get_tags, set_tags, post, messages, open_poll, read_poll, alert, notify, last_activity`; optional `message_authors` | `set_tags` receives the full list. `read_poll` returns `{user_id: "yes"/"no"}` and must skip bots. `notify(text, key, mention)` pings `mention` and nobody else. `message_authors(card)` returns `[(user_id, iso_time)]` oldest first, bots skipped; without it every vote is for anyone. `last_activity(card)` returns the ISO time of the card's NEWEST message (not its creation; a tag change is not activity) and raises when unreadable. |
| Helpdesk | `fetch_inbound, send_reply, sent_readback, last_sent_at, last_inbound_at` | `last_inbound_at(ticket, customer)` must include mail from that customer on ANY ticket. |
| Mailbox | `sent_copies(message_id)` | Counts copies in the mailbox's own Sent folder. It must be independent of the helpdesk. |
| Payments | `refund, cancel_subscription, read_refunds, read_subscription` | Reads return the provider's own records. |
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

