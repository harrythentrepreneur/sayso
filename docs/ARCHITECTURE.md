# Architecture

## Parts

```
settings.toml ──► config.py (validate) ──► runtime.build() ──► Context
                                                              │  config, clock, adapters, plugins, paths, journal
                                                              ▼
                          jobs/*.py  (one idempotent pass each; a systemd timer calls `sayso run <job>`)
                              │
      core rules:  stages.py  cases.py  votes.py  money.py  safety.py  store.py
                              │
      adapters/base.py (Protocols) ◄── fake.py (tests, demo) | reference.py (Discord, Frappe, IMAP, Stripe, GitHub, Hermes)
```

- **Core rules** hold no vendor code. They decide what is allowed.
- **Jobs** read state, call adapters and write state. Each job is safe to run
  twice and safe to kill at any line (see "Crash safety" below).
- **Adapters** are the only code that talks to the outside world. Each one
  satisfies a Protocol in `adapters/base.py`.
- **Plugins** add product-only behaviour through hooks. See [PLUGINS.md](PLUGINS.md).

## Jobs

| Job | Acts on | Does |
|---|---|---|
| intake | new helpdesk mail | Opens a card or appends to one; voids reply and close votes; reopens Done cards |
| draft | cards `In support` with no live reply vote | Writes an immutable draft file, opens a reply vote, moves the card to `Awaiting approval` |
| votes | open votes | Records operator decisions; voids a vote whose material changed; expires stale votes |
| sender | approved reply votes | Sends once, proves the send, moves the card to `Done` + `Waiting on customer` |
| money | approved money votes | Refunds or cancels once, then verifies with the provider |
| dev | cards `In dev` | Starts one bounded run per round; when a PR exists, moves the card to `In QA` |
| qa | cards `In QA` | Pre-QA script gate, fails-on-old-code check, one independent QA run on the exact head; PASS opens the merge vote, BLOCKED returns the card to `In dev` (max `max_dev_rounds`, then `Blocked`) |
| release | approved merge votes | Merges at the exact head SHA, reads the merge back, returns the card to support for a reply |
| close | `Waiting on customer` cards quiet for N days | Opens one close vote; an approved close leaves `Done` |
| reconcile | everything | Reports drift as alerts. Never repairs. |
| health | heartbeats | Alerts when a job has stopped running |

`sayso tick` runs them in this order: intake, votes, release, money, sender, dev,
qa, draft, close, reconcile, health.

## State (under `[state] dir`)

| File | Owner | Content |
|---|---|---|
| `cases.json` | cases.py | case key → card id, customer, kind, ticket, draft version, dev run, PR |
| `votes.json` | votes.py | vote key → kind, fingerprint, spec, status, who decided |
| `attempts.json` | sender, money | send/money attempts: `pending`, `SENT_AND_VERIFIED`, `VERIFIED_WITH_PROVIDER`, `UNVERIFIED` |
| `seen-messages.json` | intake | inbound message ids already acted on |
| `cursors.json` | intake | helpdesk cursor |
| `heartbeats.json` | every job | the last run and result of each job |
| `journal.jsonl` | every job | append-only log, one line per state change |
| `drafts/<case>/reply-vN.txt` | draft | immutable drafts; a vote binds to the SHA-256 of one of them |
| `receipts/<case>/*.json` | sender | proof of each send |
| `PAUSED` | operator | present = paused |
| `locks/` | cli | one lock per job, so two runs never overlap |
| `fake-world.json` | fakes | the fake systems' state (fake adapters only) |

Every JSON write is atomic (temp file, fsync, rename, mode 0600) and made while
holding a lock. A corrupt file stops the loop instead of being read as empty.

## Crash safety

- **Record intent first.** The sender and the money job write a `pending`
  attempt before they call the provider. After a crash, a `pending` attempt
  is only **re-read**, never re-executed. If the read-back cannot prove the
  action, the attempt becomes `UNVERIFIED` and an alert fires once. A human decides.
- **Idempotency keys** go to every adapter write. Where the vendor supports
  them (Stripe's `Idempotency-Key`, Discord's `nonce` + `enforce_nonce`), they
  also protect the call on the vendor's side.
- **Intake** marks a message as seen only after its effect is on the card. The
  cursor moves only after the whole batch is recorded.
- **Dev** records the start before it dispatches. A card that is interrupted
  mid-start raises an alert and is not started a second time.

## Sources of truth

- The **helpdesk** owns the conversation. The loop never keeps a competing customer history.
- The **payment provider** owns money. A local row is never a receipt.
- The **code host** owns the PR and merge state.
- The **board** is the human surface. Its tags are read live before every action.
- The **state directory** records only what the loop did and decided.
