"""Tally: read every open poll and record the operators' decision.

Tally never acts. It only turns votes into a recorded status, re-checks that
the thing voted on has not changed since, and expires votes nobody answered.
Executors (sender, money, release, close) act only on APPROVED records.
"""
from __future__ import annotations

from datetime import timedelta

from sayso import votes, pr_refs
from sayso.store import parse_iso


def current_material(ctx, rec):
    """Re-read the thing a vote is bound to. None = unreadable = void."""
    kind, spec = rec["kind"], rec.get("spec") or {}
    try:
        if kind == "reply":
            import hashlib
            from pathlib import Path
            return {"sha256": hashlib.sha256(Path(spec["path"]).read_bytes()).hexdigest()}
        if kind == "merge":
            pr = pr_refs.read(ctx, spec["pr"])
            return {"head": pr.head_sha, "state": pr.state}
        return rec.get("spec_material", spec)
    except Exception:  # noqa: BLE001 - unreadable is never approved
        return None


def merge_ballots(board: dict[str, str], web: dict[str, str]) -> dict[str, str]:
    """One ballot per person across Discord and the console. If the same person said
    Yes in one place and No in the other, No wins: a doubt is never an approval."""
    out = dict(board)
    for uid, answer in web.items():
        out[uid] = "no" if "no" in (answer, out.get(uid)) else answer
    return out


def _mirror_web_votes(ctx, rec: dict, web: dict[str, str]) -> None:
    """Show each console vote on the board card, once per person per answer."""
    for uid, answer in sorted(web.items()):
        who = ctx.config.operators.get(uid, uid)
        ctx.board.post(rec["card_id"], f"{who} voted {'Yes' if answer == 'yes' else 'No'} from the console: "
                       f"{rec['question']}", idempotency_key=f"webvote:{rec['key']}:{uid}:{answer}")


def _close_poll(ctx, rec: dict) -> None:
    """End the board poll once decided, so nobody votes on a settled question. Optional per adapter."""
    close = getattr(ctx.board, "close_poll", None)
    if close is None:
        return
    try:
        close(rec["card_id"], rec["poll_id"])
    except Exception as exc:  # noqa: BLE001 - the decision stands; a stale poll is only cosmetic
        ctx.journal.append("poll_close_failed", key=rec["key"], error=type(exc).__name__)


def run(ctx) -> dict:
    counts = {"approved": 0, "declined": 0, "void": 0, "expired": 0, "open": 0}
    expiry = timedelta(hours=ctx.config.policy.vote_expiry_hours)
    for key, rec in sorted(votes.load(ctx).items()):
        if rec["status"] != votes.OPEN:
            continue
        material = current_material(ctx, rec)
        if material is None or not votes.still_bound(rec, material):
            votes.set_status(ctx, key, votes.VOID, void_reason="the thing voted on changed or cannot be read")
            ctx.board.post(rec["card_id"], "Vote voided: what it approved has changed. Nothing was done.",
                           idempotency_key=f"void:{key}")
            counts["void"] += 1
            continue
        result = ctx.board.read_poll(rec["card_id"], rec["poll_id"])
        from sayso.console import web_votes_for  # console Yes/No, bound to this exact fingerprint
        web = web_votes_for(ctx, rec)
        ballots = merge_ballots(result.votes, web)
        _mirror_web_votes(ctx, rec, web)
        votes.record_ballots(ctx, key, {uid: {"answer": a, "via": "console" if uid in web and web[uid] == a
                                              else "board"} for uid, a in ballots.items()})
        status, by, opinions = votes.decide(ballots, ctx.config.operators)
        if status == votes.OPEN:
            if ctx.clock.now() - parse_iso(rec["opened_at"]) > expiry:
                votes.set_status(ctx, key, votes.EXPIRED)
                ctx.board.post(rec["card_id"], "Vote expired with no operator answer. Nothing was done.",
                               idempotency_key=f"expired:{key}")
                counts["expired"] += 1
            else:
                counts["open"] += 1
            continue
        decider = next((uid for uid, name in ctx.config.operators.items() if name == by and uid in ballots), None)
        via = "console" if decider in web and web[decider] == ballots.get(decider) else "board"
        votes.set_status(ctx, key, status, decided_by=by, decided_at=ctx.clock.now().isoformat(),
                         opinions=opinions, decided_via=via)
        word = "approved" if status == votes.APPROVED else "declined"
        where = " (from the console)" if via == "console" else ""
        ctx.board.post(rec["card_id"], f"{by} {word}{where}: {rec['question']}", idempotency_key=f"decided:{key}")
        _close_poll(ctx, rec)
        counts[word] += 1
    ctx.beat("votes", str(counts))
    return counts
