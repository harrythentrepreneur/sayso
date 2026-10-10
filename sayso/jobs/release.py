"""Release: merge a PR only on an approved merge vote for its EXACT head.

After the merge is read back as merged, the card returns to In support and a
new reply draft is requested, so the customer is told - through the same
vote-gated sender as every other reply. Deployment is out of scope: the
product's own pipeline owns it.
"""
from __future__ import annotations

from sayso import cases, small_fix, stages, votes, pr_refs


def _withdraw_policy(ctx, key: str, rec: dict, case: dict) -> bool:
    """Re-check a policy approval right before merging. True = withdrawn and replaced by a vote.

    QA must have passed this exact head (QA on, and the exact-head check above), the
    policy must still be on, and the whole PR set must still be small at the heads QA
    passed. A head that moved is left to the normal fingerprint check, which voids it.
    """
    refs = case.get("prs") or [rec["spec"]["pr"]]
    heads = case.get("qa_passed_heads") or {}
    if ctx.qa_runner is None or any(str(n) not in heads for n in refs):
        why = "QA did not pass every PR in the set"
    else:
        why = small_fix.why_not(ctx, refs, heads)
    if not why:
        return False
    from sayso.jobs.qa import open_merge_vote
    for k, v in sorted(votes.load(ctx).items()):
        if (v["case_key"] == rec["case_key"] and v["kind"] == "merge" and v.get("decided_via") == votes.POLICY
                and v["status"] == votes.APPROVED):
            votes.set_status(ctx, k, votes.VOID, void_reason="small-fix release withdrawn: " + why)
            pr = pr_refs.read(ctx, v["spec"]["pr"])
            if pr.state == "open" and pr.head_sha == v["spec"]["head"]:
                open_merge_vote(ctx, rec["case_key"], case, pr, ref=v["spec"]["pr"], move=False)
    ctx.board.post(rec["card_id"], f"Small-fix release withdrawn before merge ({why}). Nothing merged; "
                   "the merge now needs a vote.",
                   idempotency_key=f"small-fix-withdrawn:{rec['case_key']}:{rec['spec']['head']}")
    return True


def run(ctx) -> dict:
    ctx.require_running()
    out = {}
    if ctx.codehost is None:
        ctx.beat("release", "code host off")
        return out
    for key, rec in sorted(votes.load(ctx).items()):
        if rec["kind"] != "merge" or rec["status"] != votes.APPROVED:
            continue
        spec = rec["spec"]
        case = cases.load(ctx).get(rec["case_key"], {})
        if ctx.qa_runner is not None and (case.get("qa_passed_heads") or {}).get(str(spec["pr"])) != spec["head"]:
            # QA is on, so a merge vote is only valid for the head QA passed. A vote
            # opened any other way (a hand-edited file, an old version) merges nothing.
            ctx.alert(f"merge-no-qa:{key}", f"merge of PR {spec['pr']} refused: QA did not pass head "
                      f"{str(spec['head'])[:12]}")
            out[key] = "refused-no-qa"
            continue
        if rec.get("decided_via") == votes.POLICY and _withdraw_policy(ctx, key, rec, case):
            out[key] = "policy-withdrawn"
            continue
        pr = pr_refs.read(ctx, spec["pr"])
        host = pr_refs.host(ctx, spec["pr"])
        if pr.state == "merged" and pr.head_sha == spec["head"]:
            status = "merged"
        elif not votes.still_bound(rec, {"head": pr.head_sha, "state": pr.state}):
            votes.set_status(ctx, key, votes.VOID, void_reason="PR head or state changed after approval")
            ctx.board.post(rec["card_id"], "Merge vote voided: the PR changed after approval. Nothing merged.",
                           idempotency_key=f"void:{key}")
            out[key] = "void"
            continue
        else:
            host.merge(pr.number, spec["head"], idempotency_key=key)
            status = pr_refs.read(ctx, spec["pr"]).state
        if status != "merged":
            ctx.alert(f"merge-unverified:{key}", f"merge of PR {pr.number} reported OK but reads back as {status}")
            out[key] = "unverified"
            continue
        votes.set_status(ctx, key, votes.DONE, merged_head=spec["head"])
        ctx.board.post(rec["card_id"], f"PR {pr.number} merged at {spec['head'][:12]} (read back).",
                       idempotency_key=f"merged:{key}")
        # A multi-PR fix is shipped only when EVERY PR has a verified merge.
        # A vote on one head never grants authority over the other head.
        all_refs = case.get("prs") or [spec["pr"]]
        done_votes = [v for v in votes.load(ctx).values()
                      if v["case_key"] == rec["case_key"] and v["kind"] == "merge"
                      and v["status"] == votes.DONE]
        if (any(pr_refs.read(ctx, n).state != "merged" for n in all_refs)
                or any(not any(v["spec"].get("pr") == n and
                                   v.get("merged_head") == pr_refs.read(ctx, n).head_sha
                                   for v in done_votes) for n in all_refs)):
            out[key] = "merged-waiting-for-other-prs"
            continue
        cases.update(ctx, rec["case_key"], dev_requested=False, needs_draft=True, dev_run=None)
        cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
        out[key] = "merged"
    ctx.beat("release", str(out))
    return out
