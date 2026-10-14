"""The stable API must not drift by accident.

sayso/stable_api.json lists every sayso name that Sayso Cloud (the hosted
product) imports. A change that removes one, renames a parameter, adds a
required one, reorders positional ones, or changes a constant value breaks
Cloud. This test fails on any of those, here, before the change merges.

Changing a listed name on purpose is fine: re-record with
`python scripts/stable_api.py --write` and say so in the PR.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("stable_api", ROOT / "scripts" / "stable_api.py")
stable_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stable_api)


def _f(*params):
    return {"kind": "function", "signature": [list(p) for p in params]}


class StableApiTests(unittest.TestCase):
    def test_no_listed_name_drifted(self):
        problems = stable_api.drift(stable_api.load())
        self.assertEqual(problems, [], "\n".join(problems) +
                         "\nIf this is deliberate: python scripts/stable_api.py --write, and say so in the PR.")

    def test_the_list_covers_what_cloud_builds_on(self):
        names = stable_api.load()["names"]
        for core in ("sayso.jobs.ORDER", "sayso.runtime.Context", "sayso.store.locked",
                     "sayso.votes.open_vote", "sayso.console.record_web_vote", "sayso.brand.swipe.deck"):
            self.assertIn(core, names)
        self.assertGreaterEqual(len(names), 90)


class CompatibilityRules(unittest.TestCase):
    """The checker itself: a break must read as a break, an addition must not."""
    base = _f(("ctx", "POSITIONAL_OR_KEYWORD", False), ("timeout", "KEYWORD_ONLY", True))

    def ok(self, now):
        return stable_api.compatible(self.base, now)

    def test_unchanged_is_compatible(self):
        self.assertTrue(self.ok(self.base))

    def test_new_optional_param_is_compatible(self):
        self.assertTrue(self.ok(_f(("ctx", "POSITIONAL_OR_KEYWORD", False), ("timeout", "KEYWORD_ONLY", True),
                                   ("extra", "KEYWORD_ONLY", True))))

    def test_new_required_param_breaks(self):
        self.assertFalse(self.ok(_f(("ctx", "POSITIONAL_OR_KEYWORD", False), ("timeout", "KEYWORD_ONLY", True),
                                    ("must", "KEYWORD_ONLY", False))))

    def test_removed_param_breaks(self):
        self.assertFalse(self.ok(_f(("ctx", "POSITIONAL_OR_KEYWORD", False))))

    def test_renamed_param_breaks(self):
        self.assertFalse(self.ok(_f(("context", "POSITIONAL_OR_KEYWORD", False), ("timeout", "KEYWORD_ONLY", True))))

    def test_default_dropped_breaks(self):
        self.assertFalse(self.ok(_f(("ctx", "POSITIONAL_OR_KEYWORD", False), ("timeout", "KEYWORD_ONLY", False))))

    def test_positional_reorder_breaks(self):
        was = _f(("a", "POSITIONAL_OR_KEYWORD", False), ("b", "POSITIONAL_OR_KEYWORD", False))
        now = _f(("b", "POSITIONAL_OR_KEYWORD", False), ("a", "POSITIONAL_OR_KEYWORD", False))
        self.assertFalse(stable_api.compatible(was, now))

    def test_constant_value_change_breaks(self):
        was = {"kind": "value", "type": "str", "value": "open"}
        self.assertFalse(stable_api.compatible(was, {"kind": "value", "type": "str", "value": "opened"}))

    def test_dict_key_removed_breaks_added_is_fine(self):
        was = {"kind": "value", "type": "dict", "keys": ["draft", "intake"]}
        self.assertTrue(stable_api.compatible(was, {"kind": "value", "type": "dict", "keys": ["draft", "intake", "new"]}))
        self.assertFalse(stable_api.compatible(was, {"kind": "value", "type": "dict", "keys": ["draft"]}))

    def test_class_method_removed_breaks_added_is_fine(self):
        sig = [["self", "POSITIONAL_OR_KEYWORD", False]]
        was = {"kind": "class", "init": sig, "members": {"run": sig}}
        self.assertTrue(stable_api.compatible(was, {"kind": "class", "init": sig, "members": {"run": sig, "x": sig}}))
        self.assertFalse(stable_api.compatible(was, {"kind": "class", "init": sig, "members": {}}))

    def test_exception_losing_a_base_breaks(self):
        was = {"kind": "exception", "bases": ["builtins.RuntimeError", "builtins.Exception"]}
        self.assertFalse(stable_api.compatible(was, {"kind": "exception", "bases": ["builtins.Exception"]}))

    def test_identifier_constants_keep_value_design_tokens_do_not(self):
        self.assertEqual(stable_api.shape("open"), {"kind": "value", "type": "str", "value": "open"})
        self.assertEqual(stable_api.shape("Awaiting approval"), {"kind": "value", "type": "str", "value": "Awaiting approval"})
        self.assertEqual(stable_api.shape('"Inter",system-ui,sans-serif'), {"kind": "value", "type": "str"})

    def test_real_removal_is_reported(self):
        recorded = {"names": {"sayso.store.no_such_name": _f()}}
        self.assertIn("REMOVED", stable_api.drift(recorded)[0])

    def test_real_shape_change_is_reported(self):
        recorded = {"names": {"sayso.votes.OPEN": {"kind": "value", "type": "str", "value": "opened"}}}
        problems = stable_api.drift(recorded)
        self.assertEqual(len(problems), 1)
        self.assertIn("sayso.votes.OPEN: shape changed", problems[0])

    def test_varargs_rename_is_fine_dropping_it_breaks(self):
        was = _f(("paths", "VAR_POSITIONAL", False), ("timeout", "KEYWORD_ONLY", True))
        self.assertTrue(stable_api.compatible(was, _f(("p", "VAR_POSITIONAL", False), ("timeout", "KEYWORD_ONLY", True))))
        self.assertFalse(stable_api.compatible(was, _f(("timeout", "KEYWORD_ONLY", True))))


if __name__ == "__main__":
    unittest.main()
