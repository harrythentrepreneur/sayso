"""Yes/No votes bound to an exact revision.

A vote records who decided what, about which exact thing. The thing is
fingerprinted (kind + identity + material). Before any action, the executor
re-reads the thing and recomputes the fingerprint; if it moved, the vote is
VOID and must be asked again. A vote never performs an action by itself.

Only configured operators can decide. Anyone else's vote is kept as an
opinion. Any operator "no" declines; otherwise one operator "yes" approves.

Every vote also carries an OWNER label (sayso/owners.py): whose decision
it is. The label is shown on the poll and pings only that person. It never
changes who can decide.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sayso.store import load_json, locked, save_json

OPEN, APPROVED, DECLINED, VOID, DONE, EXPIRED = "open", "approved", "declined", "void", "done", "expired"
LIVE = frozenset({OPEN, APPROVED})
KINDS = frozenset({"reply", "money", "close", "merge"})
POLICY = "policy"   # decided_by / decided_via of a merge the small-fix release policy approved

_ID_ONLY = re.compile(r"^(?:(?:ticket|case|card|pr|no\.?|#)\s*[-#]?\s*\w*\d+[\s,;/|-]*)+$", re.I)


class VoteError(RuntimeError):
    pass


def fingerprint(kind: str, identity: dict[str, Any], material: Any) -> str:
    payload = json.dumps([kind, identity, material], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def check_human_subject(subject: str) -> None:
    """A human reads the poll cold. A bare number means nothing to them."""
    text = str(subject or "").strip()
    if len(text) < 8 or _ID_ONLY.match(text):
        raise VoteError(f"poll subject must name the person or the plain problem, got {text!r}")


def open_vote(ctx, *, key: str, kind: str, case_key: str, card_id: str, subject: str, question: str,
              identity: dict[str, Any], material: Any, spec: dict[str, Any] | None = None) -> dict:
    """Open one vote. The same key never opens twice."""
    if kind not in KINDS:
        raise VoteError(f"unknown vote kind {kind}")
    check_human_subject(subject)
    if key in load_json(ctx.paths.votes):
        return load_json(ctx.paths.votes)[key]
    from sayso import owners
    owner = owners.for_vote(ctx, case_key)       # before the votes lock: it may read the board
    tag = owners.label(ctx, owner["owner"])
    with locked(ctx.paths.votes):
        votes = load_json(ctx.paths.votes)
        if key in votes:
            return votes[key]
        ctx.board.post(card_id, owners.started_line(ctx, owner), idempotency_key=f"owner:{key}")
        poll_id = ctx.board.open_poll(card_id, f"{tag}{subject} - {question}", idempotency_key=key)
        rec = {"key": key, "kind": kind, "case_key": case_key, "card_id": card_id, "poll_id": poll_id,
               "subject": subject, "question": question, "identity": identity,
               "fingerprint": fingerprint(kind, identity, material), "spec": spec or {},
               "status": OPEN, "opened_at": ctx.clock.now().isoformat(), "owner": owner["owner"],
               "owner_source": owner.get("owner_source")}
        votes[key] = rec
        save_json(ctx.paths.votes, votes)
    ctx.journal.append("vote_opened", key=key, kind=kind, case_key=case_key, owner=owner["owner"])
    mention = owner["owner"] if owners.name_of(ctx, owner["owner"]) else None
    ctx.notify(f"vote:{key}", f"{tag}{subject} - {question}", mention=mention)
    return rec


def record_policy_approval(ctx, *, key: str, case_key: str, card_id: str, subject: str, question: str,
                           identity: dict[str, Any], material: Any, spec: dict[str, Any], reason: str) -> dict:
    """An APPROVED merge record granted by the small-fix release policy, not a person.

    No poll opens and nobody is pinged. It is still bound to the exact head by its
    fingerprint, and the release job re-checks the policy and QA before merging.
    Only merges can be policy-approved; replies, money and closes always need a person.
    """
    check_human_subject(subject)
    with locked(ctx.paths.votes):
        all_votes = load_json(ctx.paths.votes)
        if key in all_votes:
            return all_votes[key]
        now = ctx.clock.now().isoformat()
        rec = {"key": key, "kind": "merge", "case_key": case_key, "card_id": card_id, "poll_id": None,
               "subject": subject, "question": question, "identity": identity,
               "fingerprint": fingerprint("merge", identity, material), "spec": spec,
               "status": APPROVED, "opened_at": now, "decided_at": now, "decided_by": POLICY,
               "decided_via": POLICY, "policy_reason": reason, "owner": "all", "owner_source": None}
        all_votes[key] = rec
        save_json(ctx.paths.votes, all_votes)
    ctx.journal.append("vote_policy_approved", key=key, kind="merge", case_key=case_key, reason=reason)
    return rec


def load(ctx) -> dict[str, Any]:
    return load_json(ctx.paths.votes)


def set_status(ctx, key: str, status: str, **fields: Any) -> dict:
    with locked(ctx.paths.votes):
        votes = load_json(ctx.paths.votes)
        rec = votes[key]
        rec.update(fields, status=status)
        save_json(ctx.paths.votes, votes)
    ctx.journal.append("vote_" + status, key=key, **{k: v for k, v in fields.items() if k != "opinions"})
    return rec


def record_ballots(ctx, key: str, ballots: dict[str, dict]) -> None:
    """Who has voted so far, and where (board or console). Display only; decide() is the authority."""
    with locked(ctx.paths.votes):
        all_votes = load_json(ctx.paths.votes)
        if all_votes.get(key, {}).get("ballots") == ballots:
            return
        all_votes[key]["ballots"] = ballots
        save_json(ctx.paths.votes, all_votes)


def decide(votes: dict[str, str], operators: dict[str, str]) -> tuple[str, str | None, dict[str, str]]:
    """(status, decided_by, opinions). Non-operators never decide."""
    opinions = {u: a for u, a in votes.items() if u not in operators}
    counted = {u: a for u, a in votes.items() if u in operators}
    noes = [u for u, a in counted.items() if a == "no"]
    if noes:
        return DECLINED, operators[sorted(noes)[0]], opinions
    yeses = [u for u, a in counted.items() if a == "yes"]
    if yeses:
        return APPROVED, operators[sorted(yeses)[0]], opinions
    return OPEN, None, opinions


def still_bound(rec: dict[str, Any], material: Any) -> bool:
    return fingerprint(rec["kind"], rec["identity"], material) == rec["fingerprint"]


def live_for_case(ctx, case_key: str, kind: str | None = None) -> list[dict]:
    return [v for v in load(ctx).values() if v["case_key"] == case_key and v["status"] in LIVE
            and (kind is None or v["kind"] == kind)]


def void_case(ctx, case_key: str, reason: str, kinds: frozenset[str] | None = None) -> list[str]:
    voided = []
    for v in live_for_case(ctx, case_key):
        if kinds and v["kind"] not in kinds:
            continue
        set_status(ctx, v["key"], VOID, void_reason=reason)
        ctx.board.post(v["card_id"], f"Vote voided: {reason}. Nothing was done.",
                       idempotency_key=f"void:{v['key']}")
        voided.append(v["key"])
    return voided
