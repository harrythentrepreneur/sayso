# Writing an adapter

An adapter is one class that satisfies a Protocol in `sayso/adapters/base.py`.
The core never imports a vendor SDK.

## The contracts

| Protocol | Methods | Notes |
|---|---|---|
| Board | `ensure_card, get_tags, set_tags, post, messages, open_poll, read_poll, alert` | `set_tags` receives the full list. `read_poll` returns `{user_id: "yes"/"no"}` and must skip bots. |
| Helpdesk | `fetch_inbound, send_reply, sent_readback, last_sent_at, last_inbound_at` | `last_inbound_at(ticket, customer)` must include mail from that customer on ANY ticket. |
| Mailbox | `sent_copies(message_id)` | Counts copies in the mailbox's own Sent folder. It must be independent of the helpdesk. |
| Payments | `refund, cancel_subscription, read_refunds, read_subscription` | Reads return the provider's own records. |
| CodeHost | `pull_request, pr_facts(number, head), merge(number, expected_head, key)` | `merge` must refuse if the head is not `expected_head`. `pr_facts` reports unreadable as unreadable, never as empty-and-fine. |
| Drafter | `draft(case, thread) -> str` | Its output is data. It is never sent unapproved. |
| DevRunner | `start(case_key, brief, key) -> run_id, result(run_id)` | `result` returns `{"pr": int, "summary": str}` or None. |
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
