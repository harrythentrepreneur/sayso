"""Small-fix release: an optional policy that lets a small, QA-passed fix merge with no merge vote.

Ported from the PhonicsMaker loop (owner rule, 27 Sep 2026). OFF by default
(``[policy] small_fix_release = false``): with it off, every merge needs an
operator vote exactly as before.

When it is on, a case's PR set may merge on QA's pass alone only if ALL hold:

* QA is configured and passed the exact head of every PR in the set;
* every PR's changed-file list and change size can be read (unreadable keeps the vote);
* no changed path matches ``small_fix_sensitive_paths`` (money, login, secrets,
  migrations, CI and deploy by default; each product sets its own);
* the whole set changes at most ``small_fix_max_files`` files and
  ``small_fix_max_lines`` lines (added + deleted), counted together.

A UI change already needs real-app EVIDENCE before QA can pass (``qa_ui_paths``),
so that rule carries over unchanged.

The release job re-checks all of this at the exact head right before merging.
If the policy was switched off or the change is no longer small, the policy
release is withdrawn and a normal operator vote opens instead. The policy only
ever covers the MERGE. The customer reply, money and closing stay vote-gated.
"""
from __future__ import annotations

import re

from sayso import pr_refs



def facts_for(ctx, refs, heads: dict[str, str]):
    """[(ref, PrFacts or None)] for the exact recorded head of each PR. None = unreadable."""
    out = []
    for ref in refs:
        try:
            facts = pr_refs.host(ctx, ref).pr_facts(pr_refs.number(ref), heads[str(ref)])
        except Exception:  # noqa: BLE001 - unreadable keeps the vote
            facts = None
        out.append((ref, facts))
    return out


def why_not(ctx, refs, heads: dict[str, str]) -> str:
    """'' when this PR set may merge with no vote; otherwise the reason it may not."""
    policy = ctx.config.policy
    if not policy.small_fix_release:
        return "small-fix release is off"
    pattern = re.compile(policy.small_fix_sensitive_paths)
    files, lines = 0, 0
    for ref, facts in facts_for(ctx, refs, heads):
        if facts is None or not facts.files or any(not f for f in facts.files):
            return f"changed files unreadable for PR {ref}"
        if facts.changed_lines is None:
            return f"change size unreadable for PR {ref}"
        hit = next((f for f in facts.files if pattern.search(f)), None)
        if hit:
            return f"touches a sensitive path ({hit})"
        files += len(facts.files)
        lines += facts.changed_lines
    if files > policy.small_fix_max_files:
        return f"{files} files changed (small-fix limit {policy.small_fix_max_files})"
    if lines > policy.small_fix_max_lines:
        return f"{lines} lines changed (small-fix limit {policy.small_fix_max_lines})"
    return ""


def size_line(ctx, refs, heads) -> str:
    rows = [f for _, f in facts_for(ctx, refs, heads) if f is not None]
    return (f"{sum(len(f.files) for f in rows)} files, {sum(f.changed_lines or 0 for f in rows)} lines, "
            "no sensitive paths")
