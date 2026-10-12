"""Tidy (opt-in, off by default): archive finished cards so the board shows only working cases.

Ported from the PhonicsMaker loop's board tidy (30 Sep and 7 Oct 2026). With
``[policy] archive_finished = false`` (the default) this job does nothing.

When it is on, a card is archived only when ALL hold:

* its stage is ``Done`` alone, or ``Done`` + ``Waiting on customer`` with no
  card activity for ``archive_waiting_days``;
* nothing was written in the card for ``archive_quiet_minutes`` (default 60);
* no live vote pins it. A vote opened BEFORE the case's last close no longer
  pins it: the close settled the case, and an old unanswered question must not
  keep a finished card on the board forever;
* it does not carry the ``parked_label`` tag.

Archiving HIDES the card. It never locks it, deletes a message or changes a
stage. Every stage move (``cases.set_tags_verified``) unarchives first, so a
new customer email or any operator move brings the card straight back. An
unreadable card is skipped, never guessed; an archive that does not read back
is an alert. This job never repairs anything else: reconcile stays report-only.
"""
from __future__ import annotations

from datetime import timedelta

from sayso import cases, stages, votes
from sayso.store import parse_iso


def _last_close(ctx, case_key: str):
    """Time of the case's newest completed close vote, or None."""
    times = [parse_iso(v.get("decided_at") or v.get("opened_at"))
             for v in votes.load(ctx).values()
             if v["case_key"] == case_key and v["kind"] == "close" and v["status"] == votes.DONE]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def pinned_by(ctx, case_key: str) -> list[str]:
    """Live votes that keep this card visible. Votes opened before the last close do not."""
    closed = _last_close(ctx, case_key)
    out = []
    for v in votes.live_for_case(ctx, case_key):
        opened = parse_iso(v.get("opened_at"))
        if closed is not None and opened is not None and opened < closed:
            continue
        out.append(v["key"])
    return out


def run(ctx) -> dict:
    policy = ctx.config.policy
    if not policy.archive_finished:
        ctx.beat("tidy", "off")
        return {"off": True}
    if ctx.paused:
        ctx.beat("tidy", "paused")
        return {"paused": True}
    if getattr(ctx.board, "set_archived", None) is None or getattr(ctx.board, "is_archived", None) is None:
        ctx.alert("tidy-unsupported", "archive_finished is on but this board cannot archive cards; nothing archived")
        ctx.beat("tidy", "unsupported")
        return {"unsupported": True}
    now = ctx.clock.now()
    quiet = timedelta(minutes=policy.archive_quiet_minutes)
    waiting = timedelta(days=policy.archive_waiting_days)
    out: dict[str, list] = {"archived": [], "kept": [], "unreadable": [], "failed": []}
    for key, rec in sorted(cases.load(ctx).items()):
        card = rec.get("card_id")
        if not card or rec.get("folded_into"):
            continue
        try:
            tags = ctx.board.get_tags(card)
            stage = stages.stage_of(tags)
            if ctx.board.is_archived(card):
                continue
        except Exception:  # noqa: BLE001 - reconcile reports bad cards
            continue
        if stage != stages.DONE or policy.parked_label in tags:
            continue
        try:
            at = parse_iso(ctx.board.last_activity(card))
        except Exception:  # noqa: BLE001 - unreadable is never "quiet"
            at = None
        if at is None:
            out["unreadable"].append(key)
            continue
        idle = now - at
        need = waiting if stages.is_waiting(tags) else quiet
        if idle < need or pinned_by(ctx, key):
            out["kept"].append(key)
            continue
        ctx.board.set_archived(card, True)
        if not ctx.board.is_archived(card):
            ctx.alert(f"tidy-failed:{key}", f"{rec['customer'].split('@')[0]} - {rec['title']}: the board did not "
                      "archive the finished card (read back visible)")
            out["failed"].append(key)
            continue
        ctx.journal.append("card_archived", case_key=key, card_id=card, stage=stage, idle_minutes=int(idle.total_seconds() // 60))
        out["archived"].append(key)
    ctx.beat("tidy", str({k: len(v) for k, v in out.items()}))
    return out

