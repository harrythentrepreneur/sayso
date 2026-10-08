"""Personal data is hidden before real cases reach a shared console. All data here is invented."""
from __future__ import annotations

import unittest

from sayso.privacy import Masker, leaks

SALT = "test-salt-0123456789abcdef"


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.m = Masker(SALT)

    def test_same_person_same_pseudonym_everywhere(self):
        a = self.m.pseudonym("Jo.Bloggs@Example.test")
        self.assertEqual(a, self.m.pseudonym(" jo.bloggs@example.test "))
        self.assertNotEqual(a, self.m.pseudonym("someone@example.test"))
        self.assertRegex(a, r"^Customer [0-9A-F]{4}$")

    def test_pseudonym_depends_on_secret_salt(self):
        self.assertNotEqual(Masker(SALT).pseudonym("a@b.test"), Masker(SALT + "x").pseudonym("a@b.test"))
        with self.assertRaises(ValueError):
            Masker("short")

    def test_emails_payment_ids_cards_phones_ips_hidden(self):
        raw = ("Write to jo.bloggs@example.test. Charge ch_3AbCdEfGh123 for cus_ZyXwVu987, card "
               "4242 4242 4242 4242, call +61 412 345 678, from 203.0.113.7.")
        out = self.m.text(raw)
        for bit in ("jo.bloggs@", "ch_3AbCdEfGh123", "cus_ZyXwVu987", "4242 4242", "412 345", "203.0.113.7"):
            self.assertNotIn(bit, out)
        self.assertEqual(leaks(out), [])
        self.assertIn("ch_•••", out)

    def test_names_linked_to_a_customer_are_hidden(self):
        who = self.m.learn_customer("jo.bloggs@example.test", "From: Joanna Bloggs <jo.bloggs@example.test>",
                                    "Joanna Bloggs - charged twice")
        out = self.m.text("Hi Joanna, thanks. Bloggs family account. Joanna Bloggs wrote again.")
        self.assertNotIn("Joanna", out)
        self.assertNotIn("Bloggs", out)
        self.assertIn(who, out)
        self.assertNotIn(f"{who} {who}", out)

    def test_schools_hidden(self):
        out = self.m.text("She teaches at Liberton Primary and Ashburton College.")
        self.assertNotIn("Liberton", out)
        self.assertNotIn("Ashburton", out)

    def test_common_words_are_not_treated_as_names(self):
        self.m.learn_customer("may.page@example.test")
        self.assertIn("may", self.m.text("You may open the book page."))

    def test_url_query_strings_dropped(self):
        out = self.m.text("Open https://app.example.test/reset?token=abc123&u=9 now")
        self.assertNotIn("abc123", out)
        self.assertIn("https://app.example.test/reset?…", out)

    def test_handles_greetings_labels_and_part_masked_emails(self):
        who = self.m.learn_customer("q@example.test", "sunnyfox12 — book failed: Dragons", "Hi Pat,\n\nThanks")
        self.m.learn_label("Rowan Quill - full USD 199 refund")
        out = self.m.text("sunnyfox12 wrote. Hi Pat, see p***@school.example.test. Rowan Quill paid.")
        for bit in ("sunnyfox12", "Pat,", "p***@", "Rowan", "Quill"):
            self.assertNotIn(bit, out)
        self.assertIn(who, out)
        self.m.learn_label("PR #332 head abc - keep working")
        self.assertIn("PR", self.m.text("PR #332"))

    def test_names_that_are_words_masked_only_when_capitalised(self):
        who = self.m.learn_customer("x@example.test", "From: Robin Brown <x@example.test>")
        out = self.m.text("Robin Brown asked. The brown cover came from the printer.")
        self.assertNotIn("Robin", out)
        self.assertNotIn("Brown", out)
        self.assertIn("brown cover came from", out)
        self.assertIn(who, out)

    def test_strict_mode_hides_unlinked_first_names(self):
        import os
        if not os.path.exists("/usr/share/dict/words"):
            self.skipTest("no system word list")
        m = Masker(SALT, strict=True)
        out = m.text("Fran, Susan and Laura wrote on Monday about the brown cover. Harry replied.")
        for bit in ("Fran", "Susan", "Laura"):
            self.assertNotIn(bit, out)
        for keep in ("Monday", "brown cover"):
            self.assertIn(keep, out)

    def test_strict_mode_keeps_names_you_list(self):
        import os
        if not os.path.exists("/usr/share/dict/words"):
            self.skipTest("no system word list")
        out = Masker(SALT, strict=True, keep=("Harry",)).text("Susan wrote. Harry replied.")
        self.assertIn("Harry", out)
        self.assertNotIn("Susan", out)
        self.assertNotIn("Harry", Masker(SALT, strict=True).text("Susan wrote. Harry replied."))

    def test_deep_masks_nested_values(self):
        out = self.m.deep({"a": ["x@y.test", {"b": "ch_ABCDEF123"}], "n": 3})
        self.assertEqual(out["n"], 3)
        self.assertEqual(leaks(str(out)), [])


if __name__ == "__main__":
    unittest.main()
