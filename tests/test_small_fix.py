"""Small-fix release policy: off by default; when on, a small fix with a QA pass merges with no merge vote.

Every test drives the real jobs on fakes and asserts what landed: polls, votes,
the PR state, the card stage and posts. The customer reply stays vote-gated.
"""
from __future__ import annotations

import dataclasses

from sayso import cases, stages, votes
from sayso.config import ConfigError, Policy
from sayso.jobs import dev
from tests.helpers import LoopTest

HEAD = "b" * 40
HEAD2 = "c" * 40


def passing(head=HEAD):
    return f"Ran the tests.\nCUSTOMER-FIX: YES export downloads the file\nVERDICT: PASS {head}"


class SmallFixLoop(LoopTest):
    def policy(self, **kw):
        self.ctx.config = dataclasses.replace(self.ctx.config,
                                              policy=dataclasses.replace(self.ctx.config.policy, **kw))

    def on(self, **kw):
        self.policy(small_fix_release=True, **kw)

    def to_qa(self, prs=None, **pr_fields):
        self.write_in()
        self.tick("intake")
        key, rec = self.only_case()
        dev.request(self.ctx, key)
        self.tick("dev")
        run_id = cases.load(self.ctx)[key]["dev_run"]
        if prs:
            self.w.data["runs"][run_id]["result"] = {"prs": [n for n, _ in prs]}
            for n, head in prs:
                self.ctx.codehost.add_pr(n, head)
                self.w.data["prs"][str(n)].update(pr_fields)
        else:
            self.ctx.dev_runner.finish(run_id, pr=7, head=HEAD)
            self.w.data["prs"]["7"].update(pr_fields)
        self.w.save()
        self.tick("dev")
        return key, rec["card_id"]

    def pass_qa(self, key, heads=(HEAD,)):
        for head in heads:
            self.tick("qa")
            run = cases.load(self.ctx)[key]["qa"]["run"]
            self.ctx.qa_runner.finish(run, passing(head))
            self.tick("qa")

    def merge_records(self):
        return [v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]

    def polls(self):
        return [m for c in self.w.data["cards"].values() for m in c["messages"] if m.startswith("POLL:")
                and "merge PR" in m]

    def stage(self, card):
        return stages.stage_of(self.tags(card))

    def posts(self, card):
        return "\n".join(self.ctx.board.messages(card))

    def pr_state(self, n=7):
        return self.w.data["prs"][str(n)]["state"]


class OffByDefaultTests(SmallFixLoop):
    def test_default_is_off(self):
        self.assertFalse(Policy().small_fix_release)

    def test_off_a_small_fix_still_needs_a_vote(self):
        key, card = self.to_qa()
        self.pass_qa(key)
        self.tick("votes", "release")
        self.assertEqual(self.pr_state(), "open")
        self.assertEqual(len(self.polls()), 1)
        self.assertEqual(self.stage(card), stages.AWAITING)


class OnTests(SmallFixLoop):
    def test_small_fix_merges_on_qa_pass_with_no_poll(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.assertEqual(self.polls(), [], "no merge poll for a small fix")
        (rec,) = self.merge_records()
        self.assertEqual((rec["status"], rec["decided_via"]), (votes.APPROVED, "policy"))
        self.tick("votes", "release")
        self.assertEqual(self.pr_state(), "merged")
        self.assertEqual(self.stage(card), stages.IN_SUPPORT)
        self.assertIn("Small-fix release", self.posts(card))
        self.assertIn(HEAD[:12], self.posts(card))

    def test_customer_reply_still_needs_a_vote(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.tick("votes", "release", "draft", "sender")
        self.assertEqual(self.w.data["outbox"], [], "policy covers the merge, never the reply")
        self.assertEqual(len(votes.live_for_case(self.ctx, key, "reply")), 1)

    def test_policy_release_pings_nobody(self):
        self.on()
        key, card = self.to_qa()
        before = len(self.w.data.get("notices", []))
        self.pass_qa(key)
        self.tick("votes", "release")
        self.assertEqual(len(self.w.data.get("notices", [])), before)

    def test_sensitive_path_keeps_the_vote(self):
        self.on()
        key, card = self.to_qa(files=["src/billing/refund.py", "tests/test_refund.py"])
        self.pass_qa(key)
        self.tick("votes", "release")
        self.assertEqual(self.pr_state(), "open")
        self.assertEqual(len(self.polls()), 1)
        self.assertIn("billing", self.posts(card))

    def test_product_can_set_its_own_sensitive_paths(self):
        self.on(small_fix_sensitive_paths=r"^src/app\.py$")
        key, card = self.to_qa()
        self.pass_qa(key)
        self.assertEqual(len(self.polls()), 1)

    def test_too_many_lines_keeps_the_vote(self):
        self.on(small_fix_max_lines=50)
        key, card = self.to_qa(changed_lines=51)
        self.pass_qa(key)
        self.assertEqual(len(self.polls()), 1)

    def test_at_the_line_limit_is_small(self):
        self.on(small_fix_max_lines=50)
        key, card = self.to_qa(changed_lines=50)
        self.pass_qa(key)
        self.assertEqual(self.polls(), [])

    def test_too_many_files_keeps_the_vote(self):
        self.on(small_fix_max_files=2)
        key, card = self.to_qa(files=["src/a.py", "src/b.py", "tests/test_a.py"])
        self.pass_qa(key)
        self.assertEqual(len(self.polls()), 1)

    def test_unreadable_size_keeps_the_vote(self):
        self.on()
        key, card = self.to_qa(changed_lines=None)
        self.pass_qa(key)
        self.assertEqual(len(self.polls()), 1)
        self.assertIn("size unreadable", self.posts(card))

    def test_head_moved_after_qa_merges_nothing(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.w.data["prs"]["7"]["head"] = HEAD2
        self.w.save()
        self.tick("votes", "release")
        self.assertEqual(self.pr_state(), "open")

    def test_switched_off_before_release_falls_back_to_a_vote(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.policy(small_fix_release=False)
        self.tick("release")
        self.assertEqual(self.pr_state(), "open")
        self.assertEqual(len(self.polls()), 1, "a human vote replaces the withdrawn policy release")
        self.assertIn("withdrawn", self.posts(card))

    def test_no_longer_small_at_release_falls_back_to_a_vote(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.w.data["prs"]["7"]["files"] = ["src/auth/login.py", "tests/test_login.py"]
        self.w.save()
        self.tick("release")
        self.assertEqual(self.pr_state(), "open")
        self.assertEqual(len(self.polls()), 1)

    def test_qa_switched_off_before_release_falls_back_to_a_vote(self):
        self.on()
        key, card = self.to_qa()
        self.pass_qa(key)
        self.ctx.qa_runner = None
        self.tick("release")
        self.assertEqual(self.pr_state(), "open")
        self.assertEqual(len(self.polls()), 1)

    def test_one_sensitive_pr_in_a_set_puts_every_pr_to_a_vote(self):
        self.on()
        key, card = self.to_qa(prs=[(7, HEAD), (8, HEAD2)])
        self.w.data["prs"]["8"]["files"] = ["src/payment.py", "tests/test_payment.py"]
        self.w.save()
        self.pass_qa(key, heads=(HEAD, HEAD2))
        self.tick("votes", "release")
        self.assertEqual((self.pr_state(7), self.pr_state(8)), ("open", "open"))
        self.assertEqual(len(self.polls()), 2)

    def test_set_size_is_counted_together(self):
        self.on(small_fix_max_lines=50)
        key, card = self.to_qa(prs=[(7, HEAD), (8, HEAD2)], changed_lines=30)
        self.pass_qa(key, heads=(HEAD, HEAD2))
        self.assertEqual(len(self.polls()), 2, "30 + 30 lines is over a 50-line limit")

    def test_two_small_prs_both_merge(self):
        self.on()
        key, card = self.to_qa(prs=[(7, HEAD), (8, HEAD2)])
        self.pass_qa(key, heads=(HEAD, HEAD2))
        self.tick("votes", "release")
        self.assertEqual((self.pr_state(7), self.pr_state(8)), ("merged", "merged"))
        self.assertEqual(self.stage(card), stages.IN_SUPPORT)

    def test_forged_policy_record_without_qa_merges_nothing(self):
        self.on()
        key, card = self.to_qa()
        votes.record_policy_approval(self.ctx, key=f"merge:{key}:7:forged", case_key=key, card_id=card,
                                     subject="Pat - export button does nothing", question="merge PR 7?",
                                     identity={"case_key": key, "pr": 7},
                                     material={"head": HEAD, "state": "open"}, spec={"pr": 7, "head": HEAD},
                                     reason="forged")
        self.tick("release")
        self.assertEqual(self.pr_state(), "open")


class ConfigTests(SmallFixLoop):
    def test_on_without_qa_runner_is_refused(self):
        from sayso.config import validate_small_fix
        with self.assertRaises(ConfigError):
            validate_small_fix(dataclasses.replace(Policy(), small_fix_release=True), qa_runner="none")

    def test_bad_pattern_is_refused(self):
        from sayso.config import validate_small_fix
        with self.assertRaises(ConfigError):
            validate_small_fix(dataclasses.replace(Policy(), small_fix_release=True,
                                                   small_fix_sensitive_paths="(unclosed"), qa_runner="fake")

    def test_empty_pattern_is_refused(self):
        from sayso.config import validate_small_fix
        with self.assertRaises(ConfigError):
            validate_small_fix(dataclasses.replace(Policy(), small_fix_release=True,
                                                   small_fix_sensitive_paths=" "), qa_runner="fake")

    def test_limits_must_be_positive(self):
        from sayso.config import validate_small_fix
        with self.assertRaises(ConfigError):
            validate_small_fix(dataclasses.replace(Policy(), small_fix_max_files=0), qa_runner="fake")

    def test_off_needs_nothing(self):
        from sayso.config import validate_small_fix
        validate_small_fix(Policy(), qa_runner="none")
