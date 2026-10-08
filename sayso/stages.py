"""The case stage machine.

A card carries exactly ONE stage tag, plus optional labels. The one deliberate
exception: after a reply is sent, the card carries ``Done`` AND
``Waiting on customer`` - our action is complete, but their reply may reopen it.

Tags are a whole-list write on most boards, so every move is computed from the
LIVE tags and carries labels across. A move that dropped labels would silently
un-flag, for example, a high-value account.
"""
from __future__ import annotations

from typing import Iterable

NEW = "New"
IN_SUPPORT = "In support"
IN_DEV = "In dev"
IN_QA = "In QA"
AWAITING = "Awaiting approval"
DONE = "Done"
BLOCKED = "Blocked"
WAITING = "Waiting on customer"

STAGES = (NEW, IN_SUPPORT, IN_DEV, IN_QA, AWAITING, DONE, BLOCKED)
SYSTEM_TAGS = STAGES + (WAITING,)

ALLOWED: dict[str, frozenset[str]] = {
    NEW: frozenset({IN_SUPPORT, BLOCKED}),
    IN_SUPPORT: frozenset({IN_DEV, AWAITING, DONE, BLOCKED}),
    IN_DEV: frozenset({IN_QA, IN_SUPPORT, BLOCKED}),
    IN_QA: frozenset({AWAITING, IN_DEV, BLOCKED}),
    AWAITING: frozenset({DONE, IN_SUPPORT, IN_DEV, BLOCKED}),
    DONE: frozenset({IN_SUPPORT}),
    BLOCKED: frozenset({IN_SUPPORT, IN_DEV, IN_QA, AWAITING}),
}

REOPEN_FROM = frozenset({DONE, AWAITING, BLOCKED})


class StageError(ValueError):
    """An illegal tag set or an illegal move."""


def stage_of(tags: Iterable[str]) -> str:
    found = [t for t in tags if t in STAGES]
    if len(found) != 1:
        raise StageError(f"a card needs exactly one stage tag, found {found}")
    return found[0]


def is_waiting(tags: Iterable[str]) -> bool:
    return WAITING in list(tags)


def labels_of(tags: Iterable[str]) -> list[str]:
    return [t for t in tags if t not in SYSTEM_TAGS]


def validate(tags: Iterable[str]) -> list[str]:
    tags = list(tags)
    stage = stage_of(tags)
    if WAITING in tags and stage != DONE:
        raise StageError(f"'{WAITING}' is only valid together with '{DONE}'")
    return tags


def move(tags: Iterable[str], to: str, *, waiting: bool = False) -> list[str]:
    tags = validate(tags)
    current = stage_of(tags)
    if to not in ALLOWED[current]:
        raise StageError(f"illegal move {current!r} -> {to!r}")
    if waiting and to != DONE:
        raise StageError("only a move to Done may add Waiting on customer")
    return [to] + ([WAITING] if waiting else []) + labels_of(tags)


def reopen(tags: Iterable[str]) -> list[str]:
    """A new customer message returns a finished or pending card to In support."""
    tags = validate(tags)
    if stage_of(tags) in REOPEN_FROM or WAITING in tags:
        return [IN_SUPPORT] + labels_of(tags)
    return tags


def close_quiet(tags: Iterable[str]) -> list[str]:
    """Drop Waiting on customer after an approved quiet close."""
    tags = validate(tags)
    if stage_of(tags) != DONE or WAITING not in tags:
        raise StageError("only a Done + Waiting on customer card can be closed quietly")
    return [DONE] + labels_of(tags)
