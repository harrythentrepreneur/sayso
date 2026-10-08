"""Whose decision a case's votes are: one operator, or any of them.

A LABEL ONLY. Any operator can still approve or decline any vote; ``votes.decide``
never reads the owner. The label exists so the right person sees the right
question first and nobody else is pinged for it.

The owner is stored on the case record (cases.json) as ``owner`` (an operator id
or ``all``), ``owner_source``, ``owner_set_at`` and, for an override, ``owner_set_by``:

* ``sayso set-owner`` is an **override**: it wins over every inference and is only
  ever replaced by another override;
* otherwise the first operator to post in the card, read from its OLDEST message,
  is the owner (``first_operator_message``). It never changes after that;
* no operator message yet, a board that cannot report authors, or a card that
  cannot be read, is ``all``. A person is never guessed.

Ported from the PhonicsMaker loop (Kaviru, 7 Oct 2026).
"""
from __future__ import annotations

from typing import Any

from sayso import cases

ALL = "all"
OVERRIDE = "override"
FIRST_MESSAGE = "first_operator_message"
NO_OPERATOR = "no_operator_message"
UNREADABLE = "unreadable"


class OwnerError(ValueError):
    pass


def normalise(ctx, value: Any) -> str:
    """An operator id, an operator name (any case), or ``all``. Anything else is refused."""
    text = str(value or "").strip()
    if text.lower() in (ALL, "either", "any"):
        return ALL
    if text in ctx.config.operators:
        return text
    for uid, name in ctx.config.operators.items():
        if name.lower() == text.lower():
            return uid
    raise OwnerError(f"owner must be an operator id, an operator name or 'all', got {text!r}")


def info(rec: dict) -> dict[str, Any]:
    """The owner on a case record. A record without one (every case from before) reads as ``all``."""
    owner = str(rec.get("owner") or ALL)
    out = {"owner": owner, "owner_source": rec.get("owner_source"), "owner_set_at": rec.get("owner_set_at")}
    if rec.get("owner_set_by"):
        out["owner_set_by"] = rec["owner_set_by"]
    return out


def name_of(ctx, owner: str) -> str | None:
    return ctx.config.operators.get(owner) if owner != ALL else None


def label(ctx, owner: str) -> str:
    """``For Kaviru: `` or ``For any operator: ``. An owner who is no longer an operator reads as any."""
    name = name_of(ctx, owner)
    if name:
        return f"For {name}: "
    return "For either of you: " if len(ctx.config.operators) == 2 else "For any operator: "


def infer(ctx, authors) -> dict[str, Any]:
    """The first operator to post, from ``[(author_id, iso_time), ...]`` oldest first."""
    now = ctx.clock.now().isoformat()
    if authors is None:
        return {"owner": ALL, "owner_source": UNREADABLE, "owner_set_at": now}
    for author, at in authors:
        if str(author) in ctx.config.operators:
            return {"owner": str(author), "owner_source": FIRST_MESSAGE, "owner_set_at": at or now}
    return {"owner": ALL, "owner_source": NO_OPERATOR, "owner_set_at": now}


def for_vote(ctx, case_key: str) -> dict[str, Any]:
    """The owner to put on a vote about to open. Reads the board only while the owner is unknown."""
    rec = cases.load(ctx).get(case_key) or {}
    current = info(rec)
    if current["owner_source"] == OVERRIDE or current["owner"] != ALL:
        return current
    reader = getattr(ctx.board, "message_authors", None)
    if reader is None or not rec.get("card_id"):
        return current
    try:
        authors = reader(rec["card_id"])
    except Exception:  # noqa: BLE001 - an unreadable card never names a person
        authors = None
    found = infer(ctx, authors)
    if found["owner"] != ALL:
        _store(ctx, case_key, found)
    return found


def _store(ctx, case_key: str, value: dict) -> None:
    # Only two writers: for_vote (never for a case that has an override or a
    # person, see its early return) and set_override.
    cases.update(ctx, case_key, **{k: v for k, v in value.items() if v is not None})


def set_override(ctx, case_key: str, owner: Any, by: Any) -> dict[str, Any]:
    if case_key not in cases.load(ctx):
        raise OwnerError(f"unknown case {case_key}")
    setter = normalise(ctx, by)
    if setter == ALL:
        raise OwnerError("--by must name the operator who asked for this owner")
    value = {"owner": normalise(ctx, owner), "owner_source": OVERRIDE,
             "owner_set_at": ctx.clock.now().isoformat(), "owner_set_by": setter}
    _store(ctx, case_key, value)
    ctx.journal.append("owner_set", case_key=case_key, owner=value["owner"], by=setter)
    return value


def started_line(ctx, value: dict) -> str:
    """One line for the vote's card post: whose decision, and that anyone may still vote."""
    name = name_of(ctx, value["owner"])
    by = name_of(ctx, value.get("owner_set_by") or "") or "an operator"
    if value.get("owner_source") == OVERRIDE:
        return (f"{name}'s decision (set by {by}). Any operator can still vote." if name
                else f"For any operator (set by {by}).")
    if name:
        return f"Work started by {name}. Any operator can still vote."
    return "No operator has posted in this card yet, so this is for any operator."
