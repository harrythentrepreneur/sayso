# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## Reporting a vulnerability

**Do not open a public issue.** Report the problem privately through GitHub's
"Report a vulnerability" button (Security tab → Advisories), or contact the
maintainers directly.

Please include:
- the affected version or commit;
- the steps to reproduce, using fakes where possible;
- the impact: which guard is bypassed, and what could be sent, charged or merged.

We aim to acknowledge a report within 3 working days, and to agree a fix
timeline with you before anything is disclosed.

## What counts

The most serious class of bug is anything that lets the loop **send, charge,
refund, cancel or merge without an operator Yes on the exact revision**, or
that makes the loop record something as verified when it was not. The design
and its known limits are in [docs/SECURITY.md](docs/SECURITY.md) and
[docs/SAFETY.md](docs/SAFETY.md).
