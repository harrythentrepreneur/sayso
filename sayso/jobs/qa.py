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

from sayso import cases, regressions, safety, small_fix, stages, votes, pr_refs

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
# BLOCK-KIND: DEFECT | EVIDENCE <what is missing>. A block that only lacks proof
# no dev run can make (a paid run, a live service) costs no dev round.
_BLOCK_KIND = re.compile(r"^[ \t]*BLOCK-KIND:[ \t]*(\S+)[ \t]*(.*?)[ \t]*$", re.M | re.I)
_BLOCK_KIND_LABEL = re.compile(r"^[ \t]*BLOCK-KIND:", re.M | re.I)
# Any FAILING-CHECK line anywhere (plain, quoted or fenced) means a code defect.
_ANY_FAILING_CHECK = re.compile(r"FAILING-CHECK\s*:", re.I)
# QA's own tool stopped a long step (a build, a full suite).
_TOOL_TIMEOUT = re.compile(
    r"(?i)\b(?:QA[- ]tool|tool call|tool|command)\b[^.\n]{0,80}?"
    r"\b(?:timed out|time ?limit|timeout|interrupted|stopped [^.\n]{0,40}after \d+ ?(?:s|sec|seconds|minutes))")
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
    # "not-run": GitHub refused to start CI (account billing) and ran nothing. That is
    # not red: the fails-on-old-code check and QA on the exact head are the gate, and
    # every content check below still applies. A job that ran is never "not-run".
    if facts.ci not in ("green", "not-run"):
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


# Which lines does CommonMark show as ordinary text? Each rule below was a real
# false green in the PhonicsMaker loop: four QA rounds found four corners of a
# home-made fence reader. So this is a small CommonMark BLOCK parser (zero
# dependencies on purpose), checked line by line against a reference CommonMark
# parser by tests/test_qa_markdown.py. It models what can hide a line:
#   * containers: '>' block quotes and list items (a fence inside "- ```" or a
#     quote ends when its container ends; lazy paragraph lines continue it);
#   * code fences: 3+ backticks or tildes after at most 3 spaces; closed only by
#     the SAME character, at least as many, at most 3 spaces, nothing after. An
#     unclosed fence runs to the end. 4+ spaces is indented text, not a fence;
#   * HTML blocks (the seven CommonMark kinds).
# A '>' quote line is hidden (only that line: lazy continuation would otherwise
# swallow QA's next plain line, its verdict, into the quote). Where this reader
# and CommonMark could differ it hides MORE, never less: a hidden label can only
# block, never pass.
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|"
    "dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h1|h2|h3|h4|h5|h6|head|header|hr|html|iframe|"
    "legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|source|summary|"
    "table|tbody|td|tfoot|th|thead|title|tr|track|ul")
_ATTR = r"""(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^"'=<>`\x00-\x20]+|'[^']*'|"[^"]*"))?)"""
_HTML_START = [
    (1, re.compile(r"<(?:script|pre|style|textarea)(?=\s|>|$)", re.I), re.compile(r"</(?:script|pre|style|textarea)>", re.I)),
    (2, re.compile(r"<!--"), re.compile(r"-->")),
    (3, re.compile(r"<\?"), re.compile(r"\?>")),
    (4, re.compile(r"<![A-Za-z]"), re.compile(r">")),
    (5, re.compile(r"<!\[CDATA\["), re.compile(r"\]\]>")),
    (6, re.compile(rf"</?(?:{_HTML_BLOCK_TAGS})(?=\s|/?>|$)", re.I), None),
    (7, re.compile(rf"(?:<[A-Za-z][A-Za-z0-9-]*{_ATTR}*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)\s*$"), None),
]
_THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ITEM = re.compile(r"^( {0,3})([-+*]|[0-9]{1,9}[.)])(?=[ ]|$)")


def _md_lines(text: str) -> list[str]:
    """The text split the way CommonMark splits it (\\r\\n and \\r end a line too)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _fence_open(rest: str):
    m = _FENCE.match(rest)
    if not m or (m.group(2)[0] == "`" and "`" in m.group(3)):
        return None
    return m.group(2)[0], len(m.group(2))


def _html_open(rest: str, para: bool):
    if _indent(rest) > 3:
        return None
    body = rest.lstrip(" ")
    for kind, start, _end in _HTML_START:
        if start.match(body) and not (kind == 7 and para):
            return kind
    return None


def _item_open(rest: str, para: bool):
    """(width of marker+indent, content indent, blank start) or None."""
    if _THEMATIC.match(rest):
        return None
    m = _ITEM.match(rest)
    if not m:
        return None
    after = rest[m.end():]
    blank = not after.strip()
    if para and (blank or (m.group(2)[0].isdigit() and int(m.group(2)[:-1]) != 1)):
        return None                       # cannot interrupt a paragraph
    width = m.end()
    spaces = _indent(after)
    if blank:
        return width, width + 1, True
    return width, width + (spaces if spaces <= 4 else 1), False


def _starts_block(rest: str, para: bool) -> bool:
    if not rest.strip():
        return True
    if _indent(rest) > 3:
        return False
    return bool(rest.lstrip(" ").startswith(">") or _item_open(rest, para) or _fence_open(rest)
                or _html_open(rest, para) or _THEMATIC.match(rest) or _ATX.match(rest))


def _plain_mask(text: str) -> list[bool]:
    """One flag per _md_lines() line: True when CommonMark shows it as ordinary text.

    Read twice: once with CommonMark's paragraph-interruption rules, once as if any
    list item or HTML tag could start a new block. A line is plain only when both
    reads agree. Where parsers differ on an odd answer, the line is hidden."""
    return [a and b for a, b in zip(_mask(text, strict=True), _mask(text, strict=False))]


def _mask(text: str, *, strict: bool) -> list[bool]:
    lines = _md_lines(text)
    plain = [True] * len(lines)
    stack: list[dict] = []                    # open containers, outermost first
    leaf = None                               # None | "para" | "code" | ("fence", ch, n) | ("html", kind)
    for i, raw in enumerate(lines):
        line = raw.expandtabs(4)
        if line.lstrip(" ").startswith(">"):
            plain[i] = False                  # a quote line is never QA's own plain line
        pos, matched = 0, 0
        for c in stack:
            rest = line[pos:]
            if c["kind"] == "quote":
                m = re.match(r" {0,3}>", rest)
                if not m:
                    break
                pos += m.end() + (1 if line[pos + m.end():pos + m.end() + 1] == " " else 0)
            elif not rest.strip():
                if c["empty"] and c["blank_start"]:
                    break                     # an item may begin with at most one blank line
            elif _indent(rest) >= c["indent"]:
                pos += c["indent"]
            else:
                break
            matched += 1
        rest = line[pos:]
        if isinstance(leaf, tuple) and matched == len(stack):
            plain[i] = False
            if leaf[0] == "fence":
                m = re.match(r"^ {0,3}(`+|~+)[ ]*$", rest)
                if m and m.group(1)[0] == leaf[1] and len(m.group(1)) >= leaf[2]:
                    leaf = None
            elif leaf[1] in (6, 7):
                if not rest.strip():
                    leaf = None                   # the blank line ends the block; it is not part of it
            elif _HTML_START[leaf[1] - 1][2].search(rest):
                leaf = None
            continue
        if matched < len(stack) and leaf == "para" and not _starts_block(rest, False):
            continue                          # lazy paragraph continuation (any block start ends it: hides more)
        if matched < len(stack):
            del stack[matched:]
            leaf = None
        opened = False
        while True:
            rest = line[pos:]
            if not rest.strip() or _indent(rest) > 3:
                break
            if rest.lstrip(" ").startswith(">"):
                pos += _indent(rest) + 1 + (1 if line[pos + _indent(rest) + 1:pos + _indent(rest) + 2] == " " else 0)
                stack.append({"kind": "quote"})
                opened, leaf = True, None
                continue
            item = _item_open(rest, leaf == "para" and not opened and strict)
            if not item:
                break
            width, content, blank = item
            pos += width if blank else content
            stack.append({"kind": "item", "indent": content, "blank_start": blank, "empty": blank})
            opened, leaf = True, None
            if blank:
                break
        rest = line[pos:]
        if rest.strip():
            for c in stack:
                c["empty"] = False
        para = leaf == "para" and not opened and strict
        if not rest.strip():
            if leaf != "code":
                leaf = None
            continue
        if _indent(rest) > 3:
            leaf = "para" if para else "code"
            continue
        fence = _fence_open(rest)
        if fence:
            plain[i], leaf = False, ("fence",) + fence
            continue
        kind = _html_open(rest, para)
        if kind:
            plain[i] = False
            end = _HTML_START[kind - 1][2]
            leaf = None if end is not None and end.search(rest) else ("html", kind)
            continue
        if _THEMATIC.match(rest) or _ATX.match(rest) or (para and re.match(r"^ {0,3}(=+|-+)[ ]*$", rest)):
            leaf = None
            continue
        leaf = "para"
    return plain


def _plain_lines(text: str) -> list[str]:
    """Lines CommonMark shows as ordinary text: not in a code fence (closed or not), an
    HTML block or a '>' quote line."""
    return [ln for ln, ok in zip(_md_lines(text), _plain_mask(text)) if ok]


def _ends_on_plain_line(text: str) -> bool:
    """True when the last non-empty line of the text is itself a plain line."""
    for ln, ok in zip(reversed(_md_lines(text)), reversed(_plain_mask(text))):
        if ln.strip():
            return ok
    return False


def parse_verdict(text: str, head: str) -> dict:
    """PASS only when the LAST non-empty line is exactly ``VERDICT: PASS <head>`` AND
    that very line is plain (not inside a fence, HTML block or quote).

    A BLOCKED verdict anywhere (plain, quoted or fenced) wins: a fence can hide
    a PASS, never a BLOCKED. A verdict for another SHA, words after PASS, or no
    verdict at all is BLOCKED. An earlier identical plain PASS never vouches for
    a fenced final one.
    """
    raw = (text or "").splitlines()
    for line in raw:
        m = _BLOCKED_ANYWHERE.search(line)
        if m:
            return {"verdict": "BLOCKED", "reason": m.group(1).strip() or "QA blocked"}
    last = next((ln.rstrip() for ln in reversed(raw) if ln.strip()), "")
    md_last = next((ln.rstrip() for ln in reversed(_md_lines(text)) if ln.strip()), "")
    if last == f"VERDICT: PASS {head}" and md_last == last and _ends_on_plain_line(text):
        return {"verdict": "PASS", "reason": ""}
    m = _VERDICT.fullmatch(last)
    if m and m.group(2) != head:
        return {"verdict": "BLOCKED", "reason": f"QA verdict names {m.group(2)[:12]}, not the exact head"}
    return {"verdict": "BLOCKED", "reason": "QA did not end with the exact verdict line for this head"}


def block_kind(text: str) -> tuple[str, str]:
    """('EVIDENCE', what is missing), ('DEFECT', '') or ('', '') when unclear.

    Labels are read from QA's own plain lines only. EVIDENCE needs: every
    BLOCK-KIND line parses and says EVIDENCE with what is missing, NO
    FAILING-CHECK anywhere in the raw answer (even quoted or fenced), and the
    answer ends on a plain line. Anything else is a code defect or unclear,
    and both cost a dev round as before.
    """
    if _ANY_FAILING_CHECK.search(text or ""):
        return "DEFECT", ""
    plain = "\n".join(_plain_lines(text))
    found = _BLOCK_KIND.findall(plain)
    if not found or len(_BLOCK_KIND_LABEL.findall(plain)) != len(found):
        return "", ""
    kinds = {k.upper() for k, _ in found}
    if "DEFECT" in kinds:
        return "DEFECT", ""
    what = [w.strip() for _, w in found]
    if kinds != {"EVIDENCE"} or not all(what) or not _ends_on_plain_line(text):
        return "", ""
    return "EVIDENCE", what[-1][:160]


def tool_timeout_block(text: str) -> bool:
    """QA blocked because its own tool stopped a long step, and showed no code defect.

    Structural: QA names a tool timeout in its own plain lines, writes NO
    FAILING-CHECK anywhere, and its answer ends on a plain line.
    """
    if _ANY_FAILING_CHECK.search(text or "") or not _ends_on_plain_line(text):
        return False
    return bool(_TOOL_TIMEOUT.search("\n".join(_plain_lines(text))))


def _qa_blocked_this_head(text: str, head: str) -> bool:
    """QA's own last line is ``VERDICT: BLOCKED <this head> ...``. A block made by
    Sayso (no verdict, wrong SHA, customer NO) never takes a kind."""
    last = next((ln.rstrip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")
    m = _VERDICT.fullmatch(last)
    return bool(m and m.group(1) == "BLOCKED" and m.group(2) == head)


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


def _regs(ctx, rec: dict) -> list[dict]:
    return regressions.entries(rec) if ctx.config.policy.qa_regression_set else []


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
            f"{evidence}{regressions.qa_section(_regs(ctx, rec))}\n"
            "The case, starting with the customer's own words:\n\n"
            f"{thread}\n\n"
            "Before the verdict, answer: does this fix what the customer reported? One line:\n"
            "CUSTOMER-FIX: YES <why>   or   CUSTOMER-FIX: NO <what is still wrong>\n"
            "If you block, name ONE check dev can run, as one line:\n"
            "FAILING-CHECK: <command or click path> -> <what it shows now>\n"
            "DEFECT-CLASS: <the kind of fault in a few words>\n"
            "BLOCK-KIND: DEFECT\n"
            "If you found NO code defect and only proof is missing (a paid run, a live service, or\n"
            "your own tool timed out), write NO FAILING-CHECK line at all. Write instead:\n"
            "BLOCK-KIND: EVIDENCE <what is missing>\n"
            "A FAILING-CHECK line anywhere in your answer, even quoted, always counts as a code\n"
            "defect. If unsure, it is a DEFECT.\n"
            "Block only on what this PR claims to fix or on something it breaks.\n\n"
            "Your final line must be exactly one of:\n"
            f"VERDICT: PASS {pr.head_sha}\n"
            f"VERDICT: BLOCKED {pr.head_sha} <one line: what failed>\n")


def assert_brief_safe(brief: str) -> None:
    missing = [m for m in REQUIRED_QA_BOUNDARY if m not in brief]
    if missing:
        raise BriefUnsafe(f"QA brief is missing its limits {missing}; refusing to start a QA run")


def judge(text: str, head: str, *, ui: bool, regs: list | None = None) -> dict:
    """The final verdict of one QA run's output.

    Besides PASS / BLOCKED / LIMITED it can return RERUN (QA did not follow its
    brief, or its own tool timed out with no defect shown: one fresh QA run on the
    same head, never a dev round) and a BLOCKED with ``kind: EVIDENCE`` (only
    proof is missing: the card goes to a human, no dev round spent).
    """
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
        kind, why = regressions.report_problem(list(regs or []), _plain_lines(text))
        if kind == "missing":
            return {"verdict": "RERUN", "reason": why}
        if kind == "fail":
            verdict = {"verdict": "BLOCKED", "reason": why}
    kind, missing = block_kind(text)
    if verdict["verdict"] == "PASS" and (kind or _BLOCK_KIND_LABEL.search("\n".join(_plain_lines(text)))):
        return {"verdict": "BLOCKED", "reason": "QA wrote a BLOCK-KIND line and a PASS verdict"}
    if verdict["verdict"] == "BLOCKED" and _qa_blocked_this_head(text, head) and tool_timeout_block(text):
        return {"verdict": "RERUN", "reason": f"QA tool timeout, no code defect shown: {verdict['reason']}"[:200],
                "then": verdict}
    if verdict["verdict"] == "BLOCKED" and kind == "EVIDENCE" and _qa_blocked_this_head(text, head):
        verdict.update(kind="EVIDENCE", missing=missing)
    m = _FAILING_CHECK.findall("\n".join(_plain_lines(text)))
    if m and verdict["verdict"] != "PASS":
        verdict["failing_check"] = m[-1][:300]
        verdict["defect_class"] = regressions.defect_class(_plain_lines(text))
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


def evidence_block(ctx, key: str, rec: dict, head: str, missing: str) -> str:
    """QA found no code defect; only proof is missing that no dev run can make.
    No dev round is spent: the card goes to a human."""
    ctx.board.post(rec["card_id"], f"QA blocked head `{head[:12]}` for missing proof, not a code defect: {missing}. "
                   "Moved to Blocked; no dev round spent. Options: provide the proof (for example approve a "
                   "paid run), narrow the check, split, or park.", idempotency_key=f"qa-evidence:{key}:{head}")
    ctx.alert(f"qa-evidence:{key}:{head}", f"{_subject(rec)}: QA needs proof a dev run cannot make: {missing}")
    cases.move(ctx, rec["card_id"], stages.BLOCKED)
    return "blocked-evidence"


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
        qa.update(gate="pass", files=list(facts.files), ui=is_ui(facts.files, ctx.config.policy.qa_ui_paths),
                  ci_not_run=facts.ci == "not-run")
        _save(ctx, key, qa)
        if facts.ci == "not-run":
            cases.update(ctx, key, ci_not_run=True)   # case-wide: covers every PR of the set
            ctx.board.post(card, f"CI did not run on head `{head[:12]}`: GitHub refused to start it (account "
                           "billing). Not red; QA on this exact head is the gate.",
                           idempotency_key=f"qa-ci-not-run:{key}:{head}")

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
    verdict = judge(out.get("text") or "", head, ui=bool(qa.get("ui")), regs=_regs(ctx, rec))
    if verdict["verdict"] == "RERUN":
        if int(qa.get("reruns") or 0) < 1:
            qa.update(run=None, reruns=int(qa.get("reruns") or 0) + 1)
            _save(ctx, key, qa)
            ctx.board.post(card, f"QA re-runs once on head `{head[:12]}`: {verdict['reason']}. Not a QA fail "
                           "and not a dev round.", idempotency_key=f"qa-rerun:{key}:{head}")
            return "rerun"
        verdict = dict(verdict.get("then") or {"verdict": "BLOCKED", "reason": verdict["reason"]})
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
    if verdict["verdict"] != "PASS" and verdict.get("kind") == "EVIDENCE":
        return evidence_block(ctx, key, rec, head, verdict["missing"])
    if verdict["verdict"] != "PASS":
        if verdict.get("failing_check") and ctx.config.policy.qa_regression_set:
            grown = regressions.added(cases.load(ctx)[key], check=verdict["failing_check"],
                                      defect_class=verdict.get("defect_class", ""), head=head, pr=ref)
            if grown is not None:
                cases.update(ctx, key, qa_regressions=grown)
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
        why = small_fix.why_not(ctx, refs, checked) or (
            "CI did not run on a PR head" if cases.load(ctx)[key].get("ci_not_run") else "")
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
