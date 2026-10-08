"""Stage machine, store, config and vote rules."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sayso import config, stages, store, votes
from sayso.demo import DEMO_TOML


class StageTests(unittest.TestCase):
    def test_one_stage_tag_required(self):
        with self.assertRaises(stages.StageError):
            stages.stage_of(["Done", "In support"])
        with self.assertRaises(stages.StageError):
            stages.stage_of(["High value"])

    def test_waiting_only_with_done(self):
        with self.assertRaises(stages.StageError):
            stages.validate(["In support", "Waiting on customer"])
        stages.validate(["Done", "Waiting on customer"])

    def test_illegal_skip_refused(self):
        with self.assertRaises(stages.StageError):
            stages.move(["New"], "Done")

    def test_labels_survive_every_move(self):
        tags = ["In support", "High value"]
        tags = stages.move(tags, "Awaiting approval")
        tags = stages.move(tags, "Done", waiting=True)
        self.assertEqual(tags, ["Done", "Waiting on customer", "High value"])
        self.assertEqual(stages.reopen(tags), ["In support", "High value"])
        self.assertEqual(stages.close_quiet(["Done", "Waiting on customer", "X"]), ["Done", "X"])

    def test_reopen_leaves_active_work_alone(self):
        self.assertEqual(stages.reopen(["In dev"]), ["In dev"])


class StoreTests(unittest.TestCase):
    def test_corrupt_state_raises_instead_of_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("{not json")
            with self.assertRaises(store.CorruptState):
                store.load_json(p)

    def test_atomic_write_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            store.save_json(p, {"a": 1})
            self.assertEqual(store.load_json(p), {"a": 1})
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")

    def test_single_flight(self):
        with tempfile.TemporaryDirectory() as d:
            with store.single_flight(Path(d) / "j") as first:
                with store.single_flight(Path(d) / "j") as second:
                    self.assertTrue(first)
                    self.assertFalse(second)

    def test_naive_time_refused(self):
        from datetime import datetime
        with self.assertRaises(ValueError):
            store.parse_iso("2030-01-01T00:00:00")
        with self.assertRaises(ValueError):
            store.utc_iso(datetime(2030, 1, 1))


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.toml"
            p.write_text(text)
            return config.load(p)

    def base(self):
        return DEMO_TOML.format(state="/tmp/sayso-config-test")

    def test_demo_config_valid(self):
        cfg = self.load(self.base())
        self.assertEqual(cfg.slug, "demo")
        self.assertFalse(cfg.live)

    def test_unknown_key_refused(self):
        with self.assertRaisesRegex(config.ConfigError, "unknown keys"):
            self.load(self.base() + '\n[jobs]\nintak = 60\n')

    def test_real_adapter_needs_live_mode(self):
        text = self.base().replace('[board]\nadapter = "fake"', '[board]\nadapter = "discord"')
        with self.assertRaisesRegex(config.ConfigError, "needs \\[product\\] mode"):
            self.load(text)

    def test_secret_inline_refused(self):
        text = self.base().replace('[payments]\nadapter = "fake"',
                                   '[payments]\nadapter = "fake"\napi_key_env = "sk_live_abc"')
        with self.assertRaisesRegex(config.ConfigError, "environment variable"):
            self.load(text)

    def test_payments_need_cap(self):
        text = self.base().replace("refund_max_minor = 5000", "refund_max_minor = 0")
        with self.assertRaisesRegex(config.ConfigError, "refund_max_minor"):
            self.load(text)

    def test_label_cannot_shadow_stage(self):
        text = self.base().replace('labels = ["High value"]', 'labels = ["Done"]')
        with self.assertRaisesRegex(config.ConfigError, "reuse stage"):
            self.load(text)

    def test_missing_secret_is_error_not_empty(self):
        sec = config.Section("stripe", {"api_key_env": "HCML_TEST_UNSET_VAR"})
        os.environ.pop("HCML_TEST_UNSET_VAR", None)
        with self.assertRaises(config.ConfigError):
            sec.secret("api_key")


class VoteRuleTests(unittest.TestCase):
    OPS = {"1": "Harry", "2": "Kaviru"}

    def test_stranger_never_decides(self):
        status, by, opinions = votes.decide({"77": "yes"}, self.OPS)
        self.assertEqual((status, by), (votes.OPEN, None))
        self.assertEqual(opinions, {"77": "yes"})

    def test_any_operator_no_declines(self):
        self.assertEqual(votes.decide({"1": "yes", "2": "no"}, self.OPS)[0], votes.DECLINED)

    def test_fingerprint_binds_material(self):
        rec = {"kind": "reply", "identity": {"a": 1}, "fingerprint": votes.fingerprint("reply", {"a": 1}, {"sha": "x"})}
        self.assertTrue(votes.still_bound(rec, {"sha": "x"}))
        self.assertFalse(votes.still_bound(rec, {"sha": "y"}))

    def test_bare_number_subject_refused(self):
        for bad in ("case 1921", "#308", "PM-0021", "ticket 12"):
            with self.assertRaises(votes.VoteError, msg=bad):
                votes.check_human_subject(bad)
        votes.check_human_subject("Pat - export button does nothing")


if __name__ == "__main__":
    unittest.main()
