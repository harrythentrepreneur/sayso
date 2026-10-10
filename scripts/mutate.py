#!/usr/bin/env python3
"""Seeded-fault proof: delete each safety guard in a SCRATCH COPY and require a red test.

A guard that can be removed with every test still green is untested. Each
mutation below names the rule it breaks. The harness:
  1. copies the repo to a temp dir (never edits the working tree);
  2. proves the unmodified copy is GREEN (a broken harness looks like broken code);
  3. applies one mutation at a time, asserts the text actually changed, runs the
     suite, and requires a FAILURE;
  4. prints one verdict line: `MUTATION VERDICT: PASS n/n` or `FAIL`.
Exit code is 0 only when every mutation is killed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = [
    ("sender: fingerprint check removed", "sayso/jobs/sender.py",
     'if not votes.still_bound(rec, {"sha256": body_sha}):', "if False:"),
    ("sender: stage check removed", "sayso/jobs/sender.py",
     "if stages.stage_of(tags) != stages.AWAITING:", "if False:"),
    ("sender: re-send on unresolved attempt", "sayso/jobs/sender.py",
     "resend = att is None", "resend = True"),
    ("sender: mailbox receipt ignored", "sayso/jobs/sender.py",
     "if ctx.mailbox.sent_copies(row.message_id) != 1:", "if False:"),
    ("sender: helpdesk status ignored", "sayso/jobs/sender.py",
     'if row.status != "Sent" or', "if False and"),
    ("sender: money-claim check removed", "sayso/jobs/sender.py",
     "safety.check_claims_backed(body, verified_money(ctx, case_key))", "pass"),
    ("sender: pause ignored", "sayso/jobs/sender.py",
     "    ctx.require_running()\n    import time", "    import time"),
    ("votes: strangers can decide", "sayso/votes.py",
     "counted = {u: a for u, a in votes.items() if u in operators}", "counted = dict(votes); operators = {**operators, **{u: u for u in votes}}"),
    ("votes: operator no ignored", "sayso/votes.py",
     "    if noes:\n        return DECLINED", "    if False:\n        return DECLINED"),
    ("votes: bare-number subject allowed", "sayso/votes.py",
     "if len(text) < 8 or _ID_ONLY.match(text):", "if False:"),
    ("tally: changed material still approved", "sayso/jobs/tally.py",
     "if material is None or not votes.still_bound(rec, material):", "if material is None:"),
    ("intake: new mail no longer voids votes", "sayso/jobs/intake.py",
     'voided = votes.void_case(ctx, key, "the customer wrote again", kinds=frozenset({"reply", "close"}))',
     "voided = []"),
    ("intake: own address treated as customer", "sayso/jobs/intake.py",
     "if sender in ctx.config.own_addresses:", "if False:"),
    ("intake: replay dedupe removed", "sayso/jobs/intake.py",
     "            if msg.message_id in load_json(seen_path):\n                continue",
     "            if False:\n                continue"),
    ("cases: tag read-back removed", "sayso/cases.py",
     "    if sorted(live) != sorted(tags):", "    if False:"),
    ("cases: unmatched groups together", "sayso/cases.py",
     "    if kind == UNGROUPABLE:\n        return None", "    if False:\n        return None"),
    ("money: cap removed", "sayso/money.py",
     "if amount > max_minor:", "if False:"),
    ("money: re-execute on unresolved attempt", "sayso/jobs/money_job.py",
     "first = att is None", "first = True"),
    ("money: amount not verified", "sayso/jobs/money_job.py",
     "rows[0].amount == spec[\"amount\"] and ", ""),
    ("money: tampered spec executes", "sayso/jobs/money_job.py",
     "if not votes.still_bound(rec, spec):", "if False:"),
    ("dev: brief limits not enforced", "sayso/jobs/dev.py",
     "    if missing:\n        raise BriefUnsafe", "    if False:\n        raise BriefUnsafe"),
    ("dev: second run per card", "sayso/jobs/dev.py",
     'if not rec.get("dev_run"):', "if True:"),
    ("release: moved head still merges", "sayso/jobs/release.py",
     'elif not votes.still_bound(rec, {"head": pr.head_sha, "state": pr.state}):', "elif False:"),
    ("release: merge not read back", "sayso/jobs/release.py",
     'status = pr_refs.read(ctx, spec["pr"]).state', 'status = "merged"'),
    ("close: customer reply does not void", "sayso/jobs/close.py",
     "        if not still or moved:", "        if not still:"),
    ("close: only the reply ticket checked", "sayso/adapters/fake.py",
     ' or r["sender"].strip().lower() == who', ""),
    ("close: quiet window ignored", "sayso/jobs/close.py",
     "        if ctx.clock.now() - sent < quiet:\n            continue", "        pass"),
    ("config: real adapter allowed in dry-run", "sayso/config.py",
     'if adapter not in ("fake", "none") and mode != "live":', "if False:"),
    ("config: inline secret allowed", "sayso/config.py",
     'if key.endswith("_env") and not _ENV.match(str(value)):', "if False:"),
    ("store: corrupt file read as empty", "sayso/store.py",
     "        raise CorruptState(f\"{p}: {exc}\") from exc", "        return {}"),
    ("stages: labels dropped on move", "sayso/stages.py",
     "return [to] + ([WAITING] if waiting else []) + labels_of(tags)", "return [to] + ([WAITING] if waiting else [])"),
    ("dashboard: POST accepted", "sayso/dashboard.py",
     "        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815\n", ""),
    ("dashboard: customer text not escaped", "sayso/dashboard.py",
     "    return html.escape(\"\" if value is None else str(value), quote=True)",
     "    return \"\" if value is None else str(value)"),
    ("dashboard: listens beyond loopback", "sayso/dashboard.py",
     "    if not loopback:\n        raise RemoteBindRefused", "    if False:\n        raise RemoteBindRefused"),
    ("dashboard: reads files outside state dir", "sayso/dashboard.py",
     "            return None  # only files inside this product's state dir are shown", "            pass"),
    ("dashboard: corrupt state hidden", "sayso/dashboard.py",
     '                status, ctype, body = 500, "text/plain", f"state could not be read: {type(exc).__name__}: {exc}"',
     '                status, ctype, body = 200, "text/html", ""'),
    ("dashboard: board failure guessed", "sayso/dashboard.py",
     '        except Exception:  # noqa: BLE001 - show unknown, never guess\n            return ["unreadable"]',
     '        except Exception:  # noqa: BLE001 - show unknown, never guess\n            return ["Done"]'),
    ("console: csrf not checked", "sayso/console.py",
     "            if not auth.csrf_ok(user, form.get(\"csrf\")):", "            if False:"),
    ("console: stale fingerprint accepted", "sayso/console.py",
     "    if not hmac.compare_digest(str(rec.get(\"fingerprint\")), str(fingerprint)):", "    if False:"),
    ("console: tampered cookie accepted", "sayso/console.py",
     "        if not hmac.compare_digest(self._sign(payload), sig):\n            return None",
     "        if False:\n            return None"),
    ("console: non-operator can be a user", "sayso/console.py",
     "    if str(operator_id) not in config.operators:\n        raise ConsoleError(f\"operator id {operator_id!r} is not in [operators]; only operators can sign in\")",
     "    if False:\n        raise ConsoleError(f\"operator id {operator_id!r} is not in [operators]; only operators can sign in\")"),
    ("console: old password sessions live on", "sayso/console.py",
     "user.get(\"version\") != data.get(\"v\") or ", ""),
    ("console: no lockout", "sayso/console.py",
     "        return len(recent) >= MAX_FAILS", "        return False"),
    ("tally: web vote on old revision counted", "sayso/console.py",
     "if v.get(\"fingerprint\") == rec[\"fingerprint\"] and ", "if "),
    ("sync: console yes overrides a board no", "sayso/jobs/tally.py",
     '        out[uid] = "no" if "no" in (answer, out.get(uid)) else answer', "        out[uid] = answer"),
    ("sync: console votes not shown on the board", "sayso/jobs/tally.py",
     "        _mirror_web_votes(ctx, rec, web)\n", ""),
    ("sync: poll left open after a decision", "sayso/jobs/tally.py",
     "        _close_poll(ctx, rec)\n", ""),
    ("sync: poll close failure undoes the run", "sayso/jobs/tally.py",
     "    except Exception as exc:  # noqa: BLE001 - the decision stands; a stale poll is only cosmetic\n"
     "        ctx.journal.append(\"poll_close_failed\", key=rec[\"key\"], error=type(exc).__name__)",
     "    except KeyError:\n        pass"),
    ("draft: vote opened on a reply the sender refuses", "sayso/jobs/draft.py",
     'sender.check_sendable(ctx, key, rec, rec["customer"], text)', "safety.check_reply_text(text)"),
    ("sender: plugin reply rules ignored", "sayso/safety.py",
     "        if reason:\n            raise Refused", "        if False:\n            raise Refused"),
    ("sender: double Re: subject", "sayso/safety.py",
     '    rest = _RE_PREFIX.sub("", title or "").strip()', '    rest = (title or "").strip()'),
    ("imap: dropped connection not retried", "sayso/adapters/reference.py",
     "        for _ in range(self.READ_ATTEMPTS):", "        for _ in range(1):"),
    ('qa: merge vote opens before QA', 'sayso/jobs/dev.py',
     '        if ctx.qa_runner is not None:\n            continue\n        ctx.board.post(rec["card_id"], "No QA runner is configured:',
     '        if ctx.qa_runner is not None:\n            pass\n        ctx.board.post(rec["card_id"], "No QA runner is configured:'),
    ('qa: pending CI treated as done', 'sayso/jobs/qa.py',
     '    if facts.ci == "pending":\n        return "wait", []', '    if False:\n        return "wait", []'),
    ('qa: red CI ignored', 'sayso/jobs/qa.py',
     '    if facts.ci != "green":', '    if False:'),
    ('qa: no-test PR passes the gate', 'sayso/jobs/qa.py',
     '    elif not any(TEST_PATH.search(p) for p in paths):', '    elif False:'),
    ('qa: unreadable file list passes', 'sayso/jobs/qa.py',
     '    if not paths or any(not p for p in paths):', '    if False:'),
    ('qa: unreadable diff passes', 'sayso/jobs/qa.py',
     '    if facts.added is None:\n        why.append', '    if False:\n        why.append'),
    ('qa: secret in diff passes', 'sayso/jobs/qa.py',
     '        if any(safety.has_secret(ln) for ln in lines):', '        if False:'),
    ('qa: real email in diff passes', 'sayso/jobs/qa.py',
     'if not _SAFE_EMAIL.search(e)):', 'if False):'),
    ('qa: not-red tests accepted', 'sayso/jobs/qa.py',
     '        elif state == "not-red":', '        elif False:'),
    ('qa: check error counted as pass', 'sayso/jobs/qa.py',
     '        if state == "red-proved":\n            qa["red"] = state', '        if state in ("red-proved", "error"):\n            qa["red"] = state'),
    ('qa: red-proof retries unbounded', 'sayso/jobs/qa.py',
     '        if tries >= ctx.config.policy.red_proof_max_tries:', '        if False:'),
    ('qa: brief limits not enforced', 'sayso/jobs/qa.py',
     '    if missing:\n        raise BriefUnsafe(f"QA brief', '    if False:\n        raise BriefUnsafe(f"QA brief'),
    ('qa: second QA run per head', 'sayso/jobs/qa.py',
     '    if not qa.get("run"):', '    if True:'),
    ('qa: blocked anywhere ignored', 'sayso/jobs/qa.py',
     '        if m:\n            return {"verdict": "BLOCKED", "reason": m.group(1)', '        if False:\n            return {"verdict": "BLOCKED", "reason": m.group(1)'),
    ('qa: fenced PASS counted', 'sayso/jobs/qa.py',
     '    if last == f"VERDICT: PASS {head}" and plain_last == last:', '    if last == f"VERDICT: PASS {head}":'),
    ('qa: verdict for any head accepted', 'sayso/jobs/qa.py',
     '    if last == f"VERDICT: PASS {head}"', '    if last.startswith("VERDICT: PASS ")'),
    ('qa: customer-fix answer not required', 'sayso/jobs/qa.py',
     '        problem = customer_fix_problem(text)\n        if problem:', '        problem = customer_fix_problem(text)\n        if False:'),
    ('qa: customer-fix NO ignored', 'sayso/jobs/qa.py',
     '        if answer.upper() == "NO":', '        if False:'),
    ('qa: UI pass without evidence', 'sayso/jobs/qa.py',
     '        if ui and not _EVIDENCE.search(', '        if False and not _EVIDENCE.search('),
    ('qa: usage limit counted as a fail', 'sayso/jobs/qa.py',
     '        if why and not _BLOCKED_ANYWHERE.search(text or ""):', '        if False:'),
    ('qa: usage-limit retries unbounded', 'sayso/jobs/qa.py',
     '        if limited >= ctx.config.policy.qa_limit_retries:', '        if False:'),
    ('qa: dev rounds unbounded', 'sayso/jobs/qa.py',
     '    if rounds >= limit:', '    if False:'),
    ("qa: old head's verdict reused", 'sayso/jobs/qa.py',
     '    if qa.get("head") != pr.head_sha:', '    if not qa:'),
    ('release: merge without a QA pass', 'sayso/jobs/release.py',
     '        if ctx.qa_runner is not None and (case.get("qa_passed_heads") or {}).get(str(spec["pr"])) != spec["head"]:', '        if False:'),
    ('github: unreadable CI read as green', 'sayso/adapters/reference.py',
     '            return "red"                      # unreadable CI is never green', '            return "green"'),
    ('hermes qa: same profile as dev allowed', 'sayso/adapters/reference.py',
     '        if dev_profile and self.profile == dev_profile:', '        if False:'),
    ('owners: first owner changes on later posts', 'sayso/owners.py',
     '    if current["owner_source"] == OVERRIDE or current["owner"] != ALL:\n        return current', '    if current["owner_source"] == OVERRIDE:\n        return current'),
    ('owners: unreadable card guessed', 'sayso/owners.py',
     '        authors = None\n    found', '        authors = []\n    found'),
    ('owners: non-operator poster owns', 'sayso/owners.py',
     '        if str(author) in ctx.config.operators:', '        if True:'),
    ('owners: stranger accepted as owner', 'sayso/owners.py',
     '    raise OwnerError(f"owner must be an operator id', '    return text\n    raise OwnerError(f"owner must be an operator id'),
    ('owners: override without a setter', 'sayso/owners.py',
     '    if setter == ALL:\n        raise OwnerError', '    if False:\n        raise OwnerError'),
    ('votes: poll not labelled', 'sayso/votes.py',
     'f"{tag}{subject} - {question}", idempotency_key=key)', 'f"{subject} - {question}", idempotency_key=key)'),
    ('votes: notice pings for an all vote', 'sayso/votes.py',
     '    mention = owner["owner"] if owners.name_of(ctx, owner["owner"]) else None', '    mention = owner["owner"]'),
    ('runtime: notice may ping a stranger', 'sayso/runtime.py',
     '        if mention is not None and mention not in self.config.operators:', '        if False:'),
    ('discord: notice pings everyone named', 'sayso/adapters/reference.py',
     '"allowed_mentions": {"parse": [], "users": [uid] if uid else []}})', '"allowed_mentions": {"parse": ["users", "roles", "everyone"]}})'),
    ('discord: bot counted as owner', 'sayso/adapters/reference.py',
     '                if not (m.get("author") or {}).get("bot"):', '                if True:'),
    ('discord: only first page of authors read', 'sayso/adapters/reference.py',
     '            if len(rows) < 100:\n                break\n        return [(a, t)', '            break\n        return [(a, t)'),
    ("dev: parallel limit ignored", "sayso/jobs/dev.py",
     "            if active >= ctx.config.policy.max_parallel_dev_runs:", "            if False:"),
    ("dev: live run mislabelled dead", "sayso/adapters/fake.py",
     'return "unknown" if row is None else "dead" if row.get("dead") else "running"',
     'return "unknown" if row is None else "dead"'),
    ("dev: dead run starts a third time", "sayso/jobs/dev.py",
     "            if attempt >= ctx.config.policy.max_dev_rounds:", "            if False:"),
    ("dev: usage silently omitted", "sayso/jobs/dev.py",
     '        if result.get("usage"):', '        if False:'),
    ("dev: second PR forgotten", "sayso/jobs/dev.py",
     '        refs = result.get("prs") or [result.get("pr")]',
     '        refs = [result.get("pr") or (result.get("prs") or [None])[0]]'),
    ("qa: first PR QA opens votes for all", "sayso/jobs/qa.py",
     '    if index + 1 < len(refs):', '    if False:'),
    ("release: one PR counted as whole fix", "sayso/jobs/release.py",
     '        all_refs = case.get("prs") or [spec["pr"]]', '        all_refs = [spec["pr"]]'),
    ("release: external merge without vote counted", "sayso/jobs/release.py",
     '                or any(not any(v["spec"].get("pr") == n and',
     '                or False and any(not any(v["spec"].get("pr") == n and'),
    ("release: QA for another repo accepted", "sayso/jobs/release.py",
     '(case.get("qa_passed_heads") or {}).get(str(spec["pr"])) != spec["head"]',
     'case.get("qa_passed_head") != spec["head"]'),
    ("dev: unlisted repository allowed", "sayso/adapters/reference.py",
     '        if repo not in allowed:', '        if False:'),
    ("dev: agent fabricated usage accepted", "sayso/adapters/reference.py",
     '        if sids:\n            try:', '        if False:\n            try:'),
    ("stall: reminder before the wait", "sayso/jobs/stall.py",
     "            if opened is None or now - opened < remind:", "            if opened is None:"),
    ("stall: reminder repeats every pass", "sayso/jobs/stall.py",
     '    if ident in sent:\n        return False', '    if False:\n        return False'),
    ("stall: decided vote reminded", "sayso/jobs/stall.py",
     '            if v["status"] != votes.OPEN:', '            if False:'),
    ("stall: daily repeat never comes", "sayso/jobs/stall.py",
     "            period = 1 + int((now - opened - remind) / repeat)", "            period = 1"),
    ("stall: owner not pinged", "sayso/jobs/stall.py",
     "    mention = owner if owners.name_of(ctx, owner) else None", "    mention = None"),
    ("stall: card flagged before its limit", "sayso/jobs/stall.py",
     "            if idle >= limit:", "            if True:"),
    ("stall: limit ignores settings", "sayso/jobs/stall.py",
     "            stages.AWAITING: p.stall_awaiting_hours, stages.BLOCKED: p.stall_blocked_hours}",
     "            stages.AWAITING: p.stall_awaiting_hours, stages.BLOCKED: 4}"),
    ("stall: new activity does not reset", "sayso/jobs/stall.py",
     'f"stall:{key}:{stage}:{at.isoformat()}:{period}"', 'f"stall:{key}:{stage}:{period}"'),
    ("stall: parked card flagged", "sayso/jobs/stall.py",
     "            if stage not in hours or policy.parked_label in tags:", "            if stage not in hours:"),
    ("stall: done card flagged", "sayso/jobs/stall.py",
     "            if stage not in hours or policy.parked_label in tags:",
     "            if policy.parked_label in tags or stage not in hours and stage not in (stages.DONE,):"),
    ("stall: unreadable card guessed", "sayso/jobs/stall.py",
     "            if at is None:\n", "            if at is None:\n                at = now\n            if False:\n"),
    ("stall: paused loop notifies", "sayso/jobs/stall.py",
     "    if ctx.paused:\n        ctx.beat", "    if False:\n        ctx.beat"),
    ("stall: orphan before its quiet time", "sayso/jobs/stall.py",
     "            if idle >= orphan_after:", "            if True:"),
    ("stall: open vote counted as orphan", "sayso/jobs/stall.py",
     "    if votes.live_for_case(ctx, key):\n        return None", "    if False:\n        return None"),
    ("stall: awaiting without a vote ignored", "sayso/jobs/stall.py",
     '        return "the card says awaiting approval but no vote is open"', "        return None"),
    ("stall: dev with no runner ignored", "sayso/jobs/stall.py",
     '            return "no dev runner is configured"', "            return None"),
    ("stall: waiting dev slot flagged", "sayso/jobs/stall.py",
     "        return None                                  # running, or waiting for a free slot",
     '        return None if rec.get("dev_run") else "waiting"'),
    ("stall: support with nothing due ignored", "sayso/jobs/stall.py",
     '        return "no draft is due and no vote is open"', "        return None"),
    ("stall: customer address in notice", "sayso/jobs/stall.py",
     "    return f\"{rec['customer'].split('@')[0]} - {rec['title']}\"", "    return f\"{rec['customer']} - {rec['title']}\""),
    ("stall: reference uses post creation time", "sayso/adapters/reference.py",
     '        mid = str(thread.get("last_message_id") or "")', '        mid = str(card_id)'),
    ("small-fix: on by default", "sayso/config.py",
     "    small_fix_release: bool = False", "    small_fix_release: bool = True"),
    ("small-fix: off switch ignored", "sayso/small_fix.py",
     '    if not policy.small_fix_release:\n        return "small-fix release is off"', "    pass"),
    ("small-fix: sensitive path allowed", "sayso/small_fix.py",
     "        if hit:\n            return f\"touches a sensitive path ({hit})\"", "        pass"),
    ("small-fix: line limit off by one", "sayso/small_fix.py",
     "    if lines > policy.small_fix_max_lines:", "    if lines >= policy.small_fix_max_lines:"),
    ("small-fix: line limit ignored", "sayso/small_fix.py",
     "    if lines > policy.small_fix_max_lines:", "    if False:"),
    ("small-fix: file limit ignored", "sayso/small_fix.py",
     "    if files > policy.small_fix_max_files:", "    if False:"),
    ("small-fix: set counted per PR", "sayso/small_fix.py",
     "        lines += facts.changed_lines", "        lines = facts.changed_lines"),
    ("small-fix: unreadable size allowed", "sayso/small_fix.py",
     "        if facts.changed_lines is None:", "        if False:"),
    ("small-fix: unreadable files allowed", "sayso/small_fix.py",
     "        if facts is None or not facts.files or any(not f for f in facts.files):", "        if False:"),
    ("small-fix: product pattern ignored", "sayso/small_fix.py",
     "    pattern = re.compile(policy.small_fix_sensitive_paths)",
     "    from sayso.config import SMALL_FIX_SENSITIVE as _d\n    pattern = re.compile(_d)"),
    ("small-fix: release ignores a removed QA runner", "sayso/jobs/release.py",
     "    if ctx.qa_runner is None or any(str(n) not in heads for n in refs):",
     "    if any(str(n) not in heads for n in refs):"),
    ("small-fix: release does not re-check", "sayso/jobs/release.py",
     '        if rec.get("decided_via") == votes.POLICY and _withdraw_policy(ctx, key, rec, case):',
     "        if False:"),
    ("small-fix: withdrawal opens no vote", "sayso/jobs/release.py",
     "                open_merge_vote(ctx, rec[\"case_key\"], case, pr, ref=v[\"spec\"][\"pr\"], move=False)",
     "                pass"),
    ("small-fix: policy record pings", "sayso/jobs/qa.py",
     "                record_policy_merge(ctx, key, cases.load(ctx)[key], p, ref=n)",
     "                open_merge_vote(ctx, key, cases.load(ctx)[key], p, ref=n, move=False)"),
    ("small-fix: config allows no QA", "sayso/config.py",
     '    if qa_runner == "none":\n        raise', '    if False:\n        raise'),
    ("small-fix: config allows empty pattern", "sayso/config.py",
     "    if not str(policy.small_fix_sensitive_paths).strip():", "    if False:"),
    ("small-fix: config allows zero limits", "sayso/config.py",
     "        if not isinstance(value, int) or isinstance(value, bool) or value < 1:", "        if False:"),
    ("small-fix: github size ignores deletions", "sayso/adapters/reference.py",
     "                        changed += a + d", "                        changed += a"),
    ("small-fix: github missing counts read as zero", "sayso/adapters/reference.py",
     "                        changed = None        # a row without counts: size unreadable",
     "                        pass"),
    ("safety: payment ids allowed", "sayso/safety.py",
     "    if _CARD.search(text) or _PAYMENT_ID.search(text):", "    if False:"),
]


# Mutations that cannot be killed because another guard gives the same answer.
# Each one names the guard that already covers it. They must SURVIVE; if one
# starts failing, the covering guard changed and this list must be re-checked.
EQUIVALENT = [
    ("close: asked twice (votes.open_vote is idempotent by key, so no second poll opens)",
     "sayso/jobs/close.py",
     "if vkey in all_votes or votes.live_for_case(ctx, key):", "if votes.live_for_case(ctx, key):"),
]


def run_suite(where: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          cwd=where, capture_output=True, text=True, timeout=600)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sayso-mutate-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        baseline = run_suite(base)
        if baseline.returncode != 0:
            print(baseline.stderr[-3000:])
            print("MUTATION VERDICT: FAIL baseline not green; harness proves nothing")
            return 2
        killed, survived = 0, []
        for name, rel, old, new in MUTATIONS:
            work = Path(tmp) / "work"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(base, work)
            path = work / rel
            text = path.read_text()
            if text.count(old) != 1:
                survived.append(f"{name} (pattern found {text.count(old)} times; harness stale)")
                print(f"STALE     {name}")
                continue
            path.write_text(text.replace(old, new))
            result = run_suite(work)
            if result.returncode != 0:
                killed += 1
                first = next((ln for ln in result.stderr.splitlines() if ln.startswith(("FAIL:", "ERROR:"))), "?")
                print(f"KILLED    {name}  <- {first}")
            else:
                survived.append(name)
                print(f"SURVIVED  {name}")
        for name, rel, old, new in EQUIVALENT:
            work = Path(tmp) / "work"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(base, work)
            path = work / rel
            path.write_text(path.read_text().replace(old, new))
            if run_suite(work).returncode == 0:
                print(f"EQUIVALENT {name}")
            else:
                survived.append(f"{name} (listed equivalent but now killed; re-check the list)")
                print(f"CHANGED   {name}")
        total = len(MUTATIONS)
        verdict = "PASS" if not survived else "FAIL"
        print(f"MUTATION VERDICT: {verdict} {killed}/{total} killed")
        return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
