"""The docs must not lie: links resolve, every `sayso` command exists, every
settings key named in SETTINGS.md is a real key, and every guard file in
SAFETY.md exists."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from sayso import cli, config

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]


def subcommands() -> set[str]:
    """Read the command list from the real parser's help, not from source text."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            cli.main(["--help"])
        except SystemExit:
            pass
    choices = re.search(r"\{([a-z,-]+)\}", buf.getvalue())
    assert choices, buf.getvalue()
    return set(choices.group(1).split(","))


class DocsTests(unittest.TestCase):
    def test_required_docs_exist(self):
        for name in ("QUICKSTART", "ONBOARDING", "SETTINGS", "ARCHITECTURE", "STAGES", "SAFETY", "ADAPTERS",
                     "PLUGINS", "DASHBOARD", "INSTALL", "RUNBOOK", "SECURITY", "FAQ"):
            self.assertTrue((ROOT / "docs" / f"{name}.md").is_file(), name)
        for name in ("README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_relative_links_resolve(self):
        for doc in DOCS:
            for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", doc.read_text()):
                self.assertTrue((doc.parent / target).exists(), f"{doc.name} -> {target}")

    def test_every_sayso_command_exists(self):
        known = subcommands()
        for doc in DOCS:
            for cmd in re.findall(r"\bsayso ([a-z][a-z-]+)", doc.read_text()):
                self.assertIn(cmd, known, f"{doc.name}: sayso {cmd}")

    def test_settings_keys_are_real(self):
        text = (ROOT / "docs/SETTINGS.md").read_text()
        policy_keys = set(re.findall(r"^\| `([a-z_]+)` \| [0-9\[`]", text, re.M))
        self.assertEqual(policy_keys, set(config.Policy.__dataclass_fields__))
        for job in config.DEFAULT_JOBS:
            self.assertIn(f"{job} {config.DEFAULT_JOBS[job]}", text)
        for section, choices in config.ADAPTER_CHOICES.items():
            row = next(ln for ln in text.splitlines() if ln.startswith(f"| {section} |"))
            for choice in choices:
                self.assertIn(f"`{choice}`", row, section)

    def test_safety_table_files_exist(self):
        text = (ROOT / "docs/SAFETY.md").read_text()
        for path in set(re.findall(r"\| ((?:jobs/)?[a-z_]+\.py)", text)):
            self.assertTrue((ROOT / "sayso" / path).is_file(), path)

    def test_stage_names_in_docs_match_code(self):
        from sayso import stages
        text = (ROOT / "docs/STAGES.md").read_text()
        for tag in stages.SYSTEM_TAGS:
            self.assertIn(f"`{tag}`", text)
        for frm, tos in stages.ALLOWED.items():
            row = next(ln for ln in text.splitlines() if ln.startswith(f"| {frm} |"))
            self.assertEqual(set(x.strip() for x in row.split("|")[2].split(",")), set(tos), frm)


if __name__ == "__main__":
    unittest.main()
