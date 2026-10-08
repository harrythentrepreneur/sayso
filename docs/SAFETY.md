# Safety and approval model

## The one rule

No email is sent, no money moves and no code merges without a Yes from a named
operator on the **exact** revision. The executor re-checks that revision
immediately before it acts.

A vote never acts. Tally records it; a separate executor acts on it. Neither an
AI drafter, a customer email, nor a board reaction can approve anything.

## Guards and their proof

`scripts/mutate.py` copies the repo to a scratch directory, removes each guard
below one at a time, and requires a failing test. The run must print
`MUTATION VERDICT: PASS`. A guard that can be removed while every test still
passes is untested.

| Guard | Where | What it stops |
|---|---|---|
| Draft fingerprint re-checked before send | jobs/sender.py | Sending text nobody approved |
| Card must still be `Awaiting approval` | jobs/sender.py | Sending after someone moved the card |
| A `pending` attempt is re-read, never re-sent | jobs/sender.py | A second email after a crash |
| Receipt = one helpdesk Sent row to this recipient from our address, plus one mailbox Sent copy | jobs/sender.py | Calling a queued or misrouted email "sent" |
| No money claim without a verified money receipt | safety.py, jobs/sender.py | Telling a customer "we refunded you" when we did not |
| No card numbers, payment ids or credentials in replies | safety.py | Leaking payment data |
| A vote opens only on a draft the sender would send | jobs/sender.py `check_sendable`, jobs/draft.py | An approved reply refused after the Yes, costing a second draft and vote |
| No merge vote before independent QA passes the exact head | jobs/qa.py, jobs/dev.py, jobs/release.py | Merging a fix nobody but its author checked |
| A PR's tests must fail on the old code | jobs/qa.py | A green test that proves nothing |
| QA rounds are bounded; then a human decides | jobs/qa.py | An agent loop burning money on a fix that will not converge |
| Reply subject carries exactly one `Re:` | safety.py `reply_subject` | `Re: Re: ...` subjects on threads that already had one |
| Pause flag | runtime.py | Any action while an operator investigates |
| Only operators decide; any operator No declines | votes.py | A stranger or a bot approving |
| Poll subject must be human-readable | votes.py | An operator approving the wrong case |
| Material changed → vote void | jobs/tally.py | Approving one thing and doing another |
| A new customer email voids reply and close votes | jobs/intake.py | Answering or closing past what they just said |
| Our own addresses are never customers | jobs/intake.py | Loops and self-replies |
| Replayed mail is ignored | jobs/intake.py | Duplicate cards and posts after a restart |
| Tag writes read back | cases.py | Believing a stage moved when it did not |
| Unrecognised failures never group together | cases.py | Two different faults sharing one reply |
| Refund cap and currency list | money.py | Over-large or wrong-currency refunds |
| Money spec change → vote void | jobs/money_job.py | Refunding a different amount than approved |
| Refund verified by exact amount and currency | jobs/money_job.py | Calling a wrong refund "done" |
| A `pending` money attempt is never re-executed | jobs/money_job.py | A double refund after a crash |
| Dev brief must carry DO NOT MERGE / DEPLOY / CONTACT | jobs/dev.py | An unbounded coding run |
| One dev run per card | jobs/dev.py | Parallel runs on one case |
| Merge pinned to the approved head SHA | jobs/release.py | Merging code added after approval |
| Merge read back as merged | jobs/release.py | Telling a customer a fix shipped when it did not |
| A close is voided by any new customer mail, on any thread | jobs/close.py, helpdesk adapter | Closing on a customer who just wrote |
| Quiet window enforced | jobs/close.py | Closing early |
| Real adapters need `mode = "live"` | config.py | Touching real systems from a test config |
| Secrets only via environment variable names | config.py | Secrets committed in settings files |
| A corrupt state file stops the loop | store.py | Redoing work because the history looked empty |
| Labels survive every move | stages.py | Silently un-flagging a card |

## Fail closed

When a result is unknown (a timeout, an unreadable read-back, a missing tag),
the loop records `UNVERIFIED` or raises, and alerts once. It never guesses and
never retries an action that may already have happened. A human checks the
source system and decides.

## What this does not protect against

- Someone with shell access editing `votes.json` directly. The executors still
  re-check fingerprints, so an edited spec voids the vote. But a forged
  `approved` record with a matching fingerprint would be acted on. Protect the
  state directory with file permissions (see SECURITY.md).
- An operator approving without reading. The poll shows the subject and the
  question; the draft or spec is posted on the card above it.
- Vendor behaviour the reference adapters did not anticipate. Prove each
  adapter on your own account first (ONBOARDING.md, step 5).
