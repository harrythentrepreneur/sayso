# Onboarding a product to real systems

Start with [QUICKSTART.md](QUICKSTART.md) on fakes. Switch one adapter at a time,
and prove each one before you switch the next.

## What the product needs

| Piece | Reference adapter | You provide |
|---|---|---|
| Board (human work surface) | `discord` or `slack` | Discord: a forum channel, an alerts channel, a bot token. Slack: one channel and a bot token (see below) |
| Helpdesk (owns correspondence) | `frappe` | Helpdesk URL, API key and secret, site timezone set to UTC |
| Helpdesk, Zendesk instead | `zendesk` | Zendesk subdomain, an admin's email and API token (see below) |
| Helpdesk + receipt, Intercom | `intercom` (both `[helpdesk]` and `[mailbox]`) | A private-app access token, the replying admin's id, the region. See "Intercom" below |
| Mailbox (independent send receipt) | `imap` | IMAP host, user, app password, Sent folder name |
| Mailbox, Gmail with Google sign-in | `gmail` | A Google Cloud OAuth client and one sign-in ([below](#gmail-with-google-sign-in)) |
| Helpdesk + mailbox on Crisp (instead of the two rows above) | `crisp` | Crisp website id and a token keypair (see "Crisp" below) |
| Payments (optional) | `stripe` | A **restricted** key: refunds write, subscriptions write, charges read |
| Payments (optional, instead of Stripe) | `paddle` | A Paddle Billing API key with only the permissions in "Paddle setup" below |
| Payments (optional) | `lemonsqueezy` | A Lemon Squeezy API key and the store's number (see below) |
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
## Intercom (instead of Frappe + IMAP)

Use this when your customers write to you through Intercom conversations.

1. **Make a private app in YOUR workspace.** Intercom > Settings > Integrations >
   Developer Hub > New app (internal). A private app reads only your own workspace,
   so it needs no review by Intercom. Under Configure > Authentication, copy the
   Access Token into your secrets file (`ACME_INTERCOM_TOKEN=...`). Treat it like a password.
   Never paste another company's token into your settings: Intercom's terms require
   OAuth for that.
2. **Find the admin who will sign replies.** Run, with the token loaded:
   `curl -s https://api.intercom.io/me -H "Authorization: Bearer $ACME_INTERCOM_TOKEN" -H "Intercom-Version: 2.14"`
   and copy `id`. That admin needs an inbox seat. Use a real teammate, not a bot:
   Intercom does not email bot replies to customers by default.
3. **Settings:**
   ```toml
   [helpdesk]
   adapter = "intercom"
   token_env = "ACME_INTERCOM_TOKEN"
   admin_id = "1234567"
   region = "us"          # us | eu | au: where your workspace is hosted
   backfill_hours = 24    # the first run reads conversations updated in this window

   [mailbox]
   adapter = "intercom"   # the receipt re-reads the reply part in Intercom
   ```
4. **Read-only proof.** `sayso run intake` with a scratch `state.dir`. Start a
   conversation from your own test contact in the Messenger first, and check the
   card shows its verbatim text.
5. **One real reply to yourself.** Approve the draft and run `sayso run sender`.
   It must end `SENT_AND_VERIFIED`. In Intercom the reply shows as a message from
   your admin and ends with a `Ref: sayso-...` line; that line is how a retry finds
   the earlier reply, so do not remove it.

Limits: every request pins `Intercom-Version: 2.14`. Group conversations (more
than one customer) are refused, never answered. Intercom has no Sent folder, so
the receipt is a second read of Intercom, not an independent system.
## Crisp as the helpdesk

Use this when your team answers customers in Crisp. Set BOTH `[helpdesk]` and `[mailbox]` to `crisp`.

1. Find the website id: Crisp app > Settings > Workspace Settings > Setup Instructions.
2. Get a token keypair. Either:
   - **Website token** (one workspace, no Marketplace account): Settings > Workspace Settings >
     Advanced configuration > API Token > Generate Token. Only an owner can do this. Use `tier = "website"`.
   - **Plugin token**: Crisp Marketplace (a separate account) > Plugins > New Plugin (Private) > Tokens.
     The Development token works on your own trusted workspace. A Production token needs the scopes
     `website:conversation:sessions` (read), `website:conversation:messages` (read + write) and
     `website:people:conversations` (read), and Crisp reviews the request. Install the plugin on your
     website with the private install link. Use `tier = "plugin"` (the default).
3. Put both halves in the secrets file (`ACME_CRISP_ID=...`, `ACME_CRISP_KEY=...`) and add:
   ```toml
   [helpdesk]
   adapter = "crisp"
   website_id = "<your website id>"
   tier = "website"
   identifier_env = "ACME_CRISP_ID"
   key_env = "ACME_CRISP_KEY"

   [mailbox]
   adapter = "crisp"
   website_id = "<your website id>"
   tier = "website"
   identifier_env = "ACME_CRISP_ID"
   key_env = "ACME_CRISP_KEY"
   ```
4. Prove it read-only (step 5 above): write to your own chatbox from a test email address, run
   `sayso run intake`, and check the card. The first run reads the last 7 days (`start_days`).
5. One real reply to yourself (step 6 above). It must end `SENT_AND_VERIFIED`, and the conversation in
   Crisp must show exactly one operator message.

Crisp is both the record and the send receipt, so the mailbox check is weaker than an IMAP Sent folder
(see [ADAPTERS.md](ADAPTERS.md)).
## Paddle setup (instead of Stripe)

Sayso uses Paddle Billing API v1 (not Paddle Classic). One payments provider per product.

1. Start in the **sandbox** (sandbox-vendors.paddle.com). Make a sandbox account, a product, a price and one
   completed test checkout, so there is a transaction (`txn_...`) and a subscription (`sub_...`) to work on.
2. Paddle > Developer tools > Authentication > API keys > New API key. Name it `Sayso`, set an expiry, and tick
   ONLY: Transactions read, Adjustments read, Adjustments write, Subscriptions read, Subscriptions write
   (Customers read too, if a host checks who owns a transaction). Copy it: Paddle shows it once.
   Legacy keys (made before May 2025, no `pdl_` prefix) are refused, because they cannot be limited.
3. Put it in the secrets file (`ACME_PADDLE_KEY=pdl_sdbx_apikey_...`) and in the TOML:
   ```toml
   [payments]
   adapter = "paddle"
   api_key_env = "ACME_PADDLE_KEY"
   environment = "sandbox"   # "live" for a pdl_live_ key
   ```
4. Money specs name Paddle ids: a refund's `charge` is the transaction id (`txn_...`), `customer` is `ctm_...`,
   a cancel's `subscription` is `sub_...`. A cancel takes effect at the end of the billing period unless the
   spec has `"effective": "immediately"`.
5. Prove it on the sandbox: approve one small refund on your test transaction and run `sayso run money`.
   It shows `PENDING_WITH_PROVIDER` until Paddle approves it (sandbox: about every ten minutes), then
   `VERIFIED_WITH_PROVIDER`. Only then switch to a live key.

Paddle notes: most live refunds wait for Paddle's approval, and a transaction with a refund waiting cannot
get another one. Paddle has no idempotency header; the adapter writes `sayso-ref:<hash>` into the refund
reason and looks for it before it creates a refund. A 429 blocks the server's IP for 60 seconds; the
adapter does not retry inside a run.
### Lemon Squeezy payments

1. In Lemon Squeezy, switch the dashboard to **Test mode** and open
   Settings > API. Create an API key (keys made in test mode see only test data;
   every key expires after a year). Put it in the secrets file as
   `ACME_LEMONSQUEEZY_KEY=...`.
2. Find the store's number: Settings > Stores, or
   `curl -s https://api.lemonsqueezy.com/v1/stores -H 'Accept: application/vnd.api+json' -H "Authorization: Bearer $ACME_LEMONSQUEEZY_KEY"`
   and read `data[].id`.
3. In the settings file:
   ```toml
   [payments]
   adapter = "lemonsqueezy"
   api_key_env = "ACME_LEMONSQUEEZY_KEY"
   store_id = "12345"
   test_mode = true        # remove (or set false) when you switch to a live key
   ```
4. Money specs use prefixed ids: `ls_order_<order id>` for refunds,
   `ls_sub_<subscription id>` for cancellations and `ls_cust_<customer id>` for the
   customer. A bare number or a Stripe id is refused.
5. Prove it with one test purchase (card 4242 4242 4242 4242), approve a small
   partial refund of that test order, and check the order in Lemon Squeezy shows
   the refunded amount. A Lemon Squeezy key has no scopes: it can do anything the
   store owner can, so keep `refund_max_minor` low.
## Slack instead of Discord

Set `[board] adapter = "slack"` to keep the cases in one Slack channel.

1. At https://api.slack.com/apps press **Create New App** > **From scratch**, name it Sayso, pick
   your workspace.
2. **OAuth & Permissions** > **Bot Token Scopes**: add `chat:write`, `reactions:read`, `users:read`
   and `channels:history` (`groups:history` too for a private channel). Add no user scopes.
3. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`) into your secrets
   file as `ACME_SLACK_TOKEN=...`. Do not turn on token rotation: the adapter reads one fixed token.
4. In Slack, make a channel for cases (for example `#support-cases`), type `/invite @Sayso` in it,
   and copy its id (channel name > **About** > Channel ID, `C...`).
5. In the settings file:
   ```toml
   [board]
   adapter = "slack"
   channel_id = "C0123456789"
   alerts_channel_id = "C0123456790"   # optional; defaults to channel_id
   token_env = "ACME_SLACK_TOKEN"
   ```
6. `[operators]` holds Slack user ids (profile > **...** > **Copy member ID**, `U...`), for example
   `"U0123ABCD" = "Pat"`. Only these people's reactions count.
7. Votes are reactions on the vote reply in each case thread: `:white_check_mark:` approves,
   `:x:` declines. Any `:x:` from an operator declines. Run `sayso run health` and check the
   alerts channel, then do step 6 of the step-by-step above.

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
