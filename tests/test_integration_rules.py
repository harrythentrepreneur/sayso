"""The shared integration rules: one help desk at a time, one payment provider, every adapter named."""
from __future__ import annotations

import unittest
from pathlib import Path

from sayso import config
from sayso.config import ConfigError

BASE = {"product": {"slug": "acme", "mode": "live", "support_address": "support@acme.example"},
        "operators": {"1": "Ann"}, "state": {"dir": "/tmp/sayso-rules-test"}}


def parse(**sections):
    return config.parse(dict(BASE, **sections), Path("/tmp/x.toml"))


class OneHelpdeskTests(unittest.TestCase):
    def test_every_helpdesk_mailbox_needs_its_own_helpdesk(self):
        for desk in config.HELPDESK_MAILBOXES:
            for other in ("fake", "frappe", *[d for d in config.HELPDESK_MAILBOXES if d != desk]):
                with self.subTest(mailbox=desk, helpdesk=other):
                    with self.assertRaises(ConfigError) as ctx:
                        parse(helpdesk={"adapter": other}, mailbox={"adapter": desk})
                    self.assertIn("one help desk at a time", str(ctx.exception))

    def test_matching_pairs_and_plain_mailboxes_pass(self):
        for desk in config.HELPDESK_MAILBOXES:
            with self.subTest(desk=desk):
                cfg = parse(helpdesk={"adapter": desk}, mailbox={"adapter": desk})
                self.assertEqual((cfg.section("helpdesk").adapter, cfg.section("mailbox").adapter), (desk, desk))
        for desk in config.HELPDESKS:
            for mailbox in ("fake", "imap", "gmail"):
                with self.subTest(desk=desk, mailbox=mailbox):
                    parse(helpdesk={"adapter": desk}, mailbox={"adapter": mailbox})

    def test_mailbox_must_read_the_same_account(self):
        hs = {"adapter": "helpscout", "mailbox_id": "13", "client_id_env": "HS_ID", "client_secret_env": "HS_SECRET"}
        cr = {"adapter": "crisp", "website_id": "8c842203-7ed8-4e29-a608-7cf78a7d2fcc", "key_env": "CR_KEY"}
        for desk, over in ((hs, {"mailbox_id": "99"}), (hs, {"client_id_env": "OTHER_ID"}),
                           (cr, {"website_id": "11111111-2222-4333-8444-555555555555"})):
            with self.subTest(desk=desk["adapter"], over=over):
                with self.assertRaises(ConfigError) as ctx:
                    parse(helpdesk=desk, mailbox=dict(desk, **over))
                self.assertIn("same account", str(ctx.exception))
                self.assertIn(next(iter(over)), str(ctx.exception))
        for desk in (hs, cr):
            parse(helpdesk=desk, mailbox=dict(desk))
            parse(helpdesk=desk, mailbox={"adapter": desk["adapter"]})

    def test_a_number_and_the_same_digits_are_the_same_account(self):
        """QA-2 N2: mailbox_id = 13 in one section and "13" in the other is the same inbox."""
        hs = {"adapter": "helpscout", "client_id_env": "HS_ID", "client_secret_env": "HS_SECRET"}
        for a, b in ((13, "13"), ("13", 13), (13, 13), ("13", "13")):
            with self.subTest(helpdesk=a, mailbox=b):
                parse(helpdesk=dict(hs, mailbox_id=a), mailbox=dict(hs, mailbox_id=b))
        for a, b in ((13, "14"), ("13", 14), (13, " 13"), (13, "13.0"), (13, "+13"), (1, True), (13, "١٣")):
            with self.subTest(helpdesk=a, mailbox=b):
                with self.assertRaises(ConfigError) as ctx:
                    parse(helpdesk=dict(hs, mailbox_id=a), mailbox=dict(hs, mailbox_id=b))
                self.assertIn("mailbox_id differ", str(ctx.exception))

    def test_same_value_rule(self):
        same = config._same_value
        self.assertTrue(same(13, "13") and same("13", 13) and same("a", "a") and same(None, None))
        self.assertTrue(same(24, 24.0) and same(True, True), "plain equality is unchanged")
        for a, b in ((13, "14"), ("A", "a"), ("a ", "a"), (True, 1), (1, True), (True, "1"), (None, ""), (0, None),
                     ("013", 13), (13.0, "13")):
            with self.subTest(a=a, b=b):
                self.assertFalse(same(a, b))

    def test_lists_agree_with_adapter_choices(self):
        self.assertEqual(set(config.HELPDESKS), set(config.ADAPTER_CHOICES["helpdesk"]) - {"fake"})
        self.assertTrue(set(config.HELPDESK_MAILBOXES) <= set(config.ADAPTER_CHOICES["mailbox"]))


class HelpScoutDocBlockTests(unittest.TestCase):
    def test_adapters_doc_help_scout_block_loads_and_repeats_the_keys(self):
        """QA-2 N3: the documented [mailbox] block must carry the keys its own client needs."""
        import re
        import tomllib
        doc = (Path(__file__).resolve().parents[1] / "docs" / "ADAPTERS.md").read_text()
        part = doc[doc.index("## Help Scout"):]
        block = re.search(r"```toml\n(.*?)```", part, re.S).group(1)
        raw = tomllib.loads(block)
        for key in ("mailbox_id", "client_id_env", "client_secret_env"):
            self.assertEqual(raw["mailbox"].get(key), raw["helpdesk"][key], key)
        parse(**raw)


class OnePaymentProviderTests(unittest.TestCase):
    def test_payments_twice_is_a_config_error_not_a_crash(self):
        for name in config.ADAPTER_CHOICES:
            with self.subTest(section=name):
                with self.assertRaises(ConfigError) as ctx:
                    parse(**{name: [{"adapter": "fake"}, {"adapter": "none"}]})
                self.assertIn(f"[[{name}]]", str(ctx.exception))

    def test_a_single_value_is_named_as_a_value_not_a_list(self):
        """QA-2 N4: payments = "fake" (a string or number) is refused with words that fit a single value."""
        for value in ("fake", 3, True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as ctx:
                    parse(payments=value)
                msg = str(ctx.exception)
                self.assertIn("not a single value", msg)
                self.assertNotIn("not a list", msg)
                self.assertIn("[payments]", msg)

    def test_providers_are_the_payments_choices(self):
        self.assertEqual(set(config.PAYMENT_PROVIDERS), set(config.ADAPTER_CHOICES["payments"]) - {"none", "fake"})

    def test_a_second_provider_section_is_refused(self):
        for name in ("stripe", "paddle", "lemonsqueezy"):
            with self.subTest(section=name):
                with self.assertRaises(ConfigError):
                    parse(payments={"adapter": "stripe"}, **{name: {"adapter": name}})


class EveryAdapterNamedTests(unittest.TestCase):
    def test_every_adapter_section_is_allowed_and_validated(self):
        for name, choices in config.ADAPTER_CHOICES.items():
            with self.subTest(section=name):
                parse(**{name: {"adapter": choices[0]}})
                with self.assertRaises(ConfigError) as ctx:
                    parse(**{name: {"adapter": "nope"}})
                for choice in choices:
                    self.assertIn(choice, str(ctx.exception))

    def test_real_adapter_needs_live_mode(self):
        for name, choices in config.ADAPTER_CHOICES.items():
            for adapter in [c for c in choices if c not in ("fake", "none")]:
                with self.subTest(section=name, adapter=adapter):
                    raw = dict(BASE, product=dict(BASE["product"], mode="dry-run"), **{name: {"adapter": adapter}})
                    with self.assertRaises(ConfigError):
                        config.parse(raw, Path("/tmp/x.toml"))


if __name__ == "__main__":
    unittest.main()
