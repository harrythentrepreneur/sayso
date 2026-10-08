"""The QUICKSTART, run through the real CLI in a subprocess, exactly as documented.

If this test passes, a new product can be brought up on fakes from the docs
alone. It also checks both example settings files validate (live one with
placeholder secrets only).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sayso(*args, cwd, env=None, ok=True):
    out = subprocess.run([sys.executable, "-m", "sayso.cli", *args], cwd=cwd, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}, timeout=120)
    if ok and out.returncode != 0:
        raise AssertionError(f"sayso {args} exit {out.returncode}\n{out.stdout}\n{out.stderr}")
    return out


class QuickstartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sayso-qs-")
        self.d = Path(self._tmp.name)
        shutil.copy(ROOT / "examples/acme-dry-run.toml", self.d / "acme.toml")
        self.cfg = str(self.d / "acme.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def open_vote(self, kind):
        votes = json.loads((self.d / "state/acme/votes.json").read_text())
        live = [k for k, v in votes.items() if v["kind"] == kind and v["status"] == "open"]
        self.assertEqual(len(live), 1, votes)
        return live[0]

    def test_quickstart_first_email_to_close(self):
        d = self.d
        sayso("validate", "--config", self.cfg, cwd=d)
        sayso("fake", "write-in", "--config", self.cfg, "--from", "pat@customer.test",
             "--subject", "Cannot export", "--body", "Export does nothing.", cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        case = sayso("cases", "--config", self.cfg, cwd=d).stdout.split("\t")[0]
        self.assertIn("Awaiting approval", sayso("cases", "--config", self.cfg, cwd=d).stdout)
        sayso("fake", "vote", "--config", self.cfg, "--key", self.open_vote("reply"), "--user", "1001",
             "--answer", "yes", cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        self.assertIn("Done, Waiting on customer", sayso("cases", "--config", self.cfg, cwd=d).stdout)
        world = json.loads((d / "state/acme/fake-world.json").read_text())
        self.assertEqual(len(world["outbox"]), 1)
        # Quiet days pass: age the whole conversation instead of waiting.
        for row in world["inbox"]:
            row["received_at"] = "2020-01-01T00:00:00+00:00"
        for row in world["outbox"]:
            row["at"] = "2020-01-01T00:05:00+00:00"
        (d / "state/acme/fake-world.json").write_text(json.dumps(world))
        sayso("tick", "--config", self.cfg, cwd=d)
        sayso("fake", "vote", "--config", self.cfg, "--key", self.open_vote("close"), "--user", "1001",
             "--answer", "yes", cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        line = sayso("cases", "--config", self.cfg, cwd=d).stdout.strip()
        self.assertTrue(line.startswith(case) and line.endswith("\tDone"), line)

    def test_pause_blocks_sending(self):
        d = self.d
        sayso("fake", "write-in", "--config", self.cfg, "--from", "pat@customer.test", "--subject", "Hello",
             "--body", "Hi", cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        sayso("fake", "vote", "--config", self.cfg, "--key", self.open_vote("reply"), "--user", "1001",
             "--answer", "yes", cwd=d)
        sayso("pause", "--config", self.cfg, "--reason", "test", cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        self.assertEqual(json.loads((d / "state/acme/fake-world.json").read_text())["outbox"], [])
        self.assertEqual(sayso("run", "sender", "--config", self.cfg, cwd=d).returncode, 0)
        sayso("resume", "--config", self.cfg, cwd=d)
        sayso("tick", "--config", self.cfg, cwd=d)
        self.assertEqual(len(json.loads((d / "state/acme/fake-world.json").read_text())["outbox"]), 1)

    def test_timers_render_writes_units_and_installs_nothing(self):
        out = self.d / "units"
        sayso("timers", "render", "--config", self.cfg, "--out", str(out), cwd=self.d)
        names = sorted(p.name for p in out.iterdir())
        self.assertIn("sayso-acme-sender.service", names)
        self.assertIn("sayso-acme-sender.timer", names)
        self.assertEqual(len(names), 22)  # 11 jobs x (service + timer)

    def test_bad_config_exits_1(self):
        (self.d / "bad.toml").write_text("[product]\nslug='x'\n")
        r = sayso("validate", "--config", str(self.d / "bad.toml"), cwd=self.d, ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("config error", r.stderr)

    def test_live_example_validates_with_placeholder_env(self):
        r = sayso("validate", "--config", str(ROOT / "examples/acme-live.toml"), cwd=self.d)
        self.assertEqual(json.loads(r.stdout)["mode"], "live")

    def test_fake_commands_refuse_live(self):
        r = sayso("fake", "write-in", "--config", str(ROOT / "examples/acme-live.toml"), "--from", "a@b.c",
                 "--subject", "s", "--body", "b", cwd=self.d, ok=False,
                 env={k: "placeholder" for k in re.findall(r'_env = "([A-Z_]+)"',
                                                           (ROOT / "examples/acme-live.toml").read_text())})
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
