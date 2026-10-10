"""A PR is either a number in the configured repository or ``owner/repo#N``.

Cross-repository refs are never guessed from a bare number. The code-host adapter
checks the configured allowlist before a network call. This keeps two PR #7s
separate in votes, QA and release receipts.
"""
from __future__ import annotations

import re

REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")


def number(ref) -> int:
    if isinstance(ref, int) and ref > 0:
        return ref
    if isinstance(ref, str):
        match = REF.fullmatch(ref)
        if match:
            return int(match.group(2))
    raise ValueError(f"invalid PR reference: {ref!r}")


def host(ctx, ref):
    return ctx.codehost.for_ref(ref) if isinstance(ref, str) else ctx.codehost


def read(ctx, ref):
    return host(ctx, ref).pull_request(number(ref))


def ident(ref) -> str:
    return str(ref).replace("/", "-").replace("#", "-")
