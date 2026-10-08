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

from sayso import cases, stages, votes

REQUIRED_BOUNDARY = ("DO NOT MERGE", "DO NOT DEPLOY", "DO NOT CONTACT THE CUSTOMER")


class BriefUnsafe(RuntimeError):
    pass


def build_brief(ctx, case_key: str, rec: dict) -> str:
    thread = "\n\n---\n\n".join(ctx.board.messages(rec["card_id"]))
    return (f"# Case {rec['title']} ({ctx.config.name})\n\n{thread}\n\n"
            "## Limits\n- DO NOT MERGE. Open a pull request and stop.\n- DO NOT DEPLOY.\n"
            "- DO NOT CONTACT THE CUSTOMER. The loop drafts and votes on every reply.\n"
            "- End with the PR number.\n")


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
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or stages.stage_of(ctx.board.get_tags(rec["card_id"])) != stages.IN_DEV:
            continue
        if not rec.get("dev_run"):
            brief = build_brief(ctx, key, rec)
            assert_brief_safe(brief)
            attempt = int(rec.get("dev_attempt") or 0) + 1
            cases.update(ctx, key, dev_attempt=attempt, dev_run="starting")
            run_id = ctx.dev_runner.start(key, brief, idempotency_key=f"dev:{key}:{attempt}")
            cases.update(ctx, key, dev_run=run_id)
            ctx.board.post(rec["card_id"], "Dev run started. It opens a PR and stops; it cannot merge.",
                           idempotency_key=f"dev-start:{key}:{attempt}")
            out["started"].append(key)
            continue
        if rec["dev_run"] == "starting":
            ctx.alert(f"dev-start-unknown:{key}", f"dev start for {rec['title']!r} was interrupted; check by hand")
            continue
        result = ctx.dev_runner.result(rec["dev_run"])
        if not result:
            continue
        pr = ctx.codehost.pull_request(int(result["pr"]))
        if pr.state != "open":
            ctx.alert(f"dev-pr-not-open:{key}", f"dev PR {pr.url} is {pr.state}, not open")
            continue
        ctx.board.post(rec["card_id"], f"**Dev result**\n{result.get('summary', '')}\nPR: {pr.url} (head {pr.head_sha[:12]})",
                       idempotency_key=f"dev-result:{key}:{pr.number}")
        cases.update(ctx, key, pr=pr.number)
        cases.move(ctx, rec["card_id"], stages.IN_QA)
        out["pr"].append(key)
        if ctx.qa_runner is not None:
            continue                         # the QA job decides whether a merge vote opens
        ctx.board.post(rec["card_id"], "No QA runner is configured: this PR was NOT independently checked.",
                       idempotency_key=f"no-qa:{key}:{pr.number}:{pr.head_sha[:12]}")
        votes.open_vote(ctx, key=f"merge:{key}:{pr.number}:{pr.head_sha[:12]}", kind="merge", case_key=key,
                        card_id=rec["card_id"], subject=cases.subject(rec),
                        question=f"merge PR {pr.number} at head {pr.head_sha[:12]}?",
                        identity={"case_key": key, "pr": pr.number}, material={"head": pr.head_sha, "state": "open"},
                        spec={"pr": pr.number, "head": pr.head_sha})
        cases.move(ctx, rec["card_id"], stages.AWAITING)
    ctx.beat("dev", str(out))
    return out
