"""Draft: write a reply for each card in support, store it, and open a vote.

The draft file is immutable once written (a new draft is a new version), and
the vote binds to its SHA-256. The drafter's output is treated as data: a
draft that claims money moved when no money vote exists is refused here and
again by the sender. The draft job runs the sender's own ``check_sendable``,
so the two can never disagree about what may go out.
"""
from __future__ import annotations

import hashlib

from sayso import cases, safety, stages, votes
from sayso.jobs import sender


def draft_path(ctx, case_key: str, version: int):
    return ctx.paths.drafts / case_key / f"reply-v{version}.txt"


def run(ctx) -> dict:
    ctx.require_running()
    made = 0
    held = []
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or not rec.get("card_id"):
            continue
        tags = ctx.board.get_tags(rec["card_id"])
        if stages.stage_of(tags) != stages.IN_SUPPORT:
            continue
        if rec.get("dev_requested") or votes.live_for_case(ctx, key, "reply"):
            continue
        if rec.get("draft_version") and not rec.get("needs_draft"):
            continue
        text = ctx.drafter.draft({"case_key": key, **rec}, ctx.board.messages(rec["card_id"])).strip()
        try:
            sender.check_sendable(ctx, key, rec, rec["customer"], text)
        except safety.Refused as exc:
            held.append(key)
            ctx.board.post(rec["card_id"], f"Draft held, not offered for a vote: {exc}",
                           idempotency_key=f"draft-held:{key}:{hashlib.sha256(text.encode()).hexdigest()[:12]}")
            continue
        version = int(rec.get("draft_version") or 0) + 1
        path = draft_path(ctx, key, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise safety.Refused(f"{path} already exists; drafts are never overwritten")
        path.write_text(text + "\n", encoding="utf-8")
        body = path.read_text(encoding="utf-8")
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        cases.update(ctx, key, draft_version=version, needs_draft=False)
        ctx.board.post(rec["card_id"], f"**Draft reply v{version}** (not sent)\n\n{body}",
                       idempotency_key=f"draft:{key}:v{version}")
        votes.open_vote(ctx, key=f"reply:{key}:v{version}", kind="reply", case_key=key, card_id=rec["card_id"],
                        subject=cases.subject(rec),
                        question=f"send draft reply v{version} as written?",
                        identity={"case_key": key, "ticket": rec.get("ticket"), "recipient": rec["customer"],
                                  "path": str(path)},
                        material={"sha256": body_sha},
                        spec={"path": str(path), "sha256": body_sha, "version": version})
        cases.move(ctx, rec["card_id"], stages.AWAITING)
        made += 1
    result = {"drafted": made, "held": held}
    ctx.beat("draft", str(result))
    return result
