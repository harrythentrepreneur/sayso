"""QA: an independent check of one EXACT pull-request head before any merge vote.

A card the dev job leaves in ``In QA`` goes through four steps. Each one fails
closed: anything unreadable, unparsable or unexpected is never a pass.

 1. **Pre-QA script gate** (no model). CI must be green on the head, the PR
    must add or change a test file, and its added lines must carry no secret
    and no real-looking email address. CI still running = wait.
 2. **Fails on the old code.** The PR's own tests must pass on the head and
    at least one must FAIL with the non-test files put back to the base. A
    test that passes either way proves nothing. A check that cannot run is an
    infrastructure problem, not a code verdict: it is retried, then a human decides.
 3. **Independent QA run** on the exact head, briefed with the card (the
    customer's own words), hard limits, and one required verdict line. A UI
    change (``qa_ui_paths``) must also name real-app evidence.
 4. **Verdict.** PASS opens the merge vote for that exact head. BLOCKED sends
    the card back to ``In dev`` with QA's reason, at most ``max_dev_rounds``
    rounds in total; then the card goes to ``Blocked`` for a human. A provider
    usage limit is labelled as such and counts as neither a fail nor a round.

Ported from the PhonicsMaker loop, where each of these rules was added after a
real false green.
"""
from __future__ import annotations

import re

from sayso import cases, safety, small_fix, stages, votes, pr_refs

REQUIRED_QA_BOUNDARY = ("DO NOT MERGE", "DO NOT DEPLOY", "DO NOT PUSH", "DO NOT CONTACT THE CUSTOMER")

TEST_PATH = re.compile(r"(?i)(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|_test\.py$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_SAFE_EMAIL = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|[\w-]+\.(?:test|example|invalid|localhost))$"
                         r"|^(?:noreply|no-reply)@", re.I)
_VERDICT = re.compile(r"^[ \t]*VERDICT:[ \t]*(PASS|BLOCKED)[ \t]+([0-9a-f]{7,40})(?:[ \t]+(.*\S))?[ \t]*$")
_BLOCKED_ANYWHERE = re.compile(r"VERDICT:\s*BLOCKED\b(?:\s+[0-9a-f]{7,40})?(.*)$", re.I)
_CUSTOMER_FIX = re.compile(r"^[ \t]*CUSTOMER-FIX:[ \t]*(YES|NO)\b[ \t]*(.*?)[ \t]*$", re.M | re.I)
_FAILING_CHECK = re.compile(r"^[ \t]*FAILING-CHECK:[ \t]*(.+?)[ \t]*$", re.M)
_EVIDENCE = re.compile(r"^[ \t]*EVIDENCE:[ \t]*(\S.*?)[ \t]*$", re.M)
# A provider's own quota message. Matched only against the run's output, never
# the product's prose. A limit is not a QA fail and not a dev round.
_LIMIT = re.compile(r"hit your (?:weekly |daily |5-hour |session |usage )?limit|usage limit (?:reached|exceeded)"
                    r"|rate_limit_error|insufficient_quota|quota (?:exceeded|exhausted)|429 Too Many Requests", re.I)


class BriefUnsafe(RuntimeError):
    pass


# ----------------------------------------------------------------- pure rules
def added_lines(diff_lines) -> list[str]:
    return [str(x) for x in diff_lines or []]


def gate_findings(facts) -> tuple[str, list[str]]:
    """('wait', []) while CI runs; ('pass', []) or ('fail', reasons). Fails closed."""
    if facts.ci == "pending":
        return "wait", []
    why = []
    if facts.ci != "green":
        why.append("CI is not green on this head")
    paths = list(facts.files or [])
    if not paths or any(not p for p in paths):
        why.append("changed files unreadable")
    elif not any(TEST_PATH.search(p) for p in paths):
        why.append("no test file added or changed")
    if facts.added is None:
        why.append("diff unreadable")
    else:
        lines = added_lines(facts.added)
        if any(safety.has_secret(ln) for ln in lines):
            why.append("the diff adds something that looks like a secret key")
        if any(e for ln in lines for e in _EMAIL.findall(ln) if not _SAFE_EMAIL.search(e)):
            why.append("the diff adds a real-looking email address")
    return ("fail", why) if why else ("pass", [])


def _plain_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks and quotes. An unclosed fence hides the rest."""
    out, fence = [], None
    for line in (text or "").splitlines():
        mark = line.lstrip()[:3]
        if fence is None and mark in ("```", "~~~"):
            fence = mark
            continue
        if fence is not None:
            if mark == fence:
                fence = None
            continue
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return out


def parse_verdict(text: str, head: str) -> dict:
    """PASS only when the LAST non-empty line is exactly ``VERDICT: PASS <head>``.

    A BLOCKED verdict anywhere (plain, quoted or fenced) wins: a fence can hide
    a PASS, never a BLOCKED. A verdict for another SHA, words after PASS, or no
    verdict at all is BLOCKED.
    """
    raw = (text or "").splitlines()
    for line in raw:
        m = _BLOCKED_ANYWHERE.search(line)
        if m:
            return {"verdict": "BLOCKED", "reason": m.group(1).strip() or "QA blocked"}
    last = next((ln.rstrip() for ln in reversed(raw) if ln.strip()), "")
    plain_last = next((ln.rstrip() for ln in reversed(_plain_lines(text)) if ln.strip()), "")
    if last == f"VERDICT: PASS {head}" and plain_last == last:
        return {"verdict": "PASS", "reason": ""}
    m = _VERDICT.fullmatch(last)
    if m and m.group(2) != head:
        return {"verdict": "BLOCKED", "reason": f"QA verdict names {m.group(2)[:12]}, not the exact head"}
    return {"verdict": "BLOCKED", "reason": "QA did not end with the exact verdict line for this head"}


def customer_fix_problem(text: str) -> str:
    """'' when QA answered YES to 'does this fix what the customer reported'. Any NO wins."""
    found = _CUSTOMER_FIX.findall("\n".join(_plain_lines(text)))
    if not found:
        return "QA did not answer whether this fixes what the customer reported"
    for answer, why in found:
        if answer.upper() == "NO":
            return f"does not fix what the customer reported: {why}"[:200]
    return ""


def usage_limit(text: str) -> str:
    m = _LIMIT.search(text or "")
    return m.group(0) if m else ""


def is_ui(files, pattern: str) -> bool:
    return bool(pattern) and any(re.search(pattern, f) for f in files or [])


def build_brief(ctx, rec: dict, pr, *, ui: bool) -> str:
    thread = "\n\n---\n\n".join(ctx.board.messages(rec["card_id"]))
    evidence = ("\nThis PR changes the user interface. Check it in the REAL running app, through the control a\n"
                "customer presses, on the old code and on this head. Record what you did. Before the verdict\n"
                "write one line: EVIDENCE: <path or link to the recording and screenshots>\n"
                "A component test alone is not a UI pass.\n") if ui else ""
    return (f"Independent QA of one exact pull-request head. You did not write this code.\n\n"
            f"PRODUCT: {ctx.config.name}\nPR: #{pr.number} {pr.url}\nHEAD: {pr.head_sha}\n\n"
            f"1. Check out exactly {pr.head_sha} in a fresh, isolated worktree. Not the branch tip, not\n"
            f"   main: this SHA. If you cannot, the verdict is BLOCKED.\n"
            "2. Read the PR for what the fix claims. Run the tests and checks that cover the changed code.\n"
            "   Try to break the fix.\n"
            "3. DO NOT MERGE. DO NOT DEPLOY. DO NOT PUSH. DO NOT CONTACT THE CUSTOMER. You only report.\n"
            f"{evidence}\n"
            "The case, starting with the customer's own words:\n\n"
            f"{thread}\n\n"
            "Before the verdict, answer: does this fix what the customer reported? One line:\n"
            "CUSTOMER-FIX: YES <why>   or   CUSTOMER-FIX: NO <what is still wrong>\n"
            "If you block, name ONE check dev can run, as one line:\n"
            "FAILING-CHECK: <command or click path> -> <what it shows now>\n"
            "Block only on what this PR claims to fix or on something it breaks.\n\n"
            "Your final line must be exactly one of:\n"
            f"VERDICT: PASS {pr.head_sha}\n"
            f"VERDICT: BLOCKED {pr.head_sha} <one line: what failed>\n")


def assert_brief_safe(brief: str) -> None:
    missing = [m for m in REQUIRED_QA_BOUNDARY if m not in brief]
    if missing:
        raise BriefUnsafe(f"QA brief is missing its limits {missing}; refusing to start a QA run")


def judge(text: str, head: str, *, ui: bool) -> dict:
    """The final verdict of one QA run's output."""
    verdict = parse_verdict(text, head)
    if verdict["verdict"] != "PASS" and not _VERDICT.search("\n".join((text or "").splitlines()[-1:])):
        why = usage_limit(text)
        if why and not _BLOCKED_ANYWHERE.search(text or ""):
            return {"verdict": "LIMITED", "reason": f"provider usage limit: {why}"}
    if verdict["verdict"] == "PASS":
        problem = customer_fix_problem(text)
        if problem:
            return {"verdict": "BLOCKED", "reason": problem}
        if ui and not _EVIDENCE.search("\n".join(_plain_lines(text))):
            return {"verdict": "BLOCKED", "reason": "a UI change passed without real-app EVIDENCE"}
    m = _FAILING_CHECK.findall("\n".join(_plain_lines(text)))
    if m and verdict["verdict"] != "PASS":
        verdict["failing_check"] = m[-1][:300]
    return verdict


# ----------------------------------------------------------------- the job
def _qa(ctx, key: str) -> dict:
    return dict(cases.load(ctx)[key].get("qa") or {})


def _save(ctx, key: str, qa: dict) -> None:
    cases.update(ctx, key, qa=qa)


def _subject(rec: dict) -> str:
    return f"{rec['customer'].split('@')[0]} - {rec['title']}"


def open_merge_vote(ctx, key: str, rec: dict, pr, *, ref=None, move: bool = True) -> None:
    ref = ref if ref is not None else pr.number
    votes.open_vote(ctx, key=f"merge:{key}:{pr_refs.ident(ref)}:{pr.head_sha[:12]}", kind="merge", case_key=key,
                    card_id=rec["card_id"], subject=_subject(rec),
                    question=f"merge PR {pr.number} at head {pr.head_sha[:12]}?",
                    identity={"case_key": key, "pr": ref}, material={"head": pr.head_sha, "state": "open"},
                    spec={"pr": ref, "head": pr.head_sha})
    if move:
        cases.move(ctx, rec["card_id"], stages.AWAITING)


def record_policy_merge(ctx, key: str, rec: dict, pr, *, ref) -> None:
    votes.record_policy_approval(ctx, key=f"merge:{key}:{pr_refs.ident(ref)}:{pr.head_sha[:12]}:policy",
                                 case_key=key, card_id=rec["card_id"], subject=_subject(rec),
                                 question=f"merge PR {pr.number} at head {pr.head_sha[:12]}?",
                                 identity={"case_key": key, "pr": ref},
                                 material={"head": pr.head_sha, "state": "open"},
                                 spec={"pr": ref, "head": pr.head_sha}, reason="small fix, QA passed")


def send_back(ctx, key: str, rec: dict, head: str, reason: str) -> str:
    """A failed head returns to dev with the reason, or to a human after the last round."""
    rounds = int(rec.get("dev_attempt") or 0)
    limit = ctx.config.policy.max_dev_rounds
    if rounds >= limit:
        ctx.board.post(rec["card_id"], f"QA blocked head `{head[:12]}`: {reason}\nDev round {rounds} of {limit} was "
                       "the last. Moved to Blocked for a human: narrow the fix, split it, or park it.",
                       idempotency_key=f"qa-final:{key}:{head}")
        ctx.alert(f"qa-final:{key}:{head}", f"{_subject(rec)}: QA blocked after {rounds} dev rounds; a human decides")
        cases.move(ctx, rec["card_id"], stages.BLOCKED)
        return "blocked"
    ctx.board.post(rec["card_id"], f"QA blocked head `{head[:12]}`: {reason}\nBack to In dev (round {rounds} of "
                   f"{limit}); dev starts again from this card with QA's reason.",
                   idempotency_key=f"qa-back:{key}:{head}")
    cases.update(ctx, key, dev_run=None)
    cases.move(ctx, rec["card_id"], stages.IN_DEV)
    return "back-to-dev"


def step(ctx, key: str, rec: dict) -> str:
    refs = rec.get("prs") or [rec["pr"]]
    index = int(rec.get("qa_pr_index") or 0)
    if index >= len(refs):
        return "already-checked"
    ref = refs[index]
    pr = pr_refs.read(ctx, ref)
    if pr.state != "open":
        ctx.alert(f"qa-pr-not-open:{key}:{pr.number}", f"{_subject(rec)}: PR {pr.url} is {pr.state}, not open")
        return "pr-not-open"
    qa = _qa(ctx, key)
    if qa.get("head") != pr.head_sha:
        qa = {"head": pr.head_sha}           # every step below belongs to ONE head
        _save(ctx, key, qa)
    head, card = pr.head_sha, rec["card_id"]

    if qa.get("gate") != "pass":
        facts = pr_refs.host(ctx, ref).pr_facts(pr.number, head)
        state, why = gate_findings(facts)
        if state == "wait":
            return "wait-ci"
        if state == "fail":
            if why == ["CI is not green on this head"]:
                ctx.board.post(card, f"CI is red on head `{head[:12]}`, so no QA run started. Stays In QA; a "
                               "new head or a green re-run repeats this check.", idempotency_key=f"qa-ci:{key}:{head}")
                return "ci-red"
            return send_back(ctx, key, rec, head, "pre-QA check failed: " + "; ".join(why))
        qa.update(gate="pass", files=list(facts.files), ui=is_ui(facts.files, ctx.config.policy.qa_ui_paths))
        _save(ctx, key, qa)

    if qa.get("red") != "red-proved":
        tries = int(qa.get("red_tries") or 0)
        if tries >= ctx.config.policy.red_proof_max_tries:
            ctx.alert(f"qa-red-error:{key}:{head}", f"{_subject(rec)}: the fails-on-old-code check could not run "
                      f"{tries} times on {head[:12]}; a human decides")
            return "red-error"
        qa["red_tries"] = tries + 1
        _save(ctx, key, qa)
        try:
            got = ctx.qa_runner.fails_on_old_code(ref, head) or {}
        except Exception as exc:  # noqa: BLE001 - could not run is never a pass
            got = {"state": "error", "reason": type(exc).__name__}
        state = got.get("state")
        if state == "red-proved":
            qa["red"] = state
            _save(ctx, key, qa)
        elif state == "not-red":
            return send_back(ctx, key, rec, head, "the new tests also pass on the old code, so they do not prove "
                             "the fix")
        elif state == "head-red":
            return send_back(ctx, key, rec, head, str(got.get("reason") or "the PR's own tests fail on the head"))
        else:
            ctx.board.post(card, f"The fails-on-old-code check could not run on `{head[:12]}` "
                           f"({got.get('reason') or 'no result'}). Not a code verdict; retried next pass.",
                           idempotency_key=f"qa-red-error:{key}:{head}:{tries}")
            return "red-error"

    if not qa.get("run"):
        brief = build_brief(ctx, rec, pr, ui=bool(qa.get("ui")))
        assert_brief_safe(brief)
        n = int(qa.get("starts") or 0) + 1
        qa.update(run="starting", starts=n)
        _save(ctx, key, qa)                  # intent before dispatch: a restart never starts a second run
        qa["run"] = ctx.qa_runner.start(key, brief, idempotency_key=f"qa:{key}:{head}:{n}")
        _save(ctx, key, qa)
        ctx.board.post(card, f"QA started on head `{head[:12]}`. It checks and reports; it cannot merge.",
                       idempotency_key=f"qa-start:{key}:{head}:{n}")
        return "started"
    if qa["run"] == "starting":
        ctx.alert(f"qa-start-unknown:{key}:{head}", f"{_subject(rec)}: a QA start was interrupted; check by hand")
        return "start-unknown"

    out = ctx.qa_runner.result(qa["run"])
    if out is None:
        return "running"
    usage = out.get("usage")
    if usage:
        ctx.journal.append("qa_usage", case_key=key, head=head, usage=usage)
    verdict = judge(out.get("text") or "", head, ui=bool(qa.get("ui")))
    if verdict["verdict"] == "LIMITED":
        limited = int(qa.get("limited") or 0) + 1
        qa.update(run=None, limited=limited)
        _save(ctx, key, qa)
        ctx.board.post(card, f"QA stopped on a provider usage limit ({verdict['reason']}). This is not a QA fail "
                       "and not a dev round; QA re-runs on the same head.",
                       idempotency_key=f"qa-limited:{key}:{head}:{limited}")
        if limited >= ctx.config.policy.qa_limit_retries:
            ctx.alert(f"qa-limited:{key}:{head}", f"{_subject(rec)}: QA hit a usage limit {limited} times")
            qa["run"] = "limited"
            _save(ctx, key, qa)
        return "limited"
    qa["verdict"] = verdict
    _save(ctx, key, qa)
    if verdict["verdict"] != "PASS":
        reason = verdict["reason"] + (f"\nFAILING-CHECK: {verdict['failing_check']}"
                                      if verdict.get("failing_check") else "")
        return send_back(ctx, key, rec, head, reason)
    ctx.board.post(card, f"QA passed head `{head[:12]}` and confirms it fixes what the customer reported.",
                   idempotency_key=f"qa-pass:{key}:{head}")
    checked = {**rec.get("qa_passed_heads", {}), str(ref): head}
    cases.update(ctx, key, qa_passed_head=head, qa_passed_heads=checked, qa_pr_index=index + 1)
    if index + 1 < len(refs):
        return "pass-next-pr"                # no merge vote until every PR passed QA
    # Re-read EVERY head after QA, before opening any vote. A moved head voids
    # the whole set and sends the card back to the first PR for QA.
    prs = [pr_refs.read(ctx, n) for n in refs]
    if any(p.state != "open" or checked.get(str(n)) != p.head_sha for n, p in zip(refs, prs)):
        cases.update(ctx, key, qa_pr_index=0, qa_passed_heads={})
        ctx.board.post(card, "A PR changed while QA checked the set. Recheck every PR; no merge vote opened.",
                       idempotency_key=f"qa-set-moved:{key}:{head}")
        return "head-moved"
    # Optional small-fix policy (off by default): a small set with no sensitive
    # paths merges on this QA pass with no merge vote. Anything else gets votes.
    if ctx.config.policy.small_fix_release:
        why = small_fix.why_not(ctx, refs, checked)
        if not why:
            for n, p in zip(refs, prs):
                record_policy_merge(ctx, key, cases.load(ctx)[key], p, ref=n)
            ctx.board.post(card, f"Small-fix release: QA passed every PR and the change is small "
                           f"({small_fix.size_line(ctx, refs, checked)}), so it merges with no merge vote. "
                           "The customer reply still needs a vote.", idempotency_key=f"small-fix:{key}:{head}")
            cases.move(ctx, card, stages.AWAITING)
            return "pass-small-fix"
        ctx.board.post(card, f"Not a small fix ({why}), so the merge needs a vote.",
                       idempotency_key=f"not-small:{key}:{head}")
    for n, p in zip(refs, prs):
        open_merge_vote(ctx, key, cases.load(ctx)[key], p, ref=n, move=False)
    cases.move(ctx, card, stages.AWAITING)
    return "pass"


def run(ctx) -> dict:
    ctx.require_running()
    out: dict[str, str] = {}
    if ctx.qa_runner is None or ctx.codehost is None:
        ctx.beat("qa", "qa runner off")
        return out
    for key, rec in sorted(cases.load(ctx).items()):
        if rec.get("folded_into") or not rec.get("pr") or not rec.get("card_id"):
            continue
        if stages.stage_of(ctx.board.get_tags(rec["card_id"])) != stages.IN_QA:
            continue
        if (rec.get("qa") or {}).get("run") == "limited":
            continue
        out[key] = step(ctx, key, rec)
    ctx.beat("qa", str(out))
    return out
