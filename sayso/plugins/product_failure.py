"""Example plugin: group product-failure reports by an error SIGNATURE.

Modelled on the reference product's book-failure handling. When a product
emails support about a failed generation (or a customer pastes an error), the
message is claimed as its own kind, ``failure:<signature>``, so:

- each customer gets one card per distinct cause, not one per failed attempt;
- a failure never folds into the customer's billing conversation;
- an unrecognised error is NOT grouped - two different faults must never share
  one card and one reply.

Options (settings file):
  patterns   table of signature -> regular expression, matched case-insensitively
  label      board label to add (default "Product failure")
  subject_prefix  only messages whose subject starts with this are considered
"""
from __future__ import annotations

import re

from sayso.cases import UNGROUPABLE


class ProductFailure:
    def __init__(self, options: dict):
        pats = options.get("patterns") or {}
        if not pats:
            raise ValueError("product_failure plugin needs at least one pattern")
        self.patterns = {sig: re.compile(rx, re.I) for sig, rx in pats.items()}
        self.label = options.get("label", "Product failure")
        self.prefix = str(options.get("subject_prefix", "")).lower()

    def signature(self, text: str) -> str:
        for sig, rx in self.patterns.items():
            if rx.search(text):
                return sig
        return UNGROUPABLE

    def classify(self, ctx, message):
        if self.prefix and not message.subject.lower().startswith(self.prefix):
            return None
        sig = self.signature(message.body)
        kind = UNGROUPABLE if sig == UNGROUPABLE else f"failure:{sig}"
        return {"kind": kind, "labels": [self.label] if self.label in ctx.config.labels else []}


def make(options: dict) -> ProductFailure:
    return ProductFailure(options)
