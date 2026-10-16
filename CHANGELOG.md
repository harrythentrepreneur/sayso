# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

- Security: the dashboard and the console answer only to the Host they were started on (DNS-rebinding guard,
  `sayso/hostcheck.py`). A loopback server answers to `127.0.0.1:<port>`, `localhost:<port>` and
  `[::1]:<port>`; any other Host, a missing Host or two Host headers get `421 Misdirected Request` (plain
  text) before any page, login or vote. Behind a reverse proxy or tunnel that keeps its own Host name, add
  `--allowed-host NAME[:PORT]` (repeatable) to `sayso dashboard` and `sayso console serve`. `make_server` and
  `serve` gain an optional `allowed_hosts` keyword; nothing else changes.
- Console and dashboard pages, ported from Sayso Cloud: a grouped sidebar (daily work, then Admin), a dry-run
  strip and an account menu; **Decisions** as a priority queue (money and billing, repeat contacts, oldest) with
  waiting and expiry badges and a say-so lock on every decision (what the Yes binds to plus its fingerprint),
  labelled Approve & send / Approve refund / Decline; **Inbox** (was Cases) with status tabs, search and relative
  times; the case page leads with the customer's email and a decision panel with receipts; **Refunds** (was
  Money) with tiles, tabs and a provider-check column; **Insights** (was Overview) with sourced KPI tiles,
  7/14/30-day periods, labelled chart axes and time waiting on you; **Health** with jobs in run order and what to
  do when one is stale; **Audit log** with type tabs, day groups, row detail and `/p/<slug>/audit.csv`.
  The dashboard stays read-only (no forms: filters are links, search is `?q=`). Routes are unchanged.
- `sayso.brand.app`: the shared page parts (badge, KPI tile, tabs, search, stacked-on-phone table, relative time
  with the exact time in `title`, money as `$12.00 USD`, the say-so lock line, axis chart). New names only;
  `sayso.dashboard.CSS` and every name Sayso Cloud imports are unchanged.

- Stable API: `sayso/stable_api.json` lists the 99 names Sayso Cloud builds on, and
  `tests/test_stable_api.py` fails when a change would break one (removed, renamed, reordered, a new
  required parameter, a changed constant). Additions stay free. See CONTRIBUTING "Stable API".
- CI: the seeded-fault harness matches the held-draft guard again (main had been red since `f5ba3f6`).
- QA answers are read the way CommonMark shows them (zero dependencies): a fence opens only with 3+
  backticks or tildes at most 3 spaces in, closes only with the same character and at least as many,
  and runs to the end when unclosed; list items, block quotes and HTML blocks are modelled too. A PASS
  counts only when that exact final line is plain, so an earlier plain PASS can no longer vouch for a
  fenced one. Cross-checked against markdown-it-py (randomized) and on 80 real QA answers.
- QA `BLOCK-KIND: EVIDENCE <what is missing>`: a block that only lacks proof no dev run can make goes
  to `Blocked` for a human with no dev round spent. Any `FAILING-CHECK` line anywhere means a code
  defect; an unclear label keeps today's rule. A BLOCK-KIND line beside a PASS is a block.
- A QA block caused by QA's own tool timing out (no FAILING-CHECK, plain answer) re-runs QA once on the
  same head, not a dev round. A second timeout is a normal block.
- CI that GitHub refused to start (account billing) reads as `not-run`, not red, only with GitHub's own
  record: a failure annotation on `.github` with GitHub's billing sentence AND a job with no runner and
  no steps. QA on the exact head is the gate; content checks still apply and small-fix release never
  applies to such a case.
- Stall: an Awaiting-approval card whose vote is open gets only the vote reminder, not a second stall
  line repeating it.

- Close and money: a drafter can answer `NO-REPLY: <reason>` (spam, invites, a bare thank-you) to get ONE
  close vote that ends the case with nothing sent, never redrafted until the customer writes again, and
  never re-asked after a No; or end a reply with `MONEY: {spec}` lines to open refund/cancel votes while
  the reply waits for every provider receipt and is then voted on as written. A busy job lock is waited
  for (still busy exits 75 and is journalled, never a silent success), and health watches every job the
  product switched on, including money, dev, QA, release and stall.

- QA regression set (on by default, `[policy] qa_regression_set`): every FAILING-CHECK and DEFECT-CLASS QA
  blocks a case on is kept in the case record. Every later QA run re-runs the whole set first (one
  `REGRESSION: <n> PASS` line per entry; a FAIL blocks; a missing report re-runs QA once, not a dev round),
  and every later dev brief carries the set with "fix the class, not the instance". Ported from the
  PhonicsMaker loop, where one case had taken 5-7 QA runs, each on a new path into the same defect.

- Small-fix release (opt-in, off by default): `[policy] small_fix_release = true` lets a fix merge on
  an independent QA pass with no merge vote, only when every PR head passed QA, the whole set is within
  `small_fix_max_lines` / `small_fix_max_files`, no path matches `small_fix_sensitive_paths`, and every
  size is readable. Re-checked at merge time; if anything changed it falls back to a vote. Replies,
  money and closing still need a person. Code hosts report `changed_lines`.

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
