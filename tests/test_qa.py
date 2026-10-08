"""QA stage: an independent check of the exact PR head before any merge vote.

Every test drives the real jobs against fakes and asserts what landed: the
card's tags, its posts, the votes file, the PR state. Nothing greps source.
"""
from __future__ import annotations

import unittest

from sayso import cases, stages, votes
from sayso.adapters.base import PrFacts
from sayso.jobs import dev, qa
from tests.helpers import LoopTest

HEAD = "b" * 40
HEAD2 = "c" * 40


def passing(head=HEAD, extra=""):
    return f"Ran the tests.\n{extra}CUSTOMER-FIX: YES export downloads the file\nVERDICT: PASS {head}"


class QaLoop(LoopTest):
    def to_qa(self, head=HEAD):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        run_id = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(run_id, pr=7, head=head)
        self.tick("dev")
        return key, rec["card_id"]

    def stage(self, card):
        return stages.stage_of(self.tags(card))

    def qa_run(self, key):
        return cases.load(self.ctx)[key]["qa"]["run"]

    def finish_qa(self, key, text):
        self.ctx.qa_runner.finish(self.qa_run(key), text)

    def posts(self, card):
        return "\n".join(self.ctx.board.messages(card))

    def merge_votes(self):
        return [v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]

    def set_pr(self, **fields):
        self.w.data["prs"]["7"].update(fields)
        self.w.save()


class GateTests(QaLoop):
    def test_pr_waits_in_qa_no_merge_vote_before_qa(self):
        key, card = self.to_qa()
        self.assertEqual(self.stage(card), stages.IN_QA)
        self.assertEqual(self.merge_votes(), [], "no merge vote may open before QA passes")

    def test_ci_pending_waits(self):
        key, card = self.to_qa()
        self.set_pr(ci="pending")
        self.assertEqual(self.tick("qa")["qa"][key], "wait-ci")
        self.assertEqual(self.w.data.get("red_proof_calls"), None)
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_ci_red_stays_in_qa_and_says_so(self):
        key, card = self.to_qa()
        self.set_pr(ci="red")
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_QA)
        self.assertIn("CI is red", self.posts(card))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_no_test_file_goes_back_to_dev(self):
        key, card = self.to_qa()
        self.set_pr(files=["src/app.py"])
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertIn("no test file added or changed", self.posts(card))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_unreadable_file_list_fails_closed(self):
        key, card = self.to_qa()
        self.set_pr(files=[])
        self.tick("qa")
        self.assertIn("changed files unreadable", self.posts(card))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_unreadable_diff_fails_closed(self):
        key, card = self.to_qa()
        self.set_pr(added=None)
        self.tick("qa")
        self.assertIn("diff unreadable", self.posts(card))

    def test_secret_in_diff_refused(self):
        key, card = self.to_qa()
        self.set_pr(added=["API = 'sk_live_abcdef123456'"])
        self.tick("qa")
        self.assertIn("secret key", self.posts(card))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_real_email_in_diff_refused_test_domains_allowed(self):
        key, card = self.to_qa()
        self.set_pr(added=["to = 'pat@school.edu'"])
        self.tick("qa")
        self.assertIn("real-looking email", self.posts(card))
        ok = qa.gate_findings(PrFacts("green", ("a.py", "tests/test_a.py"),
                                             ("x = 'pat@customer.test'", "y = 'a@example.com'")))
        self.assertEqual(ok, ("pass", []))


class RedProofTests(QaLoop):
    def test_tests_that_pass_on_old_code_go_back(self):
        key, card = self.to_qa()
        self.w.data["red_proof"] = {"state": "not-red"}
        self.w.save()
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertIn("also pass on the old code", self.posts(card))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_head_red_goes_back(self):
        key, card = self.to_qa()
        self.w.data["red_proof"] = {"state": "head-red", "reason": "test_export fails on the head"}
        self.w.save()
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertIn("test_export fails on the head", self.posts(card))

    def test_check_error_is_not_a_verdict_and_is_bounded(self):
        key, card = self.to_qa()
        self.w.data["red_proof"] = "raise"
        self.w.save()
        for _ in range(5):
            self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_QA, "a check that cannot run is never a code verdict")
        self.assertEqual(len(self.w.data["red_proof_calls"]), 3, "bounded retries")
        self.assertTrue(any("could not run 3 times" in a for a in self.w.data["alerts"]))
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_proof_runs_once_per_head(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.tick("qa")
        self.assertEqual(self.w.data["red_proof_calls"], [[7, HEAD]])


class VerdictTests(QaLoop):
    def test_pass_opens_merge_vote_for_exact_head(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, passing())
        self.tick("qa")
        (v,) = self.merge_votes()
        self.assertEqual(v["spec"]["head"], HEAD)
        self.assertEqual(self.stage(card), stages.AWAITING)

    def test_brief_carries_limits_head_and_customer_words(self):
        key, card = self.to_qa()
        self.tick("qa")
        brief = next(iter(self.w.data["qa_runs"].values()))["brief"]
        for limit in qa.REQUIRED_QA_BOUNDARY:
            self.assertIn(limit, brief)
        self.assertIn(f"VERDICT: PASS {HEAD}", brief)
        self.assertIn("It fails.", brief, "QA must see the customer's own words")

    def test_brief_without_limits_refused(self):
        with self.assertRaises(qa.BriefUnsafe):
            qa.assert_brief_safe("check it please")

    def test_one_qa_run_per_head_across_ticks(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 1)

    def test_blocked_goes_back_to_dev_with_failing_check(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, "FAILING-CHECK: pytest tests/test_export.py -> 1 failed\n"
                            f"VERDICT: BLOCKED {HEAD} export still empty")
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertIn("pytest tests/test_export.py", self.posts(card))
        self.assertEqual(self.merge_votes(), [])

    def test_new_dev_round_brief_carries_qa_reason(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, f"VERDICT: BLOCKED {HEAD} export still empty")
        self.tick("qa", "dev")
        runs = list(self.w.data["runs"].values())
        self.assertEqual(len(runs), 2, "a card QA sends back gets a new dev run")
        self.assertIn("export still empty", runs[1]["brief"])

    def test_two_rounds_then_a_human(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, f"VERDICT: BLOCKED {HEAD} round one")
        self.tick("qa", "dev")
        run2 = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(run2, pr=7, head=HEAD2)
        self.tick("dev", "qa")
        self.finish_qa(key, f"VERDICT: BLOCKED {HEAD2} round two")
        self.tick("qa", "dev")
        self.assertEqual(self.stage(card), stages.BLOCKED)
        self.assertEqual(len(self.w.data["runs"]), 2, "no third automatic dev run")
        self.assertIn("narrow the fix, split it, or park it", self.posts(card))

    def test_pass_without_customer_fix_line_is_blocked(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, f"All good.\nVERDICT: PASS {HEAD}")
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("did not answer whether this fixes", self.posts(card))

    def test_customer_fix_no_wins(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, f"CUSTOMER-FIX: NO the button still does nothing on Safari\nVERDICT: PASS {HEAD}")
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("still does nothing on Safari", self.posts(card))

    def test_verdict_for_other_head_is_blocked(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, passing(head="d" * 40))
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("not the exact head", self.posts(card))

    def test_head_moved_restarts_qa_on_new_head(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.set_pr(head=HEAD2)
        self.finish_qa(key, passing())
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [], "a PASS for the old head approves nothing")
        self.assertEqual(cases.load(self.ctx)[key]["qa"]["head"], HEAD2)
        self.assertEqual(len(self.w.data["qa_runs"]), 2)

    def test_ui_change_needs_evidence(self):
        self.ctx.config = _with_policy(self.ctx.config, qa_ui_paths=r"\.tsx$")
        key, card = self.to_qa()
        self.set_pr(files=["src/Export.tsx", "src/Export.test.tsx"])
        self.tick("qa")
        brief = next(iter(self.w.data["qa_runs"].values()))["brief"]
        self.assertIn("REAL running app", brief)
        self.finish_qa(key, passing())
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("without real-app EVIDENCE", self.posts(card))

    def test_ui_change_with_evidence_passes(self):
        self.ctx.config = _with_policy(self.ctx.config, qa_ui_paths=r"\.tsx$")
        key, card = self.to_qa()
        self.set_pr(files=["src/Export.tsx", "src/Export.test.tsx"])
        self.tick("qa")
        self.finish_qa(key, passing(extra="EVIDENCE: qa/evidence/head.webm\n"))
        self.tick("qa")
        self.assertEqual(len(self.merge_votes()), 1)


class UsageLimitTests(QaLoop):
    def test_limit_is_not_a_fail_and_not_a_round(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, "Error: You've hit your weekly limit. Try again later.")
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_QA)
        self.assertIn("not a QA fail and not a dev round", self.posts(card))
        self.assertEqual(len(self.w.data["runs"]), 1)
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 2, "QA re-runs on the same head")

    def test_limit_retries_are_bounded(self):
        self.ctx.config = _with_policy(self.ctx.config, qa_limit_retries=2)
        key, card = self.to_qa()
        for _ in range(4):
            self.tick("qa")
            run = cases.load(self.ctx)[key]["qa"].get("run")
            if run and run != "limited" and self.ctx.qa_runner.result(run) is None:
                self.finish_qa(key, "rate_limit_error")
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 2)
        self.assertTrue(any("usage limit 2 times" in a for a in self.w.data["alerts"]))

    def test_limit_words_with_a_blocked_verdict_still_block(self):
        self.assertEqual(qa.judge(f"429 Too Many Requests seen in logs\nVERDICT: BLOCKED {HEAD} retries broken",
                                  HEAD, ui=False)["verdict"], "BLOCKED")


class ReleaseGuardTests(QaLoop):
    def test_merge_vote_without_qa_pass_merges_nothing(self):
        key, card = self.to_qa()
        # A merge vote that did not come from a QA pass (hand-made, or from an old version).
        votes.open_vote(self.ctx, key=f"merge:{key}:7:forged", kind="merge", case_key=key, card_id=card,
                        subject="Pat - export button does nothing", question="merge PR 7?",
                        identity={"case_key": key, "pr": 7}, material={"head": HEAD, "state": "open"},
                        spec={"pr": 7, "head": HEAD})
        self.vote("merge")
        self.tick("votes", "release")
        self.assertEqual(self.ctx.codehost.pull_request(7).state, "open")
        self.assertTrue(any("QA did not pass head" in a for a in self.w.data["alerts"]))


class NoQaRunnerTests(LoopTest):
    def test_without_qa_runner_vote_opens_and_card_says_unchecked(self):
        self.ctx.qa_runner = None
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        self.ctx.dev_runner.finish(cases.load(self.ctx)[key]["dev_run"], pr=7, head=HEAD)
        self.tick("dev")
        self.assertIn("NOT independently checked", "\n".join(self.ctx.board.messages(rec["card_id"])))
        self.assertEqual(len([v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]), 1)


class ParseVerdictTests(unittest.TestCase):
    def test_pass_must_be_last_plain_line(self):
        self.assertEqual(qa.parse_verdict(f"VERDICT: PASS {HEAD}", HEAD)["verdict"], "PASS")
        self.assertEqual(qa.parse_verdict(f"VERDICT: PASS {HEAD}\nmore words", HEAD)["verdict"], "BLOCKED")
        self.assertEqual(qa.parse_verdict(f"```\nVERDICT: PASS {HEAD}\n```", HEAD)["verdict"], "BLOCKED")
        self.assertEqual(qa.parse_verdict(f"> VERDICT: PASS {HEAD}", HEAD)["verdict"], "BLOCKED")
        # An unclosed fence: the raw last line is the exact PASS, but it is inside code.
        self.assertEqual(qa.parse_verdict(f"Example:\n```\nVERDICT: PASS {HEAD}", HEAD)["verdict"], "BLOCKED")
        self.assertEqual(qa.parse_verdict(f"VERDICT: PASS {HEAD} but flaky", HEAD)["verdict"], "BLOCKED")

    def test_blocked_anywhere_wins(self):
        text = f"```\nVERDICT: BLOCKED {HEAD} broken\n```\nVERDICT: PASS {HEAD}"
        self.assertEqual(qa.parse_verdict(text, HEAD)["verdict"], "BLOCKED")

    def test_customer_fix_quoted_no_does_not_count_but_plain_no_does(self):
        self.assertEqual(qa.customer_fix_problem("> CUSTOMER-FIX: NO example\nCUSTOMER-FIX: YES fixed"), "")
        self.assertIn("broken", qa.customer_fix_problem("CUSTOMER-FIX: YES ok\nCUSTOMER-FIX: NO broken"))


def _with_policy(cfg, **changes):
    import dataclasses
    return dataclasses.replace(cfg, policy=dataclasses.replace(cfg.policy, **changes))


if __name__ == "__main__":
    unittest.main()
