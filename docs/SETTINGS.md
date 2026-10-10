# Settings file reference

One TOML file per product. Unknown keys are refused, so a typo cannot quietly
fall back to a default. Validate with `sayso validate --config FILE`.

## [product]

| Key | Required | Meaning |
|---|---|---|
| `name` | no | Display name. Defaults to the slug. |
| `slug` | yes | 2–41 characters: lower-case letters, digits, `-`. Used in unit and file names. |
| `mode` | no | `dry-run` (the default) or `live`. Real adapters need `live`. |
| `support_address` | yes | The address customers write to. Replies are sent from it. |
| `own_addresses` | no | Other addresses of yours. Mail from these is never a customer, and a reply to them is refused. |
| `labels` | no | Extra board labels. They must not reuse a stage name. |

## [operators]

`"<board user id>" = "<name>"`. At least one is required. Only these users can
approve or decline. Anyone else's vote is kept as an opinion and decides nothing.

## [state]

`dir`: where cases, votes, attempts, receipts, drafts and the journal live. A
relative path is relative to the settings file. Keep it out of any git repository.

## Adapter sections

`[board] [helpdesk] [mailbox] [payments] [codehost] [drafter] [dev_runner] [qa_runner]`

Each section takes `adapter = "..."` plus the adapter's own keys.

| Section | Choices | Default |
|---|---|---|
| board | `fake`, `discord` | `fake` |
| helpdesk | `fake`, `frappe` | `fake` |
| mailbox | `fake`, `imap` | `fake` |
| payments | `none`, `fake`, `stripe` | `none` |
| codehost | `none`, `fake`, `github` | `none` |
| drafter | `fake`, `hermes` | `fake` |
| dev_runner | `none`, `fake`, `hermes` | `none` |
| qa_runner | `none`, `fake`, `hermes` | `none` |

A key ending in `_env` names an **environment variable**, never a value.
`token_env = "ACME_DISCORD_TOKEN"` is accepted. `token_env = "abc123"` is refused.
A missing or empty variable is an error when the adapter starts.

Reference adapter keys:
- discord: `forum_id`, `alerts_channel_id`, `token_env`
- frappe: `url`, `api_key_env`, `api_secret_env`
- imap: `host`, `user`, `sent_folder` (default `"[Gmail]/Sent Mail"`), `password_env`
- stripe: `api_key_env`
- github: `repo` (`owner/name`), `token_env`, optional `allowed_repos` (comma-separated `owner/name` list;
  defaults to `repo`). Cross-repository PRs outside that list are refused before any network call.
- hermes (drafter and dev_runner): `profile`
- hermes (qa_runner): `profile` (must differ from the dev profile), `red_proof_cmd` (prints one JSON line
  `{"state": "red-proved" | "not-red" | "head-red" | "error"}`; gets the PR number and head SHA as its last two arguments)

## [policy]

| Key | Default | Meaning |
|---|---|---|
| `quiet_close_days` | 3 | Days with no customer reply after our sent reply before a close vote opens |
| `readback_seconds` | 120 | How long the sender waits for proof of a send |
| `readback_interval` | 10 | Seconds between read-backs |
| `unsent_alert_hours` | 24 | The reconciler alerts when an approved reply is still unsent after this long |
| `vote_expiry_hours` | 72 | An unanswered vote expires after this long; nothing is done |
| `max_new_cases_per_run` | 40 | Intake stops and alerts past this many new cards in one run |
| `refund_max_minor` | 0 | Largest refund in minor units (cents). Must be > 0 when payments are on. |
| `currencies` | `[]` | Allowed refund currencies, lower-case ISO codes. Required when payments are on. |
| `max_parallel_dev_runs` | 2 | Maximum active dev runs at once; a finished PR frees a slot. |
| `max_dev_rounds` | 2 | Dev rounds per case. After the last one QA blocks, the card goes to `Blocked` for a human. |
| `red_proof_max_tries` | 3 | Times the fails-on-old-code check may fail to RUN on one head before a human is alerted. |
| `qa_limit_retries` | 6 | QA runs stopped by a provider usage limit on one head before a human is alerted. |
| `vote_remind_minutes` | 30 | An open vote gets its first reminder after this many minutes, then one per `stall_repeat_hours`. |
| `orphan_minutes` | 15 | An open card with nothing set to move it (no vote, no draft due, no dev/QA run or runner) is flagged after this much quiet. At most once per case, stage and day. A dev card waiting for a free slot counts as moving. |
| `stall_new_hours` | 1 | Hours of no card activity before a `New` card is flagged. |
| `stall_support_hours` | 2 | Same, for `In support`. |
| `stall_dev_hours` | 2 | Same, for `In dev`. |
| `stall_qa_hours` | 1 | Same, for `In QA`. |
| `stall_awaiting_hours` | 2 | Same, for `Awaiting approval`. |
| `stall_blocked_hours` | 4 | Same, for `Blocked`. |
| `stall_repeat_hours` | 24 | A vote reminder or stall notice repeats at most once per this many hours. Any new message in the card resets the stall clock. |
| `parked_label` | `Parked` | A card with this tag gets no stall or orphan notice (its open votes still get reminders). The tag must exist on the board. |
| `qa_ui_paths` | `""` | Regular expression for user-interface files. A PR touching one needs real-app `EVIDENCE:` to pass QA. |

## [jobs]

Seconds between runs for each job, minimum 30:
`intake 60, draft 120, sender 120, money 120, votes 60, dev 300, qa 300, release 300,
close 900, reconcile 600, stall 300, health 600`. Health reports a job as stale after three missed intervals.

## [[plugins]]

```toml
[[plugins]]
name = "product_failure"      # module in sayso.plugins, or "package.module"
enabled = false
options = { patterns = { quota = "out of credit" } }
```

## Money spec (for `sayso money-request --spec FILE`)

A refund, with the amount in minor units:
```json
{"action": "refund", "charge": "ch_...", "amount": 1900, "currency": "usd", "customer": "cus_..."}
```

A cancellation:
```json
{"action": "cancel_subscription", "subscription": "sub_...", "customer": "cus_..."}
```

Exactly these fields. The spec is what the vote approves: change any field and
the vote is void.
