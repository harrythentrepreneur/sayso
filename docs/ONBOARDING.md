# Onboarding a product to real systems

Start with [QUICKSTART.md](QUICKSTART.md) on fakes. Switch one adapter at a time,
and prove each one before you switch the next.

## What the product needs

| Piece | Reference adapter | You provide |
|---|---|---|
| Board (human work surface) | `discord` | A forum channel, an alerts channel, a bot token |
| Helpdesk (owns correspondence) | `frappe` | Helpdesk URL, API key and secret, site timezone set to UTC |
| Mailbox (independent send receipt) | `imap` | IMAP host, user, app password, Sent folder name |
| Payments (optional) | `stripe` | A **restricted** key: refunds write, subscriptions write, charges read |
| Code host (optional) | `github` | Repo `owner/name`, a fine-grained token: pull requests read/write, contents read |
| Drafter | `hermes` | A Hermes profile that writes support replies |
| Dev runner (optional) | `hermes` | A Hermes profile that codes, opens a PR and stops |

Set an optional piece's adapter to `none` to switch it off.

A different vendor needs a new adapter class. See [ADAPTERS.md](ADAPTERS.md).

## Step by step

1. **Create the board.** Make a Discord forum channel. Add these tags **by hand**, spelled exactly:
   `New`, `In support`, `In dev`, `In QA`, `Awaiting approval`, `Done`, `Blocked`,
   `Waiting on customer`, plus any `labels` from your settings file.
   The adapter never creates or edits tags. On Discord, an edit that leaves out
   tag ids deletes every tag and orphans every post.
   Make a separate text channel for alerts. Give the bot permission to view,
   send messages, create posts, manage threads, and send polls in both channels.
2. **Write the secrets file**, `~/.config/sayso/<slug>.env`, with `chmod 600`:
   ```
   ACME_DISCORD_TOKEN=...
   ACME_FRAPPE_KEY=...
   ```
   Use one variable for each `*_env` name in your settings file. Never put a secret in the TOML.
3. **Copy `examples/acme-live.toml`**, then replace every placeholder id.
   Set `refund_max_minor` to the largest refund you would approve without a second look.
4. **Validate with the secrets loaded:**
   ```bash
   set -a; . ~/.config/sayso/acme.env; set +a
   sayso validate --config acme.toml
   ```
5. **Read-only proof, one adapter at a time.** Point `state.dir` at a scratch
   directory, then run only the jobs that read:
   ```bash
   sayso run reconcile --config acme.toml
   sayso run health --config acme.toml
   ```
   To prove intake without touching customers, send one email from your own
   test address (not in `own_addresses`) and run `sayso run intake`. Check the
   card on the board, its tags, and the verbatim email card.
6. **One real reply to yourself.** Approve the draft for your own test email
   and run `sayso run sender`. It must end `SENT_AND_VERIFIED`, with the email
   in your inbox and exactly one copy in the Sent folder.
7. **Install the timers.** See [INSTALL.md](INSTALL.md).

## Microsoft 365 mailbox (OAuth instead of an app password)

Business Outlook mailboxes no longer accept passwords for IMAP. Use OAuth:

1. **Register an app.** Open the Microsoft Entra admin center > App registrations > New registration.
   Choose "Accounts in any organizational directory and personal Microsoft accounts" (or only your
   organisation). Add a **Web** redirect URI that you control, for example `http://localhost:8765/callback`.
2. **Add permissions.** API permissions > Add a permission > APIs my organization uses > Office 365
   Exchange Online > Delegated: `IMAP.AccessAsUser.All` and `SMTP.Send`. Add Microsoft Graph delegated
   `offline_access`, `openid` and `email`. If your tenant only lets users consent to verified apps, press
   "Grant admin consent".
3. **Make a client secret.** Certificates & secrets > New client secret. Copy the value at once.
4. **Get a refresh token once.** Run the authorization-code flow with PKCE against your redirect URI
   (`sayso.adapters.oauth.authorize_url` and `exchange_code` do both halves) and sign in as the mailbox.
   Keep the `refresh_token` from the answer.
5. **Allow IMAP for the mailbox.** Microsoft 365 admin center > Users > Active users > the user > Mail >
   Manage email apps: tick IMAP and Authenticated SMTP.
6. **Write the secrets file** with `ACME_MS_CLIENT_ID`, `ACME_MS_CLIENT_SECRET` and
   `ACME_MS_REFRESH_TOKEN`, and set the `[mailbox]` section as shown in
   [ADAPTERS.md](ADAPTERS.md#oauth-mailboxes-microsoft-365-google).
7. Run `sayso validate`, then the read-only proof in step 5 above.

A refresh token stops working after 90 days without use, or when the user resets their password or
an admin revokes the app. The adapter then raises `ReconnectNeeded`; repeat step 4.

## Go-live checklist

Tick every line before real customers reach the loop.

- [ ] `sayso validate` passes with the real secrets file loaded
- [ ] `python3 -m unittest discover -s tests -t .` passes on the deploy host
- [ ] `python3 scripts/mutate.py` prints `MUTATION VERDICT: PASS`
- [ ] Every board tag exists, and the spelling matches docs/STAGES.md
- [ ] `[operators]` holds only the people allowed to approve
- [ ] The helpdesk site timezone is UTC (the Frappe adapter assumes it)
- [ ] One test reply to your own address ended `SENT_AND_VERIFIED`
- [ ] Payments use a restricted key, and `refund_max_minor` is set on purpose
- [ ] The alerts channel receives a test alert
- [ ] Someone knows the pause command and has read [RUNBOOK.md](RUNBOOK.md)
- [ ] The owner has approved going live, for this exact settings file

Going live is a business decision. The loop does not decide it.
