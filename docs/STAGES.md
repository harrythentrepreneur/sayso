# Stages and votes

## Tags

Stage tags (exactly one on every card):
`New`, `In support`, `In dev`, `In QA`, `Awaiting approval`, `Done`, `Blocked`.

`Waiting on customer` is the one deliberate extra. It is only valid together
with `Done`: our work is finished, but the customer's next email may reopen the card.
`Done` alone means no reply is expected.

Labels from `[product] labels` ride along. Every move computes the new list
from the **live** tags and keeps the labels. A move that dropped them would
silently un-flag a card.

## Legal moves

| From | To |
|---|---|
| New | In support, Blocked |
| In support | In dev, Awaiting approval, Done, Blocked |
| In dev | In QA, In support, Blocked |
| In QA | Awaiting approval, In dev, Blocked |
| Awaiting approval | Done, In support, In dev, Blocked |
| Done | In support |
| Blocked | In support, In dev, In QA, Awaiting approval |

Any other move raises an error. A new customer email reopens `Done`,
`Done + Waiting`, `Awaiting approval` and `Blocked` to `In support`.
Cards in `In dev` and `In QA` keep their stage; the email is added to the card.

Every tag write is **read back**. If the board reports success but the tags did
not change, the job stops with an error.

## Life of a case

```
email ─► New ─► In support ─► (draft) Awaiting approval ─► (reply sent) Done + Waiting on customer
                   │                                                      │ quiet N days, close vote Yes
                   │                                                      ▼
                   │                                                     Done
                   └─► In dev ─► (PR) In QA ─► (QA PASS) Awaiting approval (merge vote) ─► merged ─► In support ─► new draft ...
                         ▲              │ QA BLOCKED (round < max_dev_rounds)
                         └──────────────┘ last round blocked ─► Blocked (a human decides)
```

## Votes

Each vote is one Yes/No poll on the card, about one exact thing:

| Kind | Opened by | Bound to (fingerprint) | Carried out by |
|---|---|---|---|
| reply | draft job | SHA-256 of the draft file | sender |
| money | `sayso money-request` | the full money spec | money job |
| merge | qa job (dev job when no QA runner is set) | the PR's head SHA and open state | release job |
| close | close job | the time of our last sent reply | close job |

Statuses: `open` → `approved` / `declined` / `expired` / `void` → `done`.

- Only `[operators]` decide. **Any operator No declines.** Otherwise one operator Yes approves.
- `void`: the thing changed after the vote opened (a draft edit, a new PR
  head, the customer wrote again, the card was handed to dev). Nothing is done.
  Ask again with a new vote.
- `expired`: no operator answered within `vote_expiry_hours`. Nothing is done.
- The poll subject must name the person or the plain problem
  ("Pat - charged twice"), never a bare number ("case 1921"). An operator reads
  it cold, with no other context.

## QA

When `[qa_runner]` is set, a PR waits in `In QA` until all of these pass on its
exact head (jobs/qa.py):

1. **Pre-QA gate** (script): CI green, a test file added or changed, no secret and
   no real-looking email address in the added lines. CI still running = wait.
   CI red = stays `In QA` with a note. Any other failure = back to `In dev`.
2. **Fails on the old code**: the PR's own tests pass on the head and at least one
   fails on the base. Tests that pass either way go back to `In dev`. A check that
   cannot run is retried `red_proof_max_tries` times, then a human is alerted.
3. **Independent QA run** by a different agent, briefed with the card. It must end
   with `VERDICT: PASS <head>` and answer `CUSTOMER-FIX: YES`. A UI change
   (`qa_ui_paths`) must also give an `EVIDENCE:` line.
4. **Rounds**: BLOCKED returns the card to `In dev`; QA's reason is on the card,
   so the next dev brief carries it. After `max_dev_rounds` the card goes to
   `Blocked`. A provider usage limit is neither a fail nor a round.

A new head starts the steps again. The release job merges only a head QA passed.
Without a QA runner the merge vote opens straight away and the card says the PR
was not independently checked.
