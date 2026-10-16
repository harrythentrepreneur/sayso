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

    def test_lists_agree_with_adapter_choices(self):
        self.assertEqual(set(config.HELPDESKS), set(config.ADAPTER_CHOICES["helpdesk"]) - {"fake"})
        self.assertTrue(set(config.HELPDESK_MAILBOXES) <= set(config.ADAPTER_CHOICES["mailbox"]))


class OnePaymentProviderTests(unittest.TestCase):
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
