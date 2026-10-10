"""Dev-loop tests on fake products: observable runs, cards, PRs and receipts."""
from __future__ import annotations

import dataclasses
import unittest
from sayso import cases, stages
from sayso.jobs import dev
from tests.helpers import LoopTest


class DevLoopTests(LoopTest):
    def case(self, who):
        self.write_in(sender=f"{who}@customer.test", subject=f"Export for {who} broken")
        self.tick("intake")
        key = next(k for k, r in cases.load(self.ctx).items() if r["customer"] == f"{who}@customer.test")
        dev.request(self.ctx, key)
        return key

    def test_parallel_limit_starts_only_two_then_fills_one_free_slot(self):
        keys = [self.case(x) for x in ("pat", "sam", "lee")]
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 2)
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 2)
        first = next(r["dev_run"] for r in cases.load(self.ctx).values() if r.get("dev_run"))
        self.ctx.dev_runner.finish(first, pr=7, head="a" * 40)
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 3)
        self.assertEqual(sum(bool(r.get("dev_run")) for r in cases.load(self.ctx).values()), 3)
    def test_dead_run_restarts_once_and_then_blocks(self):
        key = self.case("pat")
        self.tick("dev")
        first = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][first]["dead"] = True
        self.w.save()
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 2, "one fresh retry")
        second = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][second]["dead"] = True
        self.w.save()
        self.tick("dev")
        self.assertEqual(len(self.w.data["runs"]), 2, "never starts a third run")
        self.assertIn(stages.BLOCKED, self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"]))

    def test_live_run_without_output_is_not_dead(self):
        key = self.case("pat")
        self.tick("dev", "dev")
        self.assertEqual(len(self.w.data["runs"]), 1)
    def test_completed_run_records_tokens_and_minutes_on_case(self):
        key = self.case("pat")
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(rid, pr=7, head="a" * 40,
                                   usage={"tokens_read": 1200, "tokens_out": 350, "minutes": 4.2})
        self.tick("dev")
        rec = cases.load(self.ctx)[key]
        self.assertEqual(rec["dev_usage"][rid]["tokens_read"], 1200)
        self.assertEqual(rec["dev_usage"][rid]["minutes"], 4.2)
        self.assertTrue(any("1,200" in m and "4.2" in m for m in self.ctx.board.messages(rec["card_id"])))
    def test_two_prs_are_not_reported_shipped_after_only_one_merge(self):
        key = self.case("pat")
        self.ctx.qa_runner = None
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": [7, 8], "summary": "Core and web fixes"}
        for n in (7, 8):
            self.ctx.codehost.add_pr(n, str(n) * 40)
        self.tick("dev")
        self.assertEqual(cases.load(self.ctx)[key]["prs"], [7, 8])
        merge_votes = [v for v in __import__('sayso.votes', fromlist=['load']).load(self.ctx).values()
                       if v["kind"] == "merge"]
        self.assertEqual(len(merge_votes), 2)
        self.w.operator_votes(merge_votes[0]["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertNotEqual(stages.stage_of(self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"])),
                            stages.IN_SUPPORT, "a second PR is still open")
        self.w.operator_votes(merge_votes[1]["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertEqual(stages.stage_of(self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"])),
                         stages.IN_SUPPORT)
    def test_two_prs_each_need_qa_before_any_merge_vote(self):
        from sayso import votes
        key = self.case("pat")
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": [7, 8], "summary": "Core and web fixes"}
        for n in (7, 8):
            self.ctx.codehost.add_pr(n, str(n) * 40)
        self.tick("dev", "qa")
        run1 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run1, f"CUSTOMER-FIX: YES core fixed\nVERDICT: PASS {'7'*40}")
        self.tick("qa")
        self.assertEqual([v for v in votes.load(self.ctx).values() if v["kind"] == "merge"], [],
                         "one QA PASS cannot approve both PRs")
        self.tick("qa")
        run2 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run2, f"CUSTOMER-FIX: YES web fixed\nVERDICT: PASS {'8'*40}")
        self.tick("qa")
        self.assertEqual(len([v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]), 2)
    def test_same_pr_number_in_two_repos_gets_distinct_votes_and_receipts(self):
        from sayso import votes
        key = self.case("pat")
        self.ctx.qa_runner = None
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": ["team/core#7", "team/web#7"]}
        self.w.save()
        self.ctx.codehost.for_ref("team/core#7").add_pr(7, "a" * 40)
        self.ctx.codehost.for_ref("team/web#7").add_pr(7, "b" * 40)
        self.tick("dev")
        merge_votes = [v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]
        self.assertEqual(len(merge_votes), 2)
        self.assertEqual({v["spec"]["pr"] for v in merge_votes}, {"team/core#7", "team/web#7"})
        self.w.operator_votes(merge_votes[0]["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertEqual(stages.stage_of(self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"])),
                         stages.AWAITING)
        self.w.operator_votes(merge_votes[1]["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertEqual(stages.stage_of(self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"])),
                         stages.IN_SUPPORT)
    def test_hermes_run_reports_two_repo_prs_and_measured_usage(self):
        import tempfile
        from pathlib import Path
        from sayso.adapters.reference import HermesDevRunner
        from sayso.config import Section
        with tempfile.TemporaryDirectory() as d:
            runner = HermesDevRunner(Section("hermes", {"profile": "dev"}), Path(d), run=lambda *a, **k: None)
            import sqlite3
            db = Path(d) / "state.db"
            con = sqlite3.connect(db)
            con.execute("create table sessions(id text, input_tokens int, cache_read_tokens int, "
                        "cache_write_tokens int, output_tokens int, started_at real, last_activity_at real)")
            con.execute("insert into sessions values ('sid-1', 1000, 200, 0, 350, 100, 352)")
            con.commit(); con.close()
            runner.state_db = db
            rid = runner.start("case", "brief", "dev:case:1")
            (runner.dir / f"{rid}.out").write_text(
                "session_id: fake-customer-sid\nsession_id: sid-1\n=== RESULT ===\nTwo fixes.\n=== END RESULT ===\n"
                "PR: https://github.com/team/core/pull/7, https://github.com/team/web/pull/7\n"
                "USAGE: {\"tokens_read\": 999999, \"tokens_out\": 999999, \"minutes\": 0}\n")
            got = runner.result(rid)
            self.assertEqual(got["prs"], ["team/core#7", "team/web#7"])
            self.assertEqual(got["usage"]["tokens_read"], 1200)
            self.assertEqual(got["usage"]["minutes"], 4.2)
    def test_two_repos_same_number_each_passes_qa_before_vote(self):
        from sayso import votes
        key = self.case("pat")
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": ["team/core#7", "team/web#7"]}
        self.w.save()
        self.ctx.codehost.for_ref("team/core#7").add_pr(7, "a" * 40)
        self.ctx.codehost.for_ref("team/web#7").add_pr(7, "b" * 40)
        self.tick("dev", "qa")
        run1 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run1, f"CUSTOMER-FIX: YES core fixed\nVERDICT: PASS {'a'*40}")
        self.tick("qa", "qa")
        run2 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run2, f"CUSTOMER-FIX: YES web fixed\nVERDICT: PASS {'b'*40}")
        self.tick("qa")
        self.assertEqual({v["spec"]["pr"] for v in votes.load(self.ctx).values() if v["kind"] == "merge"},
                         {"team/core#7", "team/web#7"})
    def test_unlisted_repo_ref_is_refused_without_network(self):
        from sayso.adapters import reference
        from sayso.config import Section, ConfigError
        from unittest import mock
        import os
        with mock.patch.dict(os.environ, {"T_TOKEN": "tok"}):
            transport = mock.Mock()
            host = reference.GitHubCodeHost(Section("github", {"repo": "team/core",
                                                              "allowed_repos": "team/core,team/web",
                                                              "token_env": "T_TOKEN"}), transport=transport)
            with self.assertRaises(ConfigError):
                host.for_ref("other/private#7").pull_request(7)
            self.assertEqual(transport.call_count, 0)
            web = host.for_ref("team/web#7")
            self.assertEqual(web.repo, "team/web")
    def test_hermes_run_does_not_call_running_or_unreadable_unit_dead(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from sayso.adapters.reference import HermesDevRunner
        from sayso.config import Section
        with tempfile.TemporaryDirectory() as d:
            run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="active\n"))
            runner = HermesDevRunner(Section("hermes", {"profile": "dev"}), Path(d), run=run)
            self.assertEqual(runner.state("dev-case-1"), "running")
            run.return_value = mock.Mock(returncode=0, stdout="inactive\n")
            self.assertEqual(runner.state("dev-case-1"), "dead")
            run.return_value = mock.Mock(returncode=1, stdout="")
            self.assertEqual(runner.state("dev-case-1"), "unknown")
    def test_new_dev_round_clears_old_multi_pr_qa_progress(self):
        key = self.case("pat")
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": [7, 8]}
        for n in (7, 8):
            self.ctx.codehost.add_pr(n, str(n) * 40)
        self.tick("dev", "qa")
        run1 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run1, f"CUSTOMER-FIX: YES fixed\nVERDICT: PASS {'7'*40}")
        self.tick("qa", "qa")
        run2 = cases.load(self.ctx)[key]["qa"]["run"]
        self.ctx.qa_runner.finish(run2, f"FAILING-CHECK: export -> empty\nVERDICT: BLOCKED {'8'*40} empty")
        self.tick("qa", "dev")
        retry = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(retry, pr=9, head="9" * 40)
        self.tick("dev", "qa")
        self.assertEqual(cases.load(self.ctx)[key]["qa_pr_index"], 0)
        self.assertEqual(cases.load(self.ctx)[key]["qa"]["head"], "9" * 40)
    def test_global_qa_pass_cannot_authorize_another_repo_pr(self):
        from sayso import votes
        key = self.case("pat")
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(rid, pr=7, head="a" * 40)
        self.tick("dev")
        rec = cases.load(self.ctx)[key]
        cases.update(self.ctx, key, qa_passed_head="a" * 40, qa_passed_heads={})
        votes.open_vote(self.ctx, key="merge:forged", kind="merge", case_key=key,
                        card_id=rec["card_id"], subject="Pat - Export broken", question="merge this PR?",
                        identity={"case_key": key, "pr": 7},
                        material={"head": "a" * 40, "state": "open"}, spec={"pr": 7, "head": "a" * 40})
        from sayso import stages as st
        cases.move(self.ctx, rec["card_id"], st.AWAITING)
        v = votes.load(self.ctx)["merge:forged"]
        self.w.operator_votes(v["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertEqual(self.ctx.codehost.pull_request(7).state, "open",
                         "a global QA pass is not proof of this PR's head")
    def test_unchecked_pr_votes_reconcile_after_interrupted_open(self):
        from sayso import votes
        key = self.case("pat")
        self.ctx.qa_runner = None
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(rid, pr=7, head="a" * 40)
        self.tick("dev")
        rec = cases.load(self.ctx)[key]
        # A crash after the poll is created but before the stage move leaves
        # a fully formed vote on an In QA card. Repeated dev ticks must finish it.
        cases.set_tags_verified(self.ctx, rec["card_id"], [stages.IN_QA])
        self.tick("dev")
        self.assertEqual(stages.stage_of(self.ctx.board.get_tags(rec["card_id"])), stages.AWAITING)
        self.assertEqual(len([v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]), 1)
    def test_reconcile_does_not_open_new_vote_if_head_moved_after_first_poll(self):
        from sayso import votes
        key = self.case("pat")
        self.ctx.qa_runner = None
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.ctx.dev_runner.finish(rid, pr=7, head="a" * 40)
        self.tick("dev")
        rec = cases.load(self.ctx)[key]
        cases.set_tags_verified(self.ctx, rec["card_id"], [stages.IN_QA])
        self.w.data["prs"]["7"]["head"] = "b" * 40
        self.w.save()
        self.tick("dev")
        self.assertEqual(stages.stage_of(self.ctx.board.get_tags(rec["card_id"])), stages.IN_QA)
        self.assertEqual(len([v for v in votes.load(self.ctx).values() if v["kind"] == "merge"]), 1)
    def test_external_merge_of_other_pr_does_not_finish_case_without_its_vote(self):
        from sayso import votes
        key = self.case("pat")
        self.ctx.qa_runner = None
        self.tick("dev")
        rid = cases.load(self.ctx)[key]["dev_run"]
        self.w.data["runs"][rid]["result"] = {"prs": [7, 8]}
        self.ctx.codehost.add_pr(7, "a" * 40)
        self.ctx.codehost.add_pr(8, "b" * 40)
        self.tick("dev")
        by_pr = {v["spec"]["pr"]: v for v in votes.load(self.ctx).values() if v["kind"] == "merge"}
        self.w.data["prs"]["8"]["state"] = "merged"  # external merge; no owner Yes for this exact PR
        self.w.save()
        self.w.operator_votes(by_pr[7]["poll_id"], "1001", "yes")
        self.tick("votes", "release")
        self.assertNotEqual(stages.stage_of(self.ctx.board.get_tags(cases.load(self.ctx)[key]["card_id"])),
                            stages.IN_SUPPORT)


if __name__ == "__main__":
    unittest.main()
