"""QA block kinds, one re-run on a QA tool timeout, and CI that GitHub never started.

Ported from the PhonicsMaker loop. Every loop test drives the real jobs on fakes
and asserts what landed: the card stage, its posts, the dev runs, the votes.
"""
from __future__ import annotations

import dataclasses
import unittest

from sayso import cases, stages
from sayso.jobs import qa
from tests.test_qa import HEAD, QaLoop, passing

EVIDENCE = (f"No code defect found; the paid render could not run here.\n"
            f"BLOCK-KIND: EVIDENCE a paid render of the export\nVERDICT: BLOCKED {HEAD} proof missing")
TIMEOUT = (f"The full suite ran over the QA tool time limit; the QA tool timed out after 420 seconds.\n"
           f"VERDICT: BLOCKED {HEAD} suite did not finish")


class BlockKindRules(unittest.TestCase):
    def test_clean_evidence(self):
        self.assertEqual(qa.block_kind(EVIDENCE), ("EVIDENCE", "a paid render of the export"))

    def test_any_failing_check_is_a_defect_even_quoted_or_fenced(self):
        for wrap in ("FAILING-CHECK: x -> y", "> FAILING-CHECK: x -> y", "```\nFAILING-CHECK: x -> y\n```"):
            with self.subTest(wrap=wrap):
                self.assertEqual(qa.block_kind(wrap + "\n" + EVIDENCE)[0], "DEFECT")

    def test_unclear_labels_are_never_evidence(self):
        for label in ("BLOCK-KIND: EVIDENCE", "BLOCK-KIND: MAYBE x", "BLOCK-KIND:\nBLOCK-KIND: EVIDENCE x",
                      "> BLOCK-KIND: EVIDENCE x", "```\nBLOCK-KIND: EVIDENCE x\n```", ""):
            with self.subTest(label=label):
                text = f"{label}\nVERDICT: BLOCKED {HEAD} y"
                self.assertNotEqual(qa.block_kind(text)[0], "EVIDENCE")

    def test_defect_beside_evidence_is_defect(self):
        self.assertEqual(qa.block_kind("BLOCK-KIND: DEFECT\n" + EVIDENCE)[0], "DEFECT")

    def test_an_answer_ending_in_a_fence_is_never_evidence(self):
        self.assertEqual(qa.block_kind(EVIDENCE + "\n```\n"), ("", ""))

    def test_timeout_needs_no_failing_check_and_a_plain_end(self):
        self.assertTrue(qa.tool_timeout_block(TIMEOUT))
        self.assertFalse(qa.tool_timeout_block("FAILING-CHECK: pytest -> 1 failed\n" + TIMEOUT))
        self.assertFalse(qa.tool_timeout_block(f"> the tool timed out\nVERDICT: BLOCKED {HEAD} x"))
        self.assertFalse(qa.tool_timeout_block(f"VERDICT: BLOCKED {HEAD} export still empty"))


class BlockKindLoop(QaLoop):
    def run_qa(self, text, head=HEAD):
        key, card = self.to_qa(head)
        self.tick("qa")
        self.finish_qa(key, text)
        self.tick("qa")
        return key, card

    def test_missing_proof_goes_to_a_human_with_no_dev_round(self):
        key, card = self.run_qa(EVIDENCE)
        self.assertEqual(self.stage(card), stages.BLOCKED)
        self.assertIn("missing proof, not a code defect", self.posts(card))
        self.assertIn("a paid render of the export", self.posts(card))
        self.tick("dev", "qa")
        self.assertEqual(len(self.w.data["runs"]), 1, "no dev round spent on missing proof")
        self.assertEqual(self.merge_votes(), [])

    def test_a_defect_still_goes_back_to_dev(self):
        key, card = self.run_qa(f"FAILING-CHECK: pytest -> 1 failed\nBLOCK-KIND: DEFECT\n"
                                f"VERDICT: BLOCKED {HEAD} export still empty")
        self.assertEqual(self.stage(card), stages.IN_DEV)

    def test_evidence_with_a_failing_check_goes_back_to_dev(self):
        key, card = self.run_qa("FAILING-CHECK: pytest -> 1 failed\n" + EVIDENCE)
        self.assertEqual(self.stage(card), stages.IN_DEV)

    def test_a_block_kind_line_beside_a_pass_is_blocked_not_pass(self):
        key, card = self.run_qa(passing(extra="BLOCK-KIND: EVIDENCE a screenshot\n"))
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("BLOCK-KIND line and a PASS verdict", self.posts(card))

    def test_a_kind_on_a_verdict_for_another_head_is_ignored(self):
        other = "d" * 40
        key, card = self.run_qa(EVIDENCE.replace(HEAD, other))
        self.assertEqual(self.stage(card), stages.IN_DEV)

    def test_the_brief_asks_for_the_block_kind(self):
        key, card = self.to_qa()
        self.tick("qa")
        brief = next(iter(self.w.data["qa_runs"].values()))["brief"]
        self.assertIn("BLOCK-KIND: EVIDENCE <what is missing>", brief)
        self.assertIn("BLOCK-KIND: DEFECT", brief)


class ToolTimeoutLoop(QaLoop):
    def test_a_tool_timeout_reruns_qa_once_with_no_dev_round(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, TIMEOUT)
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_QA)
        self.assertIn("re-runs once", self.posts(card))
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 2, "one fresh QA run on the same head")
        self.finish_qa(key, passing())
        self.tick("qa")
        self.assertEqual(len(self.merge_votes()), 1)
        self.assertEqual(len(self.w.data["runs"]), 1, "no dev round spent")

    def test_a_second_timeout_is_a_normal_block(self):
        key, card = self.to_qa()
        for _ in range(2):
            self.tick("qa")
            self.finish_qa(key, TIMEOUT)
            self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertIn("suite did not finish", self.posts(card))

    def test_a_timeout_beside_a_failing_check_is_never_rerun(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, "FAILING-CHECK: pytest -> 1 failed\n" + TIMEOUT)
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_DEV)
        self.assertEqual(len(self.w.data["qa_runs"]), 1)


class CiNotRunLoop(QaLoop):
    def test_ci_github_never_started_lets_qa_run_and_says_so(self):
        key, card = self.to_qa()
        self.set_pr(ci="not-run")
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 1, "QA on the exact head is the gate")
        self.assertIn("GitHub refused to start it", self.posts(card))

    def test_not_run_does_not_excuse_a_content_fault(self):
        key, card = self.to_qa()
        self.set_pr(ci="not-run", files=["src/app.py"])
        self.tick("qa")
        self.assertEqual(self.w.data["qa_runs"], {})
        self.assertIn("no test file", self.posts(card))

    def test_real_red_still_stops_qa(self):
        key, card = self.to_qa()
        self.set_pr(ci="red")
        self.tick("qa")
        self.assertEqual(self.w.data["qa_runs"], {})

    def test_not_run_never_counts_as_a_small_fix(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, policy=dataclasses.replace(
            self.ctx.config.policy, small_fix_release=True))
        key, card = self.to_qa()
        self.set_pr(ci="not-run")
        self.tick("qa")
        self.finish_qa(key, passing())
        self.tick("qa")
        self.assertIn("CI did not run", self.posts(card))
        self.assertEqual(len(self.merge_votes()), 1)
        self.assertEqual(self.merge_votes()[0].get("decided_via"), None, "a vote, not a policy merge")
        self.assertTrue(cases.load(self.ctx)[key].get("ci_not_run"))


if __name__ == "__main__":
    unittest.main()
