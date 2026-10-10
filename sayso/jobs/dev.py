"""Dev: a card tagged In dev starts ONE bounded coding run; its PR gets a merge vote.

- The card is the specification. The brief carries the card's messages plus
  explicit limits, and ``assert_brief_safe`` refuses to start a run whose
  brief lost them.
- A start is recorded before the run is dispatched, so a restart cannot start
  a second run for the same card.
- With a QA runner configured, a PR goes to ``In QA`` and the QA job decides
  whether a merge vote opens (jobs/qa.py). A card QA sends back starts a new
  round; QA's reason is already a card message, so it is in the new brief.
- Without a QA runner, the PR's merge vote opens directly and the card says
  plainly that no independent QA ran.
"""
from __future__ import annotations

from sayso import cases, stages, votes, pr_refs

REQUIRED_BOUNDARY = ("DO NOT MERGE", "DO NOT DEPLOY", "DO NOT CONTACT THE CUSTOMER")


class BriefUnsafe(RuntimeError):
    pass


def build_brief(ctx, case_key: str, rec: dict) -> str:
    thread = "\n\n---\n\n".join(ctx.board.messages(rec["card_id"]))
    return (f"# Case {rec['title']} ({ctx.config.name})\n\n{thread}\n\n"
            "## Limits\n- DO NOT MERGE. Open a pull request and stop.\n- DO NOT DEPLOY.\n"
            "- DO NOT CONTACT THE CUSTOMER. The loop drafts and votes on every reply.\n"
            "- End with a result block: === RESULT ===, a short summary, === END RESULT ===.\n"
            "- Then write PR: <GitHub URL>[, <GitHub URL>...] for every repository you changed.\n")


def assert_brief_safe(brief: str) -> None:
    missing = [m for m in REQUIRED_BOUNDARY if m not in brief]
    if missing:
        raise BriefUnsafe(f"brief is missing its limits {missing}; refusing to start a coding run")


def request(ctx, case_key: str) -> None:
    """Operator or support agent hands a case to dev."""
    rec = cases.load(ctx)[case_key]
    cases.update(ctx, case_key, dev_requested=True)
    votes.void_case(ctx, case_key, "the case was handed to dev", kinds=frozenset({"reply"}))
    tags = ctx.board.get_tags(rec["card_id"])
    if stages.stage_of(tags) == stages.AWAITING:
        cases.move(ctx, rec["card_id"], stages.IN_SUPPORT)
    cases.move(ctx, rec["card_id"], stages.IN_DEV)


def run(ctx) -> dict:
    ctx.require_running()
    out = {"started": [], "pr": []}
    if ctx.dev_runner is None:
        ctx.beat("dev", "dev runner off")
        return out
    if ctx.qa_runner is None:
        for key, rec in sorted(cases.load(ctx).items()):
            if not rec.get("prs") or not rec.get("card_id") or rec.get("folded_into"):
                continue
            if stages.stage_of(ctx.board.get_tags(rec["card_id"])) != stages.IN_QA:
                continue
            prs = [pr_refs.read(ctx, n) for n in rec["prs"]]
            recorded = rec.get("dev_pr_heads") or {}
            if any(p.state != "open" or recorded.get(str(n)) != p.head_sha
                   for n, p in zip(rec["prs"], prs)):
                ctx.alert(f"dev-votes-pr-changed:{key}", "PR changed before merge votes opened; check by hand")
                continue
            for ref, p in zip(rec["prs"], prs):
                votes.open_vote(ctx, key=f"merge:{key}:{pr_refs.ident(ref)}:{p.head_sha[:12]}",
                                kind="merge", case_key=key, card_id=rec["card_id"],
                                subject=f"{rec['customer'].split('@')[0]} - {rec['title']}",
                                question=f"merge PR {p.number} at head {p.head_sha[:12]}?",
                                identity={"case_key": key, "pr": ref},
                                material={"head": p.head_sha, "state": "open"},
                                spec={"pr": ref, "head": p.head_sha})
            cases.move(ctx, rec["card_id"], stages.AWAITING)
    active = sum(bool(r.get("dev_run")) for r in cases.load(ctx).values()
                 if r.get("card_id") and not r.get("folded_into")
                 and stages.stage_of(ctx.board.get_tags(r["card_id"])) == stages.IN_DEV)
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or stages.stage_of(ctx.board.get_tags(rec["card_id"])) != stages.IN_DEV:
            continue
        if not rec.get("dev_run"):
            if active >= ctx.config.policy.max_parallel_dev_runs:
                continue
            brief = build_brief(ctx, key, rec)
            assert_brief_safe(brief)
            attempt = int(rec.get("dev_attempt") or 0) + 1
            cases.update(ctx, key, dev_attempt=attempt, dev_run="starting")
            run_id = ctx.dev_runner.start(key, brief, idempotency_key=f"dev:{key}:{attempt}")
            cases.update(ctx, key, dev_run=run_id)
            active += 1
            ctx.board.post(rec["card_id"], "Dev run started. It opens a PR and stops; it cannot merge.",
                           idempotency_key=f"dev-start:{key}:{attempt}")
            out["started"].append(key)
            continue
        if rec["dev_run"] == "starting":
            ctx.alert(f"dev-start-unknown:{key}", f"dev start for {rec['title']!r} was interrupted; check by hand")
            continue
        result = ctx.dev_runner.result(rec["dev_run"])
        if not result:
            state = ctx.dev_runner.state(rec["dev_run"])
            if state != "dead":  # running or unreadable: never start a duplicate
                continue
            attempt = int(rec.get("dev_attempt") or 0)
            if attempt >= ctx.config.policy.max_dev_rounds:
                ctx.board.post(rec["card_id"], "Dev run ended without a PR twice. Blocked for a human; no "
                               "third run started.", idempotency_key=f"dev-dead-final:{key}:{attempt}")
                cases.move(ctx, rec["card_id"], stages.BLOCKED)
                active -= 1
                continue
            ctx.board.post(rec["card_id"], "Dev run ended with no PR. Starting one fresh retry.",
                           idempotency_key=f"dev-dead-retry:{key}:{attempt}")
            brief = build_brief(ctx, key, rec)
            assert_brief_safe(brief)
            cases.update(ctx, key, dev_run="starting", dev_attempt=attempt + 1)
            retry = ctx.dev_runner.start(key, brief, idempotency_key=f"dev:{key}:{attempt + 1}")
            cases.update(ctx, key, dev_run=retry)
            out["started"].append(key)
            continue
        if result.get("usage"):
            usage = result["usage"]
            if not isinstance(usage, dict) or not all(isinstance(usage.get(n), (int, float))
                                                     for n in ("tokens_read", "tokens_out", "minutes")):
                ctx.alert(f"dev-usage-invalid:{key}:{rec['dev_run']}", "dev usage was unreadable; not reported")
            else:
                by_run = {**rec.get("dev_usage", {}), rec["dev_run"]: usage}
                cases.update(ctx, key, dev_usage=by_run)
                ctx.board.post(rec["card_id"],
                               f"Dev run usage: {int(usage['tokens_read']):,} tokens read, "
                               f"{int(usage['tokens_out']):,} out, {usage['minutes']:.1f} minutes.",
                               idempotency_key=f"dev-usage:{key}:{rec['dev_run']}")
        refs = result.get("prs") or [result.get("pr")]
        if not isinstance(refs, list) or not refs or len(set(map(str, refs))) != len(refs):
            ctx.alert(f"dev-prs-invalid:{key}", "dev result has no valid, distinct PR numbers")
            continue
        try:
            prs = [pr_refs.read(ctx, n) for n in refs]
        except (ValueError, KeyError) as exc:
            ctx.alert(f"dev-prs-unreadable:{key}", f"dev PR list unreadable: {type(exc).__name__}")
            continue
        if any(p.state != "open" for p in prs):
            ctx.alert(f"dev-pr-not-open:{key}", "a dev PR is not open; case stays In dev")
            continue
        for p in prs:
            ctx.board.post(rec["card_id"], f"**Dev result**\n{result.get('summary', '')}\n"
                           f"PR: {p.url} (head {p.head_sha[:12]})",
                           idempotency_key=f"dev-result:{key}:{p.url}")
        cases.update(ctx, key, pr=refs[0], prs=refs, dev_pr_heads={str(n): p.head_sha for n, p in zip(refs, prs)},
                     qa_pr_index=0, qa_passed_heads={}, qa={})
        cases.move(ctx, rec["card_id"], stages.IN_QA)
        active -= 1
        out["pr"].append(key)
        if ctx.qa_runner is not None:
            continue
        ctx.board.post(rec["card_id"], "No QA runner is configured: these PRs were NOT independently checked.",
                       idempotency_key=f"no-qa:{key}:{rec['dev_run']}")
        for ref, p in zip(refs, prs):
            votes.open_vote(ctx, key=f"merge:{key}:{pr_refs.ident(ref)}:{p.head_sha[:12]}", kind="merge", case_key=key,
                            card_id=rec["card_id"], subject=f"{rec['customer'].split('@')[0]} - {rec['title']}",
                            question=f"merge PR {p.number} at head {p.head_sha[:12]}?",
                            identity={"case_key": key, "pr": ref},
                            material={"head": p.head_sha, "state": "open"},
                            spec={"pr": ref, "head": p.head_sha})
        cases.move(ctx, rec["card_id"], stages.AWAITING)
    ctx.beat("dev", str(out))
    return out
