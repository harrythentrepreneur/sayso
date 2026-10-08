"""Shared refusal rules. Every executor calls these; a refusal never acts.

These are deliberately conservative text checks. They are a second line of
defence, not the approval itself: the approval is the vote.
"""
from __future__ import annotations

import re

_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_PAYMENT_ID = re.compile(r"\b(?:cus|sub|pi|ch|py|in|re|cs)_[A-Za-z0-9]{6,}\b")
_SECRET = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]+|\bghp_[A-Za-z0-9]{20,}|\bxox[abp]-[A-Za-z0-9-]+")

# Past-tense money claims. A reply must not tell a customer money moved unless
# a money vote for that case has a verified provider receipt.
_MONEY_DONE = {
    "refund": re.compile(r"\b(?:refunded|reimbursed|(?:processed|issued|sent|made)\s+(?:a|the|your|you\s+a)?\s*"
                         r"(?:full\s+|partial\s+)?refund|refund\s+(?:has\s+been|was|is)\s+(?:issued|processed|sent|made))\b",
                         re.I),
    "cancel": re.compile(r"\b(?:cancell?ed\s+(?:your|the)\s+(?:subscription|plan|membership)"
                         r"|(?:subscription|plan|membership)\s+(?:has\s+been|was|is)\s+(?:now\s+)?cancell?ed)\b", re.I),
}
_NOT_DONE = re.compile(r"\b(?:will|would|can|could|may|might|once|if|after|when|going\s+to|happy\s+to)\b[^.!?\n]{0,40}$",
                       re.I)


class Refused(RuntimeError):
    """A safety rule refused the action. Nothing was done."""


def check_no_payment_identifiers(text: str) -> None:
    if _CARD.search(text) or _PAYMENT_ID.search(text):
        raise Refused("text carries a card number or payment-provider id")
    if _SECRET.search(text):
        raise Refused("text carries something that looks like a credential")


def has_secret(text: str) -> bool:
    return bool(_SECRET.search(text or ""))


def money_claims(text: str) -> set[str]:
    """Which money actions the text claims are already DONE."""
    found = set()
    for kind, pattern in _MONEY_DONE.items():
        for m in pattern.finditer(text):
            sentence_start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            if _NOT_DONE.search(text[sentence_start:m.start()]):
                continue
            found.add(kind)
    return found


def check_reply_text(text: str) -> None:
    if not text.strip():
        raise Refused("empty reply")
    check_no_payment_identifiers(text)


_RE_PREFIX = re.compile(r"^(?:\s*re\s*:\s*)+", re.I)


def reply_subject(title: str) -> str:
    """Exactly one "Re: " in front of the case title, however many it already has."""
    rest = _RE_PREFIX.sub("", title or "").strip()
    return "Re: " + (rest or "(no subject)")


def check_plugins(ctx, case: dict, text: str) -> None:
    """Product checks from plugins (``check_reply`` hook). A returned reason refuses the text."""
    for plugin in getattr(ctx, "plugins", None) or []:
        hook = getattr(plugin, "check_reply", None)
        if hook is None:
            continue
        reason = hook(ctx, case, text)
        if reason:
            raise Refused(f"{type(plugin).__name__}: {reason}")


def check_claims_backed(text: str, verified_money: set[str]) -> None:
    """A reply may say money moved only when a verified receipt says so."""
    unbacked = money_claims(text) - verified_money
    if unbacked:
        raise Refused(f"reply claims {sorted(unbacked)} but no verified money receipt exists for this case")
