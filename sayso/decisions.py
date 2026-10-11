"""What a drafter's answer decides, beyond the reply text itself.

A drafter answers in one of three shapes, read only from WHOLE lines at the
very start or very end of its output, never from the middle of a reply:

* ``NO-REPLY: <reason>`` as the first non-blank line, and nothing else after it
  but optional notes: no reply is owed (spam, an invite, a bare thank-you). It
  becomes ONE close vote; approved, the card goes to Done with nothing sent.
* A reply followed by one or more trailing ``MONEY: {json spec}`` lines: the
  reply needs a refund or cancellation first. Each spec becomes a money vote
  and the reply is HELD until every one is verified with the provider. Then
  the same reply text gets its own reply vote.
* Anything else is a plain reply.

The MONEY lines are stripped from the reply and never sent. Malformed or empty
decisions are refused here; the draft job holds them for a human.
"""
from __future__ import annotations

import json
import re

NO_REPLY = re.compile(r"\ANO-REPLY:[ \t]*(.*)\Z")
MONEY = re.compile(r"\AMONEY:[ \t]*(.*)\Z")


class DecisionError(ValueError):
    pass


def parse(text: str) -> dict:
    """{"kind": "reply"|"no_reply", "reply": str, "reason": str, "money": [spec, ...]}"""
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    m = NO_REPLY.match(first)
    if m:
        reason = m.group(1).strip()
        if len(reason) < 3:
            raise DecisionError("NO-REPLY needs a reason a person can check")
        return {"kind": "no_reply", "reply": "", "reason": reason[:500], "money": []}
    money = []
    while lines and MONEY.match(lines[-1].strip()):
        raw = MONEY.match(lines.pop().strip()).group(1)  # type: ignore[union-attr]
        try:
            spec = json.loads(raw)
        except ValueError:
            raise DecisionError("a MONEY line is not valid JSON") from None
        if not isinstance(spec, dict):
            raise DecisionError("a MONEY line must be one JSON object")
        money.insert(0, spec)
        while lines and not lines[-1].strip():
            lines.pop()
    reply = "\n".join(lines).strip()
    if any(MONEY.match(ln.strip()) for ln in lines):
        raise DecisionError("MONEY lines must come last, after the reply")
    if money and not reply:
        raise DecisionError("a money proposal needs the reply it unlocks")
    return {"kind": "reply", "reply": reply, "reason": "", "money": money}
