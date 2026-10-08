"""Release: merge a PR only on an approved merge vote for its EXACT head.

After the merge is read back as merged, the card returns to In support and a
new reply draft is requested, so the customer is told - through the same
vote-gated sender as every other reply. Deployment is out of scope: the
product's own pipeline owns it.
"""
from __future__ import annotations

from sayso import cases, stages, votes


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
        if ctx.qa_runner is not None and case.get("qa_passed_head") != spec["head"]:
            # QA is on, so a merge vote is only valid for the head QA passed. A vote
            # opened any other way (a hand-edited file, an old version) merges nothing.
            ctx.alert(f"merge-no-qa:{key}", f"merge of PR {spec['pr']} refused: QA did not pass head "
                      f"{str(spec['head'])[:12]}")
            out[key] = "refused-no-qa"
            continue
        pr = ctx.codehost.pull_request(int(spec["pr"]))
        if pr.state == "merged" and pr.head_sha == spec["head"]:
            status = "merged"
        elif not votes.still_bound(rec, {"head": pr.head_sha, "state": pr.state}):
            votes.set_status(ctx, key, votes.VOID, void_reason="PR head or state changed after approval")
            ctx.board.post(rec["card_id"], "Merge vote voided: the PR changed after approval. Nothing merged.",
                           idempotency_key=f"void:{key}")
            out[key] = "void"
            continue
        else:
            ctx.codehost.merge(pr.number, spec["head"], idempotency_key=key)
            status = ctx.codehost.pull_request(pr.number).state
        if status != "merged":
            ctx.alert(f"merge-unverified:{key}", f"merge of PR {pr.number} reported OK but reads back as {status}")
            out[key] = "unverified"
            continue
        votes.set_status(ctx, key, votes.DONE, merged_head=spec["head"])
        ctx.board.post(rec["card_id"], f"PR {pr.number} merged at {spec['head'][:12]} (read back).",
                       idempotency_key=f"merged:{key}")
        cases.update(ctx, rec["case_key"], dev_requested=False, needs_draft=True, dev_run=None)
        cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
        out[key] = "merged"
    ctx.beat("release", str(out))
    return out
