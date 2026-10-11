"""QA regression set: every check QA failed a case on is re-run on every later head.

Drives the real dev and QA jobs on fakes and asserts what landed: briefs, case
record, card posts, tags. Nothing greps source.
"""
from __future__ import annotations

import dataclasses
import unittest

from sayso import cases, stages
from sayso.config import ConfigError, Policy
from sayso.jobs import qa
from tests.test_qa import HEAD, HEAD2, QaLoop

HEAD3 = "d" * 40


def blocked(head, check, klass="unchecked text reaches the save"):
    return f"FAILING-CHECK: {check}\nDEFECT-CLASS: {klass}\nVERDICT: BLOCKED {head} still broken"


def passing(head, regs=""):
    return f"{regs}CUSTOMER-FIX: YES works\nVERDICT: PASS {head}"


class RegressionSetTests(QaLoop):
    def set_policy(self, **kw):
        self.ctx.config = dataclasses.replace(self.ctx.config,
                                              policy=dataclasses.replace(self.ctx.config.policy, **kw))

    def block_round_one(self, check="node grid.mjs -> saves clouds"):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, blocked(HEAD, check))
        self.tick("qa")
        return key, card

    def second_head_in_qa(self, key):
        self.tick("dev")
        run2 = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(run2, pr=7, head=HEAD2)
        self.tick("dev", "qa")

    def qa_brief(self, n=-1):
        return list(self.w.data["qa_runs"].values())[n]["brief"]

    def test_a_blocked_head_adds_its_check_and_class_to_the_case(self):
        key, card = self.block_round_one()
        regs = cases.load(self.ctx)[key]["qa_regressions"]
        self.assertEqual([(e["n"], e["check"], e["defect_class"], e["head"]) for e in regs],
                         [(1, "node grid.mjs -> saves clouds", "unchecked text reaches the save", HEAD)])
        self.assertEqual(self.stage(card), stages.IN_DEV)

    def test_the_next_dev_brief_carries_the_set_and_the_class_rule(self):
        key, card = self.block_round_one()
        self.tick("dev")
        brief = list(self.w.data["runs"].values())[-1]["brief"]
        self.assertIn("node grid.mjs -> saves clouds", brief)
        self.assertIn("Fix the CLASS, not the instance", brief)
        self.assertIn("run EVERY check above", brief)

    def test_the_first_dev_and_qa_briefs_have_no_set(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.assertNotIn("REGRESSION SET", self.qa_brief())
        self.assertNotIn("Regression set", list(self.w.data["runs"].values())[0]["brief"])

    def test_the_next_qa_brief_lists_the_set_and_asks_for_a_line_per_entry(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        b = self.qa_brief()
        self.assertIn(HEAD2, b)
        self.assertIn("REGRESSION SET", b)
        self.assertIn("node grid.mjs -> saves clouds", b)
        self.assertIn("REGRESSION: <n> PASS", b)
        qa.assert_brief_safe(b)

    def test_a_pass_reporting_every_entry_opens_the_merge_vote(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, passing(HEAD2, "REGRESSION: 1 PASS\n"))
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.AWAITING)
        self.assertEqual(len(self.merge_votes()), 1)

    def test_an_earlier_check_failing_again_blocks_the_head(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, passing(HEAD2, "REGRESSION: 1 FAIL still saves clouds\n"))
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("fail again on this head", self.posts(card))
        self.assertEqual(self.stage(card), stages.BLOCKED)   # round 2 of 2 was the last

    def test_a_missing_report_reruns_qa_once_then_blocks(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, passing(HEAD2))
        self.tick("qa")
        self.assertEqual(self.stage(card), stages.IN_QA)
        self.assertIn("QA re-runs once", self.posts(card))
        self.assertEqual(self.merge_votes(), [])
        self.tick("qa")
        self.assertEqual(len(self.w.data["qa_runs"]), 3, "one fresh QA run on the same head")
        self.finish_qa(key, passing(HEAD2))
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertEqual(self.stage(card), stages.BLOCKED)
        self.assertEqual(len(self.w.data["qa_runs"]), 3, "never a second re-run")

    def test_regression_lines_in_a_fence_do_not_count(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, passing(HEAD2, "```\nREGRESSION: 1 PASS\n```\n"))
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])

    def test_a_fail_line_wins_over_a_later_pass_for_the_same_entry(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, passing(HEAD2, "REGRESSION: 1 FAIL still saves\nREGRESSION: 1 PASS\n"))
        self.tick("qa")
        self.assertEqual(self.merge_votes(), [])
        self.assertIn("fail again on this head", self.posts(card))

    def test_the_same_check_is_kept_once(self):
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, blocked(HEAD2, "node grid.mjs -> saves clouds"))
        self.tick("qa")
        self.assertEqual(len(cases.load(self.ctx)[key]["qa_regressions"]), 1)

    def test_a_second_new_check_is_appended(self):
        self.set_policy(max_dev_rounds=3)
        key, card = self.block_round_one()
        self.second_head_in_qa(key)
        self.finish_qa(key, blocked(HEAD2, "node input.mjs -> saves clouds"))
        self.tick("qa")
        regs = cases.load(self.ctx)[key]["qa_regressions"]
        self.assertEqual([e["n"] for e in regs], [1, 2])
        self.tick("dev")
        brief = list(self.w.data["runs"].values())[-1]["brief"]
        self.assertIn("node grid.mjs", brief)
        self.assertIn("node input.mjs", brief)

    def test_policy_off_records_and_requires_nothing(self):
        self.set_policy(qa_regression_set=False)
        key, card = self.block_round_one()
        self.assertNotIn("qa_regressions", cases.load(self.ctx)[key])
        self.second_head_in_qa(key)
        self.assertNotIn("REGRESSION SET", self.qa_brief())

    def test_a_fenced_failing_check_is_not_recorded(self):
        key, card = self.to_qa()
        self.tick("qa")
        self.finish_qa(key, f"```\nFAILING-CHECK: <cmd> -> <what>\n```\nVERDICT: BLOCKED {HEAD} broken")
        self.tick("qa")
        self.assertNotIn("qa_regressions", cases.load(self.ctx)[key])

    def test_the_setting_must_be_a_boolean(self):
        from sayso import config
        raw = {"policy": {"qa_regression_set": "yes"}}
        merged = self._raw_config()
        merged["policy"] = {**merged.get("policy", {}), **raw["policy"]}
        with self.assertRaisesRegex(ConfigError, "qa_regression_set must be true or false"):
            config.parse(merged, self.ctx.config.path)

    def _raw_config(self):
        import tomllib
        return tomllib.loads(self.ctx.config.path.read_text())

    def test_the_default_is_on(self):
        self.assertTrue(Policy().qa_regression_set)


if __name__ == "__main__":
    unittest.main()
