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
- **Stripe:** use a restricted key. Pass `Idempotency-Key`. Verify the refund
  by listing refunds for the charge and matching amount and currency exactly.
- **GitHub:** `PUT /pulls/{n}/merge` with `sha` fails with 409 when the head moved.
  On a private repo, some plans cannot enforce required checks, so a green CI
  run is a convention, not a gate.

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
