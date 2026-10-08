# FAQ

**Can it reply without a human?**
No. Every outbound reply needs an operator Yes on that exact draft. That is the product.

**Does it need an LLM?**
Only the `hermes` drafter and dev runner use one. Polling, tallies, sending,
verification and reconciliation are plain scripts, so an idle loop costs nothing
in model tokens. That does not make the whole system free: drafting and dev runs cost money.

**We use Zendesk / Help Scout / Slack / Linear. Can we use this?**
Yes, if you write one adapter per system. See [ADAPTERS.md](ADAPTERS.md). The
core, the rules and the tests do not change.

**Two people approve different answers at the same time?**
Any operator No declines. Otherwise the first recorded Yes approves. A vote
covers one revision, so a changed draft is a new vote.

**The customer replies while a reply vote is open.**
Intake voids the vote and returns the card to `In support`. A new draft that
answers the new email is made.

**Why a separate mailbox check if the helpdesk says Sent?**
A helpdesk can mark mail sent that was queued, bounced or sent twice. The
mailbox's own Sent folder is an independent witness. Two sources agreeing is the receipt.

**Why does it never retry a failed send?**
A timeout does not mean the email did not go. A blind retry sends a second
email to a customer. The loop re-reads for proof, then asks a human.

**Can it deploy?**
No. Release merges at the approved head SHA. Deployment belongs to the
product's own pipeline and its own approval.

**Where is the audit trail?**
`journal.jsonl` (every state change), `votes.json` (who decided what, when,
about which fingerprint), and `receipts/` (proof of each send).

**Windows or macOS?**
The code runs anywhere Python 3.11 runs, but file locks use `fcntl`, so
Windows is not supported. The timer templates are for systemd; on macOS, use
launchd to call `sayso run <job>`.
