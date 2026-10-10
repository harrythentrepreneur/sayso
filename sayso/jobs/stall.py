"""Stall checks: nothing sits in the loop unnoticed.

Three checks, ported from the PhonicsMaker loop's stall guard (Kaviru, 27 Sep - 9 Oct 2026):

1. **Vote reminders.** A vote open longer than ``vote_remind_minutes`` gets one
   reminder, then at most one more per ``stall_repeat_hours``.
2. **Stall limits.** Every open stage has a time limit on card activity (the
   newest message in the card). Past it the case is flagged once, then at most
   once per ``stall_repeat_hours`` while the same quiet stretch lasts. Any new
   message in the card resets the clock.
3. **Nobody is working on it.** An open card with nothing set to move it (no
   open vote, no draft due, no dev or QA run, no runner configured) is flagged
   after ``orphan_minutes`` of quiet, once per case and stage per day.

Every notice goes to the alerts channel and pings only the case's owner (a vote
for anyone pings nobody). It never names a customer address. Cards carrying the
``parked_label`` get no stall or orphan notice; their votes still get reminders.

This job only reads and notifies. It never moves a stage, opens or closes a vote,
starts work or sends anything. Its own notices are not card messages, so they can
never reset the clock they measure. An unreadable card is reported as unreadable,
never guessed.
"""
from __future__ import annotations

from datetime import timedelta

from sayso import cases, owners, stages, votes
from sayso.store import load_json, locked, parse_iso, save_json

WHY_STALL = {
    stages.NEW: "support has not picked it up",
    stages.IN_SUPPORT: "support has not finished it",
    stages.IN_DEV: "no dev result or PR yet",
    stages.IN_QA: "QA has not finished",
    stages.AWAITING: "a decision in the card is waiting",
    stages.BLOCKED: "it is blocked and nobody has moved it",
}


def limits(ctx) -> dict[str, int]:
    p = ctx.config.policy
    return {stages.NEW: p.stall_new_hours, stages.IN_SUPPORT: p.stall_support_hours,
            stages.IN_DEV: p.stall_dev_hours, stages.IN_QA: p.stall_qa_hours,
            stages.AWAITING: p.stall_awaiting_hours, stages.BLOCKED: p.stall_blocked_hours}


def _sent_path(ctx):
    return ctx.paths.root / "stall.json"


def _subject(rec: dict) -> str:
    return f"{rec['customer'].split('@')[0]} - {rec['title']}"


def _notice(ctx, sent: dict, ident: str, rec: dict, text: str) -> bool:
    """Send one notice for ``ident`` unless it already went out. Records it durably first."""
    if ident in sent:
        return False
    owner = owners.info(rec)["owner"]
    mention = owner if owners.name_of(ctx, owner) else None
    sent[ident] = ctx.clock.now().isoformat()
    save_json(_sent_path(ctx), sent)             # intent first: a crash never double-pings
    ctx.notify(f"stall:{ident}", owners.label(ctx, owner) + text, mention=mention)
    return True


def why_not_moving(ctx, key: str, rec: dict, stage: str) -> str | None:
    """None when something is set to move this case; otherwise the reason nothing is."""
    if votes.live_for_case(ctx, key):
        return None                                  # a vote is open or approved and queued
    if stage == stages.NEW:
        return "intake has not moved it to support"
    if stage == stages.IN_SUPPORT:
        draft_due = (not rec.get("dev_requested")
                     and (not rec.get("draft_version") or rec.get("needs_draft")))
        if draft_due and ctx.drafter is not None:
            return None
        return "no draft is due and no vote is open"
    if stage == stages.IN_DEV:
        if ctx.dev_runner is None:
            return "no dev runner is configured"
        if rec.get("dev_run") == "starting":
            return "a dev start was interrupted"
        return None                                  # running, or waiting for a free slot
    if stage == stages.IN_QA:
        if ctx.qa_runner is None:
            return None if (ctx.dev_runner is not None and rec.get("prs")) else "no QA runner is configured"
        qa = rec.get("qa") or {}
        if not rec.get("pr") or qa.get("run") in ("limited", "starting"):
            return "QA cannot continue on its own"
        if int(qa.get("red_tries") or 0) >= ctx.config.policy.red_proof_max_tries and qa.get("red") != "red-proved":
            return "the fails-on-old-code check gave up"
        return None
    if stage == stages.AWAITING:
        return "the card says awaiting approval but no vote is open"
    return None                                      # Blocked waits for a human by design


def run(ctx) -> dict:
    out = {"reminded": [], "stalled": [], "orphans": [], "unreadable": []}
    if ctx.paused:
        ctx.beat("stall", "paused")
        return {"paused": True}
    now = ctx.clock.now()
    policy = ctx.config.policy
    repeat = timedelta(hours=policy.stall_repeat_hours)
    with locked(_sent_path(ctx)):
        sent = load_json(_sent_path(ctx), default={})
        all_cases = cases.load(ctx)

        # 1. Vote reminders.
        remind = timedelta(minutes=policy.vote_remind_minutes)
        for vkey, v in sorted(votes.load(ctx).items()):
            if v["status"] != votes.OPEN:
                continue
            opened = parse_iso(v.get("opened_at"))
            if opened is None or now - opened < remind:
                continue
            period = 1 + int((now - opened - remind) / repeat)
            rec = all_cases.get(v["case_key"]) or {"customer": "", "title": v.get("subject", "")}
            wait = int((now - opened).total_seconds() // 60)
            text = (f"{v['subject']} - still waiting for a decision after "
                    f"{wait // 60}h {wait % 60}m: {v['question']}")
            if _notice(ctx, sent, f"vote:{vkey}:{period}", rec, text):
                out["reminded"].append(vkey)

        # 2 and 3. Stall limits and cards nobody is working on.
        hours = limits(ctx)
        orphan_after = timedelta(minutes=policy.orphan_minutes)
        for key, rec in sorted(all_cases.items()):
            if rec.get("folded_into") or not rec.get("card_id"):
                continue
            try:
                tags = ctx.board.get_tags(rec["card_id"])
                stage = stages.stage_of(tags)
            except Exception:  # noqa: BLE001 - reconcile reports bad tags
                continue
            if stage not in hours or policy.parked_label in tags:
                continue
            try:
                at = parse_iso(ctx.board.last_activity(rec["card_id"]))
            except Exception:  # noqa: BLE001
                at = None
            if at is None:
                ctx.alert(f"stall-unreadable:{key}:{now.date().isoformat()}",
                          f"{_subject(rec)}: cannot read the card's last activity; stall check skipped")
                out["unreadable"].append(key)
                continue
            idle = now - at
            limit = timedelta(hours=hours[stage])
            if idle >= limit:
                period = 1 + int((idle - limit) / repeat)
                text = (f"{_subject(rec)} - {stage}, no activity for {int(idle.total_seconds() // 3600)}h: "
                        f"{WHY_STALL[stage]}.")
                if _notice(ctx, sent, f"stall:{key}:{stage}:{at.isoformat()}:{period}", rec, text):
                    out["stalled"].append(key)
            if idle >= orphan_after:
                why = why_not_moving(ctx, key, rec, stage)
                if why:
                    text = (f"{_subject(rec)} - {stage}, nothing is moving it: {why}. "
                            f"Quiet for {int(idle.total_seconds() // 60)} min.")
                    if _notice(ctx, sent, f"orphan:{key}:{stage}:{now.date().isoformat()}", rec, text):
                        out["orphans"].append(key)
    ctx.beat("stall", str({k: len(v) for k, v in out.items()}))
    return out
