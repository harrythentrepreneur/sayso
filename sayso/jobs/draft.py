"""Draft: write a reply for each card in support, store it, and open a vote.

The draft file is immutable once written (a new draft is a new version), and
the vote binds to its SHA-256. The drafter's output is treated as data: a
draft that claims money moved when no money vote exists is refused here and
again by the sender. The draft job runs the sender's own ``check_sendable``,
so the two can never disagree about what may go out.

The drafter may also decide (``sayso/decisions.py``):

* **no reply owed** -> ONE close vote with its reason. Approved, the case closes
  with nothing sent and is never redrafted until the customer writes again.
  Declined, a real reply is drafted. A second no-reply for the same message is
  held for a human, never asked twice.
* **money first** -> a money vote per proposed refund/cancel, and the reply is
  HELD (stored, not voted). When every proposed action is verified with the
  provider, the SAME reply text gets its reply vote: no redraft. If an operator
  declines a proposed action, the held reply is dropped and a new draft is made.
"""
from __future__ import annotations

import hashlib

from sayso import cases, decisions, money, safety, stages, votes
from sayso.jobs import sender


def draft_path(ctx, case_key: str, version: int):
    return ctx.paths.drafts / case_key / f"reply-v{version}.txt"


def _subject(rec: dict) -> str:
    return f"{rec['customer'].split('@')[0]} - {rec['title']}"


def _hold(ctx, key: str, rec: dict, why: str, text: str) -> None:
    """Hold a draft for a person. The case then waits: it is NOT redrafted on the next pass (that would bill
    the drafter and post the same note every minute) until the customer writes again or a person asks for a
    new draft; both set needs_draft. ``held_draft`` records which inbound message the hold answered."""
    ctx.board.post(rec["card_id"], f"Draft held, not offered for a vote: {why}",
                   idempotency_key=f"draft-held:{key}:{hashlib.sha256(text.encode()).hexdigest()[:12]}")
    cases.update(ctx, key, needs_draft=False,
                 held_draft=str(rec.get("last_inbound") or rec.get("created_at") or "held"))


def _store_draft(ctx, key: str, rec: dict, text: str) -> tuple[int, str, str]:
    version = int(rec.get("draft_version") or 0) + 1
    path = draft_path(ctx, key, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise safety.Refused(f"{path} already exists; drafts are never overwritten")
    path.write_text(text + "\n", encoding="utf-8")
    return version, str(path), path.read_text(encoding="utf-8")


def _open_reply_vote(ctx, key: str, rec: dict, version: int, path: str, body: str) -> None:
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    votes.open_vote(ctx, key=f"reply:{key}:v{version}", kind="reply", case_key=key, card_id=rec["card_id"],
                    subject=_subject(rec), question=f"send draft reply v{version} as written?",
                    identity={"case_key": key, "ticket": rec.get("ticket"), "recipient": rec["customer"],
                              "path": path},
                    material={"sha256": body_sha},
                    spec={"path": path, "sha256": body_sha, "version": version})


def release_held(ctx, key: str, rec: dict) -> str | None:
    """Move a held reply on once its money votes are settled. None = nothing held."""
    held = rec.get("held_reply")
    if not held:
        return None
    all_votes = votes.load(ctx)
    mv = [all_votes.get(k) for k in held["money_votes"]]
    if any(v is None for v in mv):
        ctx.alert(f"held-money-missing:{key}", f"{_subject(rec)}: a held reply's money vote is missing")
        return "held-money-missing"
    if any(v["status"] in (votes.DECLINED, votes.VOID, votes.EXPIRED) for v in mv):
        cases.update(ctx, key, held_reply=None, needs_draft=True)
        ctx.board.post(rec["card_id"], "A proposed money action was declined or voided, so the held reply "
                       f"(v{held['version']}) is dropped. A new draft follows.",
                       idempotency_key=f"held-dropped:{key}:v{held['version']}")
        if stages.stage_of(ctx.board.get_tags(rec["card_id"])) == stages.AWAITING \
                and not votes.live_for_case(ctx, key):
            cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
        return "held-dropped"
    if not all(v["status"] == votes.DONE for v in mv):
        return "held-waiting"
    if rec.get("needs_draft"):
        # The customer wrote again while the money was pending. The money decision
        # stands; the reply is redrafted so it answers the newer message too.
        cases.update(ctx, key, held_reply=None)
        ctx.board.post(rec["card_id"], f"Money verified. The customer wrote again meanwhile, so held reply "
                       f"v{held['version']} is replaced by a new draft.",
                       idempotency_key=f"held-superseded:{key}:v{held['version']}")
        return "held-superseded"
    with open(held["path"], encoding="utf-8") as stream:
        body = stream.read()
    if hashlib.sha256(body.encode()).hexdigest() != held["sha256"]:
        cases.update(ctx, key, held_reply=None, needs_draft=True)
        ctx.alert(f"held-changed:{key}", f"{_subject(rec)}: the held reply file changed; a new draft follows")
        return "held-changed"
    try:
        sender.check_sendable(ctx, key, rec, rec["customer"], body)
    except safety.Refused as exc:
        cases.update(ctx, key, held_reply=None)
        _hold(ctx, key, rec, f"after the money receipt the held reply is still not sendable: {exc}", body)
        return "held-refused"
    _open_reply_vote(ctx, key, rec, held["version"], held["path"], body)
    cases.update(ctx, key, held_reply=None)
    if stages.stage_of(ctx.board.get_tags(rec["card_id"])) != stages.AWAITING:
        cases.move(ctx, rec["card_id"], stages.AWAITING)
    ctx.board.post(rec["card_id"], f"Money verified with the provider; held reply v{held['version']} is now up "
                   "for a vote as written.", idempotency_key=f"held-released:{key}:v{held['version']}")
    return "held-released"


def _no_reply(ctx, key: str, rec: dict, reason: str) -> str:
    marker = rec.get("last_inbound") or rec.get("created_at") or ""
    if rec.get("no_reply_asked_for") == marker:
        ctx.alert(f"no-reply-twice:{key}:{marker}", f"{_subject(rec)}: the drafter said no-reply again after a No; "
                  "a person decides")
        _hold(ctx, key, rec, "no-reply was already declined for this message; a person decides", reason)
        cases.update(ctx, key, needs_draft=False)
        return "held"
    vkey = f"close:{key}:noreply:{hashlib.sha256(marker.encode()).hexdigest()[:12]}"
    ctx.board.post(rec["card_id"], f"**No reply proposed.** Reason: {reason}", idempotency_key=f"noreply-note:{vkey}")
    votes.open_vote(ctx, key=vkey, kind="close", case_key=key, card_id=rec["card_id"], subject=_subject(rec),
                    question="close without a reply? (nothing will be sent)",
                    identity={"case_key": key, "no_reply": True},
                    material={"no_reply": True, "last_inbound": marker, "reason": reason},
                    spec={"no_reply": True, "last_inbound": marker, "reason": reason})
    cases.update(ctx, key, needs_draft=False, no_reply_asked_for=marker)
    cases.move(ctx, rec["card_id"], stages.AWAITING)
    return "no-reply"


def _money_first(ctx, key: str, rec: dict, reply: str, specs: list[dict]) -> str:
    if ctx.payments is None:
        _hold(ctx, key, rec, "the drafter proposed a money action but payments are off for this product", reply)
        cases.update(ctx, key, needs_draft=False)
        return "held"
    p = ctx.config.policy
    try:
        for spec in specs:
            money.validate(spec, max_minor=p.refund_max_minor, currencies=p.currencies)
    except money.MoneySpecError as exc:
        _hold(ctx, key, rec, f"proposed money action refused: {exc}", reply)
        cases.update(ctx, key, needs_draft=False)
        return "held"
    proposed = frozenset("refund" if s["action"] == "refund" else "cancel" for s in specs)
    try:
        sender.check_sendable(ctx, key, rec, rec["customer"], reply, assume_money=proposed)
    except safety.Refused as exc:
        _hold(ctx, key, rec, str(exc), reply)
        cases.update(ctx, key, needs_draft=False)
        return "held"
    version, path, body = _store_draft(ctx, key, rec, reply)
    from sayso.jobs import money_job
    keys = [money_job.request(ctx, case_key=key, spec=s, subject=_subject(rec))["key"] for s in specs]
    cases.update(ctx, key, draft_version=version, needs_draft=False,
                 held_reply={"version": version, "path": path, "sha256": hashlib.sha256(body.encode()).hexdigest(),
                             "money_votes": keys})
    what = " and ".join(money.describe(s) for s in specs)
    ctx.board.post(rec["card_id"], f"**Draft reply v{version}** (not sent; it waits for the {what} to be verified "
                   f"with the provider)\n\n{body}", idempotency_key=f"draft:{key}:v{version}")
    cases.move(ctx, rec["card_id"], stages.AWAITING)
    return "money-first"


def run(ctx) -> dict:
    ctx.require_running()
    made = 0
    held = []
    out: dict[str, str] = {}
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or not rec.get("card_id"):
            continue
        released = release_held(ctx, key, rec)
        if released:
            out[key] = released
            continue
        tags = ctx.board.get_tags(rec["card_id"])
        if stages.stage_of(tags) != stages.IN_SUPPORT:
            continue
        if rec.get("dev_requested") or votes.live_for_case(ctx, key, "reply"):
            continue
        if not rec.get("needs_draft") and (rec.get("draft_version") or rec.get("no_reply_asked_for")
                                           or rec.get("held_draft")):
            continue                          # drafted, a no-reply decision pending/settled, or held for a person
        text = ctx.drafter.draft({"case_key": key, **rec}, ctx.board.messages(rec["card_id"])).strip()
        try:
            decision = decisions.parse(text)
        except decisions.DecisionError as exc:
            held.append(key)
            _hold(ctx, key, rec, str(exc), text)
            cases.update(ctx, key, needs_draft=False)
            continue
        if decision["kind"] == "no_reply":
            out[key] = _no_reply(ctx, key, rec, decision["reason"])
            continue
        if decision["money"]:
            out[key] = _money_first(ctx, key, rec, decision["reply"], decision["money"])
            if out[key] == "held":
                held.append(key)
            continue
        reply = decision["reply"]
        try:
            sender.check_sendable(ctx, key, rec, rec["customer"], reply)
        except safety.Refused as exc:
            held.append(key)
            _hold(ctx, key, rec, str(exc), reply)
            continue
        version, path, body = _store_draft(ctx, key, rec, reply)
        cases.update(ctx, key, draft_version=version, needs_draft=False)
        ctx.board.post(rec["card_id"], f"**Draft reply v{version}** (not sent)\n\n{body}",
                       idempotency_key=f"draft:{key}:v{version}")
        _open_reply_vote(ctx, key, rec, version, path, body)
        cases.move(ctx, rec["card_id"], stages.AWAITING)
        made += 1
    result = {"drafted": made, "held": held, **({"decisions": out} if out else {})}
    ctx.beat("draft", str(result))
    return result
