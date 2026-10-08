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
| Decisions | `/p/<slug>/` | Cards per stage, and every open vote with the exact thing being voted on: the draft text, the money spec, the PR head, or the last-reply time |
| Board | `/p/<slug>/board` | Cards in columns by their live stage, with labels |
| Case | `/p/<slug>/case/<key>` | The customer's own email first, then votes with who decided, every draft version, send receipts, the card thread and a timeline |
| Money | `/p/<slug>/money` | Refund cap, currencies, and each money request with its vote and provider check |
| Health | `/p/<slug>/health` | Each job's last run and whether it is stale, the pause state, recent alerts |
| Audit log | `/p/<slug>/audit` | The last 300 journal events, newest first |
| JSON | `/api/<slug>.json` | Stage counts, open votes and health, for scripts |

## Why it is safe to leave running

- **It never writes.** It reads the state files with plain reads: no locks, no
  temp files, no new directories. It never builds the loop's runtime, so it
  cannot reach an adapter that sends, charges or merges. A test browses every
  page twice and checks that every file's hash and timestamp are unchanged.
- **GET only.** POST, PUT, PATCH and DELETE get HTTP 405. No page has a form,
  button, input or script.
- **This machine only.** It refuses to listen on anything but loopback,
  because it has no login. To see it from another computer, use an SSH tunnel:
  `ssh -L 8765:127.0.0.1:8765 host`.
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

Approving from the web, login, and cost tracking are not built. Web approval
will be a new board adapter with its own safety tests, so the core does not change.
