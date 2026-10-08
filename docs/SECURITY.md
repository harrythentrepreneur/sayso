# Security

## Secrets

- Secrets never live in the settings file. `*_env` keys name environment
  variables. Anything else in an `_env` key is refused.
- Keep secrets in `~/.config/sayso/<slug>.env`, mode 600, loaded by the systemd
  unit's `EnvironmentFile`.
- Use the narrowest key each vendor offers: a Stripe restricted key, a GitHub
  fine-grained token for one repo, a Discord bot limited to two channels, and
  a Frappe API user with agent rights only.
- Drafts and replies are refused if they contain card numbers, payment-provider
  ids or credential-shaped strings.

## Data

- The helpdesk owns customer correspondence. The state directory holds case
  ids, the customer's address, drafts, and approval and receipt records. Treat
  it as customer data: mode 700, backed up, never committed to git (it is in
  `.gitignore`).
- The board shows the customer's email verbatim, so that operators read the
  customer's own words. Use a private board that only operators can see.
- Logs and the journal hold keys, statuses and ids, not message bodies.

## Threat model

| Threat | Mitigation |
|---|---|
| A customer email instructs the AI to refund, send or leak | Email is data. The drafter only drafts; every action needs an operator vote. |
| Someone who is not an operator votes Yes | Only `[operators]` ids decide; others are recorded as opinions. |
| A bot votes | Board adapters skip bot users when reading a poll. |
| A draft or spec is edited after approval | Fingerprints are re-checked; the vote goes void. |
| A replay or double run | Idempotency keys, per-job locks, the seen-message list, and pending attempts that are never re-executed. |
| A stolen settings file | It holds no secrets. |
| Shell access to the state directory | Out of scope for the loop. Protect it with OS permissions and host access control. See SAFETY.md, "What this does not protect against". |

## Reporting a problem

Report privately to the repository owners. Do not open a public issue for a
security problem.
