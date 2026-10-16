"""Every integration test module keeps its no-real-network guard.

Each adapter's tests install a socket guard in setUpModule that refuses any connection that is not to this
machine. This test imports each module, installs its guard, and tries one outside connection (a TEST-NET-3
address that is never routed, so a missing guard fails here without reaching any real host)."""
from __future__ import annotations

import importlib
import socket
import unittest

GUARDED = ("test_oauth", "test_gmail", "test_helpscout", "test_zendesk", "test_intercom", "test_crisp",
           "test_paddle", "test_lemonsqueezy", "test_slack_adapter", "test_events", "test_customer_facts")
OUTSIDE = ("203.0.113.7", 443)     # RFC 5737 documentation range: never a real host


class NetworkGuardTests(unittest.TestCase):
    def test_every_integration_module_refuses_an_outside_connection(self):
        for name in GUARDED:
            with self.subTest(module=name):
                mod = importlib.import_module(f"tests.{name}")
                self.assertTrue(hasattr(mod, "setUpModule") and hasattr(mod, "tearDownModule"),
                                f"{name} has no socket guard")
                real = socket.socket.connect
                mod.setUpModule()
                try:
                    self.assertIsNot(socket.socket.connect, real, f"{name}: guard did not patch connect")
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    try:
                        with self.assertRaises(Exception) as ctx:
                            s.connect(OUTSIDE)
                        self.assertNotIsInstance(ctx.exception, (TimeoutError, socket.timeout),
                                                 f"{name}: the connection was attempted, not refused")
                    finally:
                        s.close()
                finally:
                    mod.tearDownModule()
                self.assertIs(socket.socket.connect, real, f"{name}: guard not removed after the module")


if __name__ == "__main__":
    unittest.main()
