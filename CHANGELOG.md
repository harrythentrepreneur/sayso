# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

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
- `baker` adapter for a texting product (iMessage / WhatsApp) as the helpdesk: reads the product's case signals,
  sends an approved reply through the product, proves it with the product's record AND the provider's delivery
  status. `sending = false` on `[helpdesk]` for a shadow run. `InboundMessage` gained optional `kind`, `labels`
  and `card`; poll subjects come from `cases.subject()`. Drafter `style` option. See docs/ADAPTERS.md.
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
