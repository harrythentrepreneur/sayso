"""Reference adapters against a RECORDING transport. No network, no accounts."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from sayso.adapters import reference
from sayso.adapters.http import Client, HttpError
from sayso.config import ConfigError, Section


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body.decode() if body else None})
        status, payload = self.responses.pop(0)
        return status, {}, json.dumps(payload).encode()


ENV = {"T_TOKEN": "tok", "T_KEY": "key", "T_SECRET": "sec", "T_PW": "pw"}


class HttpTests(unittest.TestCase):
    def test_429_retry_after_honoured(self):
        rec = Recorder([(429, {"retry_after": 2}), (200, {"ok": 1})])
        waits = []
        self.assertEqual(Client("https://x", {}, rec, sleep=waits.append).request("GET", "/a"), {"ok": 1})
        self.assertEqual(waits, [2.0])

    def test_error_raises(self):
        with self.assertRaises(HttpError):
            Client("https://x", {}, Recorder([(500, {"e": 1})])).request("GET", "/a")

    def test_user_agent_set(self):
        rec = Recorder([(200, {})])
        Client("https://x", {}, rec).request("GET", "/a")
        self.assertIn("User-Agent", rec.calls[0]["headers"])


@mock.patch.dict(os.environ, ENV)
class DiscordTests(unittest.TestCase):
    TAGS = {"available_tags": [{"id": "1", "name": "New"}, {"id": "2", "name": "In support"}]}

    def board(self, responses):
        rec = Recorder(responses)
        b = reference.DiscordForumBoard(Section("discord", {"forum_id": "F", "alerts_channel_id": "A",
                                                            "token_env": "T_TOKEN"}), transport=rec)
        return b, rec

    def test_needs_ids(self):
        with self.assertRaises(ConfigError):
            reference.DiscordForumBoard(Section("discord", {"token_env": "T_TOKEN"}))

    def test_set_tags_sends_whole_list_by_id(self):
        b, rec = self.board([(200, self.TAGS), (200, {})])
        b.set_tags("P", ["In support"])
        self.assertEqual(json.loads(rec.calls[1]["body"]), {"applied_tags": ["2"]})

    def test_unknown_tag_refused_not_created(self):
        b, rec = self.board([(200, self.TAGS)])
        with self.assertRaises(ConfigError):
            b.set_tags("P", ["Done"])
        self.assertTrue(all(c["method"] == "GET" for c in rec.calls), "must never edit available_tags")

    def test_chunks_on_words_and_strips(self):
        parts = reference.DiscordForumBoard.chunks("word " * 1000)
        self.assertTrue(all(len(p) <= 1900 and p == p.strip() for p in parts))
        self.assertEqual(" ".join(parts).split(), ["word"] * 1000)

    def test_bot_votes_ignored(self):
        b, _ = self.board([(200, {"users": [{"id": "9", "bot": True}, {"id": "1"}]}), (200, {"users": []})])
        self.assertEqual(b.read_poll("P", "M").votes, {"1": "yes"})

    def test_last_activity_reads_last_message_not_post_creation(self):
        post_id, last_id = "1477455328051200000", "1558358419046400000"   # post made in March, last message in October
        b, rec = self.board([(200, {"id": post_id, "last_message_id": last_id})])
        self.assertEqual(b.last_activity(post_id), "2026-10-10T06:00:00+00:00")
        self.assertEqual(rec.calls[0]["url"].split("?")[0][-len(post_id):], post_id)

    def test_last_activity_without_a_message_is_unreadable(self):
        b, _ = self.board([(200, {"id": "1477455328051200000", "last_message_id": None})])
        with self.assertRaises(RuntimeError):
            b.last_activity("1477455328051200000")


@mock.patch.dict(os.environ, ENV)
class StripeTests(unittest.TestCase):
    def test_refund_uses_idempotency_key(self):
        rec = Recorder([(200, {"id": "re_1", "currency": "usd"})])
        s = reference.StripePayments(Section("stripe", {"api_key_env": "T_KEY"}), transport=rec)
        s.refund("ch_1", 500, "usd", idempotency_key="money:x")
        self.assertEqual(rec.calls[0]["headers"]["Idempotency-Key"], "money:x")
        self.assertIn("amount=500", rec.calls[0]["body"])

    def test_currency_mismatch_raises(self):
        rec = Recorder([(200, {"id": "re_1", "currency": "eur"})])
        s = reference.StripePayments(Section("stripe", {"api_key_env": "T_KEY"}), transport=rec)
        with self.assertRaises(RuntimeError):
            s.refund("ch_1", 500, "usd", idempotency_key="k")


@mock.patch.dict(os.environ, ENV)
class GitHubTests(unittest.TestCase):
    def test_merge_pins_sha(self):
        rec = Recorder([(200, {"merged": True})])
        g = reference.GitHubCodeHost(Section("github", {"repo": "o/r", "token_env": "T_TOKEN"}), transport=rec)
        g.merge(3, "abc", idempotency_key="k")
        self.assertEqual(json.loads(rec.calls[0]["body"])["sha"], "abc")

    def _gh(self, responses):
        rec = Recorder(responses)
        return rec, reference.GitHubCodeHost(Section("github", {"repo": "o/r", "token_env": "T_TOKEN"}), transport=rec)

    def test_pr_facts_green_with_files_and_added_lines(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "@@\n+x = 1\n-y\n"},
                               {"filename": "tests/test_a.py", "patch": "+def test(): pass"}]),
                         (200, {"check_runs": [{"status": "completed", "conclusion": "success"}]}),
                         (200, {"state": "success", "statuses": []})])
        f = g.pr_facts(3, "abc")
        self.assertEqual((f.ci, f.files, f.added), ("green", ("a.py", "tests/test_a.py"),
                                                    ("x = 1", "def test(): pass")))

    def test_pr_facts_counts_changed_lines(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x", "additions": 3, "deletions": 2},
                               {"filename": "b.py", "patch": "+y", "additions": 1, "deletions": 0}]),
                         (200, {"check_runs": [{"status": "completed", "conclusion": "success"}]}),
                         (200, {"state": "success", "statuses": []})])
        self.assertEqual(g.pr_facts(3, "abc").changed_lines, 6)

    def test_pr_facts_missing_counts_is_unreadable_size(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x", "additions": 3, "deletions": 2},
                               {"filename": "b.py", "patch": "+y"}]),
                         (200, {"check_runs": [{"status": "completed", "conclusion": "success"}]}),
                         (200, {"state": "success", "statuses": []})])
        self.assertIsNone(g.pr_facts(3, "abc").changed_lines)

    def test_pr_facts_unreadable_ci_is_red_never_green(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]), (500, {"message": "boom"})])
        self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_pr_facts_no_ci_yet_is_pending(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]), (200, {"check_runs": []}),
                         (200, {"state": "pending", "statuses": []})])
        self.assertEqual(g.pr_facts(3, "abc").ci, "pending")

    def test_pr_facts_failed_check_is_red(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]),
                         (200, {"check_runs": [{"status": "completed", "conclusion": "failure"}]}),
                         (200, {"state": "success", "statuses": []})])
        self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    BILL = [{"annotation_level": "failure", "path": ".github",
             "message": "The job was not started because recent account payments have failed or your spending "
                        "limit needs to be increased."}]

    def _billing(self, notes=None, job=None, conclusion="failure", status=None):
        return self._gh([(200, [{"filename": "a.py", "patch": "+x"}]),
                         (200, {"check_runs": [{"id": 11, "status": "completed", "conclusion": conclusion}]}),
                         (200, status or {"state": "success", "statuses": []}),
                         (200, self.BILL if notes is None else notes),
                         (200, job if job is not None else {"steps": [], "runner_id": None, "runner_name": None})])

    def test_ci_github_refused_for_billing_is_not_run(self):
        rec, g = self._billing()
        self.assertEqual(g.pr_facts(3, "abc").ci, "not-run")
        self.assertTrue(rec.calls[3]["url"].endswith("/check-runs/11/annotations"))
        self.assertTrue(rec.calls[4]["url"].endswith("/actions/jobs/11"))

    def test_billing_text_from_a_job_that_ran_is_red(self):
        _, g = self._billing(job={"steps": [{"name": "x"}], "runner_id": 4, "runner_name": "r"})
        self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_billing_text_with_any_sign_the_job_ran_is_red(self):
        for job in ({"steps": [{"name": "x"}], "runner_id": None, "runner_name": None},
                    {"steps": [], "runner_id": 4, "runner_name": None},
                    {"steps": [], "runner_id": None, "runner_name": "runner-1"},
                    {"runner_id": None, "runner_name": None}, []):
            with self.subTest(job=job):
                _, g = self._billing(job=job)
                self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_billing_words_not_from_github_are_red(self):
        for notes in ([], [{**self.BILL[0], "path": "tests/x.py"}], [{**self.BILL[0], "annotation_level": "notice"}],
                      [{**self.BILL[0], "message": "Printed: " + self.BILL[0]["message"]}]):
            with self.subTest(notes=notes):
                _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]),
                                 (200, {"check_runs": [{"id": 11, "status": "completed", "conclusion": "failure"}]}),
                                 (200, {"state": "success", "statuses": []}), (200, notes)])
                self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_unreadable_billing_record_is_red(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]),
                         (200, {"check_runs": [{"id": 11, "status": "completed", "conclusion": "failure"}]}),
                         (200, {"state": "success", "statuses": []}), (500, {"message": "boom"})])
        self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_a_failing_status_context_beside_billing_is_red(self):
        _, g = self._gh([(200, [{"filename": "a.py", "patch": "+x"}]),
                         (200, {"check_runs": [{"id": 11, "status": "completed", "conclusion": "failure"}]}),
                         (200, {"state": "failure", "statuses": [{"state": "failure"}]})])
        self.assertEqual(g.pr_facts(3, "abc").ci, "red")

    def test_pr_facts_hidden_patch_is_unreadable_diff(self):
        _, g = self._gh([(200, [{"filename": "big.json", "status": "modified"}]),
                         (200, {"check_runs": [{"status": "completed", "conclusion": "success"}]}),
                         (200, {"state": "success", "statuses": []})])
        self.assertIsNone(g.pr_facts(3, "abc").added)

    def test_merged_state(self):
        rec = Recorder([(200, {"merged": True, "state": "closed", "head": {"sha": "abc"}, "html_url": "u"})])
        g = reference.GitHubCodeHost(Section("github", {"repo": "o/r", "token_env": "T_TOKEN"}), transport=rec)
        self.assertEqual(g.pull_request(3).state, "merged")


@mock.patch.dict(os.environ, ENV)
class FrappeTests(unittest.TestCase):
    def test_readback_needs_one_sent_queue_row(self):
        import hashlib
        body = "hello"
        sha = hashlib.sha256(body.encode()).hexdigest()
        comm = {"name": "C1", "recipients": "a@b.test", "sender": "support@example.com", "subject": "s",
                "content": body, "message_id": "<m>"}
        rec = Recorder([(200, {"data": [comm]}), (200, {"data": [{"name": "Q", "status": "Sent"},
                                                                  {"name": "Q2", "status": "Sent"}]})])
        f = reference.FrappeHelpdesk(Section("frappe", {"url": "https://h", "api_key_env": "T_KEY",
                                                        "api_secret_env": "T_SECRET"}), "support@example.com",
                                     transport=rec)
        rows = f.sent_readback("7", sha)
        self.assertEqual(rows[0].status, "Not Sent", "two queue rows is not proof of one send")


@mock.patch.dict(os.environ, ENV)
class ImapTests(unittest.TestCase):
    def test_counts_sent_copies_read_only(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b"4 9"])
        box = reference.ImapMailbox(Section("imap", {"host": "h", "user": "u", "password_env": "T_PW"}),
                                    connect=lambda: conn)
        self.assertEqual(box.sent_copies("abc@x"), 2)
        self.assertTrue(conn.select.call_args.kwargs["readonly"])
        self.assertIn("<abc@x>", conn.search.call_args.args)

    def _box(self, conn):
        return reference.ImapMailbox(Section("imap", {"host": "h", "user": "u", "password_env": "T_PW"}),
                                     connect=lambda: conn)

    def test_dropped_connection_retried(self):
        conn = mock.Mock()
        conn.search.side_effect = [OSError("peer closed connection"), OSError("peer closed connection"),
                                   ("OK", [b"7"])]
        self.assertEqual(self._box(conn).sent_copies("abc@x"), 1)
        self.assertEqual(conn.search.call_count, 3)

    def test_three_failures_raise(self):
        conn = mock.Mock()
        conn.search.side_effect = OSError("down")
        with self.assertRaises(RuntimeError):
            self._box(conn).sent_copies("abc@x")
        self.assertEqual(conn.search.call_count, 3, "bounded: three reads, no more")


class HermesQaRunnerTests(unittest.TestCase):
    def runner(self, run=None, **opts):
        import tempfile
        from pathlib import Path
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        return reference.HermesQaRunner(Section("hermes", {"profile": "acme-qa", **opts}), Path(self._d.name),
                                        run=run or mock.Mock(), dev_profile="acme-dev")

    def test_same_profile_as_dev_refused(self):
        with self.assertRaises(ConfigError):
            reference.HermesQaRunner(Section("hermes", {"profile": "acme-dev"}), None, dev_profile="acme-dev")

    def test_no_red_proof_command_is_an_error_not_a_pass(self):
        self.assertEqual(self.runner().fails_on_old_code(3, "abc")["state"], "error")

    def test_red_proof_command_gets_pr_and_head(self):
        run = mock.Mock(return_value=mock.Mock(stdout='noise\n{"state": "red-proved"}\n'))
        got = self.runner(run, red_proof_cmd="./red.sh --repo o/r").fails_on_old_code(3, "abc")
        self.assertEqual(got["state"], "red-proved")
        self.assertEqual(run.call_args.args[0], ["./red.sh", "--repo", "o/r", "3", "abc"])

    def test_red_proof_garbage_is_error(self):
        run = mock.Mock(return_value=mock.Mock(stdout="all good!"))
        self.assertEqual(self.runner(run, red_proof_cmd="x").fails_on_old_code(3, "abc")["state"], "error")

    def test_result_only_when_the_run_finished(self):
        r = self.runner()
        rid = r.start("k", "brief DO NOT MERGE", idempotency_key="qa:k:abc:1")
        self.assertIsNone(r.result(rid))
        (r.dir / f"{rid}.out").write_text("VERDICT: PASS abc")
        self.assertEqual(r.result(rid), {"text": "VERDICT: PASS abc"})
        r.start("k", "brief", idempotency_key="qa:k:abc:1")
        self.assertEqual(r.run.call_count, 1, "same key never starts a second unit")


if __name__ == "__main__":
    unittest.main()
