# Plugins

A plugin holds logic only one product needs, so the shared loop stays small.
Plugins are off unless the settings file enables them.

## Shape

A module with `make(options) -> object`. The object may implement any of these hooks:

| Hook | Called by | Returns |
|---|---|---|
| `classify(ctx, message)` | intake, for each new customer message | `{"kind": str, "labels": [str]}` to claim the message, or `None` |
| `check_reply(ctx, case, text)` | draft (before the vote opens) and sender (before the send) | a reason string to refuse the draft, or `None` |
| `extra_jobs()` | `sayso run` | `{job_name: fn(ctx) -> dict}`: extra timer jobs |

`kind` decides grouping: one card per customer per kind. Pick kinds that mean
"the same problem", not "the same action to take".

## Enable one

```toml
[[plugins]]
name = "product_failure"            # sayso.plugins.product_failure
enabled = true
options = { patterns = { quota = "out of credit", timeout = "timed out" }, label = "Product failure" }
```

A plugin outside this repo uses its full module path: `name = "acme_loop.refunds"`.

## The example: `product_failure`

This plugin generalises the reference deployment's book-failure handling.
It claims messages whose body matches one of the named patterns, and gives
each signature its own card per customer. It follows two lessons:

- **An unrecognised error must not group.** It gets kind `unmatched`, which
  never matches an existing card. If unknown errors shared a card, two
  different faults would get one wrong reply.
- **Group on the cause, not the action.** "Retry" and "contact dev" are
  actions. Many causes share one action. Signatures come from the error text.

## The example: `reply_rules`

Refuses a draft that matches any named pattern, for product rules the core
does not know (a retired plan name, a promised date, a language check).

```toml
[[plugins]]
name = "reply_rules"
enabled = true
options = { forbid = { retired_plan = "lifetime plan", promise = "by tomorrow" } }
```

Because the draft job and the sender call the SAME check, a vote is never
opened on a reply the sender would refuse after the operator says Yes. The
reference deployment lost a round trip to exactly that: an approved draft was
refused at send time and needed a second draft and a second vote.

## Rules for plugin authors

- A plugin must not send, charge or merge. It may only classify, label, and
  add jobs that open votes. The executors stay in the core.
- Test the plugin against the product's REAL message corpus before you enable
  it. A word list that silences findings across your own data is off, not safe.
