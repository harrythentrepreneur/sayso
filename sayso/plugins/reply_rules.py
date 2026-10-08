"""Example plugin: refuse reply drafts that break a product's own text rules.

The core checks money claims and payment identifiers. A product often has more
rules: never promise a date, never name an internal tool, never mention a
retired plan. This plugin refuses any draft that matches a named pattern.

It runs through the ``check_reply`` hook, which the draft job calls BEFORE a
vote opens and the sender calls again before it sends. So an operator is never
asked to approve a reply the sender would then refuse.

Options (settings file):
  forbid   table of rule name -> regular expression, matched case-insensitively
"""
from __future__ import annotations

import re


class ReplyRules:
    def __init__(self, options: dict):
        rules = options.get("forbid") or {}
        if not rules:
            raise ValueError("reply_rules plugin needs at least one forbid pattern")
        self.rules = {name: re.compile(rx, re.I) for name, rx in rules.items()}

    def check_reply(self, ctx, case, text):
        for name, rx in self.rules.items():
            if rx.search(text):
                return f"draft breaks the {name!r} rule"
        return None


def make(options: dict) -> ReplyRules:
    return ReplyRules(options)
