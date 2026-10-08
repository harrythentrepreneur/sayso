# Operator runbook

## Stop everything now

```bash
sayso pause --config acme.toml --reason "why"
```

Intake, draft, sender, money, dev, release and close stop at their next run.
Reconcile and health keep reporting. A run already in progress finishes its
current step. To stop the timers themselves as well:
`systemctl --user stop 'sayso-acme-*.timer'`.

Resume with `sayso resume --config acme.toml`. Before you resume, check the
alerts channel for anything raised while you were investigating.

## Alerts and what to do

Each alert fires once per problem, and the text names the case.

| Alert | Meaning | Do |
|---|---|---|
| `reply ... started but not proven sent` | The send attempt is `UNVERIFIED` | Check the helpdesk ticket and the mailbox Sent folder by hand. If it went out, say so on the card and move the card to `Done` + `Waiting on customer`. If it did not, move the card back to `In support` so a new draft and vote are made. **Never** edit `attempts.json` to force a resend. |
| `reply refused: ...` | A safety rule refused an approved reply | Read the reason. Usually a money claim with no receipt: fix the draft (a new version needs a new vote), or complete the money action first. |
| `... could not be verified with the provider` | A money attempt is `UNVERIFIED` | Check the payment provider dashboard. Record what actually happened on the card. Never re-run it blind. |
| `merge ... reads back as open` | The code host said OK but did not merge | Check the PR. Branch protection or a failing check is the usual cause. |
| `dev start ... was interrupted` | The dev job crashed between recording and dispatching | Check whether a run started. Clear `dev_run` in `cases.json` only if none did. |
| `jobs not running: ...` | No heartbeat for three intervals | `systemctl --user status sayso-acme-<job>.service`, then `journalctl`. |
| `intake stopped at N new cards` | A burst of mail (or a loop) | Check the helpdesk for a mail loop before you raise `max_new_cases_per_run`. |
| `awaiting-no-vote`, `bad-tags`, `card-missing` | Board and records disagree | Someone moved the card by hand. Put it back to a legal stage (STAGES.md). The reconciler never repairs anything itself. |
| `approved-not-sent` | An approved reply is still unsent after `unsent_alert_hours` | Check the sender service. Usually the card was moved off `Awaiting approval`. |

## Recover after a crash or reboot

Do nothing special. Every job is idempotent, and every `pending` attempt is
re-read before anything else happens. Run `sayso status` and read the alerts channel.

## Restore state from backup

Stop the timers first. Restore the whole state directory, not single files:
votes, attempts and receipts must agree with each other. Then run
`sayso run reconcile` and read every finding before you resume.

## Rotate a key

1. Create the new key at the vendor.
2. Replace the value in `~/.config/sayso/<slug>.env`. The variable name stays the same.
3. `sayso validate --config ...`, then `sayso run reconcile --config ...` (reads only).
4. Revoke the old key.

No restart is needed: each timer run reads the secrets file fresh.

## Change an operator

Edit `[operators]` and validate. Votes already approved stay approved. Open
votes are decided under the new list from the next tally.

## Upgrade

`git pull`, then run the tests and `scripts/mutate.py` on the host. Read
CHANGELOG.md for state-format changes. Pause during the pull if a release
note asks you to.
