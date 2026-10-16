# Onboarding a product to real systems

Start with [QUICKSTART.md](QUICKSTART.md) on fakes. Switch one adapter at a time,
and prove each one before you switch the next.

## What the product needs

| Piece | Reference adapter | You provide |
|---|---|---|
| Board (human work surface) | `discord` | A forum channel, an alerts channel, a bot token |
| Helpdesk (owns correspondence) | `frappe` | Helpdesk URL, API key and secret, site timezone set to UTC |
| Helpdesk, Zendesk instead | `zendesk` | Zendesk subdomain, an admin's email and API token (see below) |
| Mailbox (independent send receipt) | `imap` | IMAP host, user, app password, Sent folder name |
| Mailbox, Gmail with Google sign-in | `gmail` | A Google Cloud OAuth client and one sign-in ([below](#gmail-with-google-sign-in)) |
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

## Gmail with Google sign-in

Use this when an app password is not possible (2-Step Verification off, or the Workspace admin blocks app
passwords). The mailbox then signs in with a refresh token over XOAUTH2. Nothing here is sent to Sayso.

1. **Create a Google Cloud project** at console.cloud.google.com (or reuse one you own).
2. **Turn on nothing.** IMAP/SMTP need no API to be enabled; only the OAuth client below.
3. **Branding / consent screen** (Google Auth Platform > Branding): app name, support email, your
   address as developer contact. Audience: *Internal* if the mailbox is in your own Workspace
   organisation (no review, no user cap), otherwise *External* and keep it in **Testing**, then add
   the mailbox address under Audience > Test users.
4. **Data access**: add the scope `https://mail.google.com/` (Google lists it as *restricted*; it is
   the only scope that works for IMAP and SMTP).
5. **Clients > Create client > Desktop app.** Copy the client id and secret into your secrets file:
   ```
   ACME_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   ACME_GOOGLE_CLIENT_SECRET=...
   ```
6. **Sign in once** on a machine with a browser (it listens on 127.0.0.1 only):
   ```bash
   set -a; . ~/.config/sayso/acme.env; set +a
   python3 -m sayso.adapters.gmail --client-id-env ACME_GOOGLE_CLIENT_ID \
       --client-secret-env ACME_GOOGLE_CLIENT_SECRET --out ~/.config/sayso/acme.env \
       --var ACME_GOOGLE_REFRESH_TOKEN --user help@yourshop.com
   ```
   Tick the Gmail box on the Allow screen. The command appends the refresh token to the env file
   (mode 600) and never prints it.
7. **Settings:**
   ```toml
   [mailbox]
   adapter = "gmail"
   user = "help@yourshop.com"
   client_id_env = "ACME_GOOGLE_CLIENT_ID"
   client_secret_env = "ACME_GOOGLE_CLIENT_SECRET"
   refresh_token_env = "ACME_GOOGLE_REFRESH_TOKEN"
   ```
8. **Workspace admins** who restrict third-party apps: Admin console > Security > API controls >
   App access control > add the client id as *Trusted*. Without it Google answers `admin_policy_enforced`.

Limits to know: an External app in **Testing** gets refresh tokens that expire after **7 days** (sign in
again weekly, or use Internal). Google keeps at most 100 live refresh tokens per account per client; the
oldest silently stops working. A password change on the Google account ends the token. When the token
stops working the mailbox raises `SignInExpired` once, and you repeat step 6.
### Help Scout instead of Frappe + IMAP

1. In Help Scout, open **Your Profile > My Apps > Create My App**. Any redirect URL works for
   Client Credentials (for example `https://example.com`). Copy the App ID and App Secret.
   The app acts as YOUR user, so that user must be able to see and reply in the inbox.
   The Free plan has no API access.
2. Find the inbox number: `GET https://api.helpscout.net/v2/mailboxes` once with a token from
   step 1 lists every inbox with its `id` and `email`.
3. Put `ACME_HS_CLIENT_ID=...` and `ACME_HS_CLIENT_SECRET=...` in the secrets file and set
   `[helpdesk]` and `[mailbox]` to `adapter = "helpscout"` (see [ADAPTERS.md](ADAPTERS.md)).
   Set `support_address` to the inbox's own email address.
4. Read-only proof: `sayso run intake` after writing in from your own test address. The card's
   ticket is the Help Scout conversation id. Then step 6 above with your own conversation: the
   sender must end `SENT_AND_VERIFIED`, and Help Scout must show exactly one reply.
## Zendesk instead of Frappe

1. In Zendesk Admin Center, open **Apps and integrations > APIs > Zendesk API**, turn on token access
   and add a token named `sayso`. Use an **admin** account: the ticket-events export Sayso reads is admin
   only. (Zendesk marks API tokens as deprecated in favour of OAuth; for OAuth set `auth = "oauth"` and
   put the access token in the variable instead.)
2. Check that a trigger emails the requester when an agent adds a public comment (the default
   "Notify requester of comment update" does). Sayso's send receipt is that notification.
3. Settings:
   ```toml
   [helpdesk]
   adapter = "zendesk"
   subdomain = "acme"            # acme.zendesk.com
   email = "admin@acme.example"
   token_env = "ACME_ZENDESK_TOKEN"
   [mailbox]
   adapter = "zendesk"
   ```
   Set `[product] support_address` to your Zendesk support address.
4. Prove it read-only: `sayso run intake` against a test ticket you opened as an end user from your own
   address, then one approved reply to yourself (step 6 above). The ticket must have no CCs.

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
