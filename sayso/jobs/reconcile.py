"""Reconcile: compare the loop's records with the live board. Report, never repair.

A repair that guesses is worse than the drift it replaces, so this job only
raises alerts (once per finding). Findings:
  card-missing          a recorded card no longer exists on the board
  bad-tags              a card's live tags break the one-stage rule
  approved-not-sent     a reply vote approved more than N hours ago with no receipt
  unverified-attempt    a send or money attempt that was never proven
  awaiting-no-vote      a card in Awaiting approval with no live vote
"""
from __future__ import annotations

from datetime import timedelta

from sayso import cases, stages, votes
from sayso.store import load_json, parse_iso


def findings(ctx) -> list[tuple[str, str]]:
    out = []
    all_votes = votes.load(ctx)
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into"):
            continue
        try:
            tags = ctx.board.get_tags(rec["card_id"])
        except Exception:  # noqa: BLE001
            out.append((f"card-missing:{key}", f"card for {rec['title']!r} cannot be read"))
            continue
        try:
            stage = stages.stage_of(stages.validate(tags))
        except stages.StageError as exc:
            out.append((f"bad-tags:{key}", f"{rec['title']!r}: {exc}"))
            continue
        if stage == stages.AWAITING and not votes.live_for_case(ctx, key):
            out.append((f"awaiting-no-vote:{key}", f"{rec['title']!r} is Awaiting approval with no open vote"))
    limit = timedelta(hours=ctx.config.policy.unsent_alert_hours)
    for vkey, v in all_votes.items():
        if v["kind"] == "reply" and v["status"] == votes.APPROVED and v.get("decided_at"):
            if ctx.clock.now() - parse_iso(v["decided_at"]) > limit:
                out.append((f"approved-not-sent:{vkey}", f"{v['subject']!r}: approved reply still not sent"))
    for akey, att in load_json(ctx.paths.attempts).items():
        if att.get("status") == "UNVERIFIED":
            out.append((f"unverified-attempt:{akey}", f"{akey}: attempt never proven; needs a human check"))
    return out


def run(ctx) -> dict:
    found = findings(ctx)
    for key, text in found:
        ctx.alert(key, text)
    ctx.beat("reconcile", f"{len(found)} findings")
    return {"findings": [k for k, _ in found]}
