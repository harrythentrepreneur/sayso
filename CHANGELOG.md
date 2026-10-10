# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

- Stall checks: a new read-only `stall` job reminds the case owner about any vote open 30+ minutes
  (then daily), flags a card quiet past its stage's limit (then daily, reset by any new message),
  and flags an open card that nothing is set to move after 15 minutes (once per case, stage and day).
  A dev card waiting for a free slot counts as moving. Every time is a setting; `Parked` cards are
  skipped. Boards gain `last_activity(card)`.

- Dev loop: configurable parallel runs; one bounded fresh retry for a proven dead run; multiple PRs
  (including across allowed repositories) each require exact-head QA and a separate merge vote; a case
  returns to support only after every PR is read back as merged. Tokens and minutes come from Hermes's
  session database, not an agent's answer.

- Vote owners: every vote says whose decision it is (`For Harry:` / `For either of you:`), the card says who
  started the work, and the decision notice pings only that operator. The owner is the first operator to post
  in the card; `sayso set-owner` overrides it. A label only: any operator can still decide. Ported from the
  PhonicsMaker loop.

### 0.2.0 (release candidate)

- Open source under the Apache License 2.0 (was proprietary). A NOTICE file credits the authors and Hermes Agent.
- Renamed to **Sayso**: the package is `sayso`, the command is `sayso` (`hcml` still works as an alias),
  and systemd units are named `sayso-<product>-<job>`. Existing installs: re-render units with
  `sayso timers render` and replace the old `hcml-*` units. The console cookie is now `sayso_session`,
  so operators sign in once more.
- New README with the phone demo GIF, the production numbers from PhonicsMaker, and a credits section.
- Console: the phone layout wraps the tabs onto two rows, so "Money", "Health" and "Audit log" are no longer cut off.
- Privacy masker: product, team and character names are no longer hard-coded; pass them with `Masker(..., keep=[...])`.

- QA stage (`qa` job, `[qa_runner]`): pre-QA script gate (CI green, a test changed, no secrets or real emails in
  the diff), fails-on-old-code check, one independent QA run on the exact head with a required verdict line and
  customer-fix answer, real-app evidence for UI changes, QA-blocked cards back to dev for at most
  `max_dev_rounds`, provider usage limits labelled instead of failed. The merge vote opens only on a QA pass,
  and the release job refuses a merge for any head QA did not pass. Ported from the PhonicsMaker loop.

- Draft job runs the sender's own `check_sendable` before it opens a vote, so an operator is never asked to approve a
  reply the sender would refuse. Product rules plug in through a new `check_reply` hook; example plugin `reply_rules`.
- Reply subjects carry exactly one `Re:` (a ticket titled `Re: ...` no longer gets `Re: Re: ...`).
- IMAP Sent-folder read retries a dropped connection up to 3 times (read only); only 3 failures count as unreadable.
- CI installs the system word list (`wamerican`) that privacy masking reads; without it one privacy test failed on every run.

- Read-only web dashboard: `sayso dashboard --config F` or `--demo`. Decisions, board, case pages, money,
  health and audit log for one or more products. GET only, loopback only, writes nothing. See docs/DASHBOARD.md.

## [0.1.0] - 2026-09-28

First complete version on fakes.

- Core: stage machine, one card per customer per kind, revision-bound votes,
  safety refusals, money specs with a cap and a currency list, atomic locked
  state, and an append-only journal.
- Jobs: intake, draft, votes, sender, money, dev, release, close, reconcile, health.
- Adapter Protocols; fakes with fault injection; reference adapters for Discord,
  Frappe Helpdesk, IMAP, Stripe, GitHub and Hermes (off unless `mode = "live"`).
- Plugins, with the `product_failure` example (off by default).
- CLI (`sayso`), systemd unit renderer (it installs nothing), fake end-to-end demo.
- 91 tests on fakes (including a quickstart run and docs-accuracy checks); mutation harness with 32/32 guards killed.
- Not yet run against a live product.
- Project docs: README with diagrams, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue and PR templates, CODEOWNERS.

[0.1.0]: https://github.com/harrythentrepreneur/sayso/releases/tag/v0.1.0
