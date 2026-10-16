# Dashboard (read-only)

A local web view of one or more products. It shows everything the loop knows
and **changes nothing**. Votes are still cast on the board poll.

```bash
sayso dashboard --demo                                  # fake data, safe anywhere
sayso dashboard --config acme.toml                      # one product
sayso dashboard --config acme.toml --config beta.toml   # several products, one page
```

Then open <http://127.0.0.1:8765/>. Use `--port` to change the port.

## Screens

| Screen | Path | Shows |
|---|---|---|
| Products | `/` | Each product: decisions waiting, cases, running or paused, job health |
| Decisions | `/p/<slug>/` | The queue: money and billing first, then repeat contacts, then oldest (`?sort=oldest` / `newest`). Each decision shows the exact thing voted on (draft text, refund spec, PR head, last-reply time), how long it has waited, an "Expires in" badge near `vote_expiry_hours`, triage flags, and the say-so lock: what the Yes binds to plus its fingerprint |
| Swipe | `/p/<slug>/swipe` | Console only ("Review one by one" on Decisions). One open decision at a time: drag right for Approve, left for Decline, or use the arrow keys or buttons. A swipe posts the same fingerprinted vote as the buttons; if the version changed, the card springs back with the reason. Only this page runs a script (`/static/swipe.js`, same origin, CSP `script-src 'self'`). |
| Inbox | `/p/<slug>/board` | One row per case: customer, subject, preview, what is waiting on you, flags, last email (relative, exact UTC time on hover). Tabs: All, Waiting on you, In progress, Closed (`?view=`). Search with `?q=` (a search box in the console) |
| Case | `/p/<slug>/case/<key>` | The customer's own email first (headers folded), the timeline, then a decision panel (material, lock, buttons), receipts (sends seen in Sent, refunds verified with the provider), properties, past votes and every draft version |
| Refunds | `/p/<slug>/money` | Tiles: waiting, refunded and verified, declined or not run, the refund cap. Tabs by status (`?view=`). Each request with amount, decision and provider check |
| Insights | `/p/<slug>/overview` | KPI tiles with a source line each, 7/14/30-day periods (`?days=`), time waiting on you, daily charts with a labelled axis, outcomes, flags, who decided. Numbers the core does not record are named, not shown |
| Health | `/p/<slug>/health` | A state line, every job in run order with its result in words, last run and interval; a stale job gets "What to do" (the command to run it once and its journalctl unit); recent alerts |
| Audit log | `/p/<slug>/audit` | Journal events newest first, grouped by UTC day, with tabs by type (`?kind=`), a case filter (`?case=`) and search (`?q=`); each row opens to the full journal line; vote rows show what the Yes bound to and the fingerprint. The newest 500 are shown |
| Audit CSV | `/p/<slug>/audit.csv` | Every matching journal line (same filters, no cap) as a CSV download. Cells that start like a formula get a leading `'` |
| JSON | `/api/<slug>.json` | Stage counts, open votes and health, for scripts |

Both the read-only dashboard and the signed-in console render these pages from the same code
(`sayso/dashboard.py`), with the shared parts from `sayso.brand.app`. The dashboard shows no forms: the
sort, tabs and filters are plain links, and search is the `?q=` parameter.

## Why it is safe to leave running

- **It never writes.** It reads the state files with plain reads: no locks, no
  temp files, no new directories. It never builds the loop's runtime, so it
  cannot reach an adapter that sends, charges or merges. A test browses every
  page twice and checks that every file's hash and timestamp are unchanged.
- **GET only.** POST, PUT, PATCH and DELETE get HTTP 405. No page has a form,
  button, input or script, including every filter, tab and the CSV export (tests sweep them).
- **This machine only.** It refuses to listen on anything but loopback,
  because it has no login. To see it from another computer, use an SSH tunnel:
  `ssh -L 8765:127.0.0.1:8765 host`.
- **Only its own Host name (DNS rebinding).** A web page on another site can point its own domain at
  127.0.0.1 and try to read the dashboard from your browser. The browser still sends that domain as the
  `Host` header, so the dashboard answers only to `127.0.0.1:<port>`, `localhost:<port>` and
  `[::1]:<port>`; anything else (or no Host) gets `421 Misdirected Request`. The console does the same for
  the address it is started on. If you put either behind a reverse proxy or tunnel that passes its own name
  through, list that name: `--allowed-host dash.example.com` (any port) or `--allowed-host
  dash.example.com:8443` (that port only). Repeat the flag for more names. Only list names you control.
- **Customer text is shown as text.** Email bodies, subjects and drafts are
  HTML-escaped, and a strict Content-Security-Policy blocks scripts.
- **Only this product's files.** A draft path outside the state directory is
  not read.
- **Fail closed.** A corrupt state file shows an error page (HTTP 500), never
  an empty board. If a real board cannot be read, the stage shows as
  `unreadable`, never a guess.

Each guard has an entry in `scripts/mutate.py`.

## Stages with a real board

With fake adapters, stages come from the fake world file. With a real board,
the dashboard calls the board adapter's `get_tags` for each card on each page
load. That is a read, but it does use the board's API quota, so keep the
dashboard for people rather than for polling scripts.

## Not yet

The read-only dashboard cannot approve; approving from the web is the console's job
(`sayso console`, operator sign-in, see the Swipe and Decisions screens). Cost tracking is not built.
