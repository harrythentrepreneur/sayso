"""DNS-rebinding guard: the dashboard and the console answer only to the Host they were started on.

Every test talks to a REAL server over a socket and sets the Host header by hand, the way a
rebinding attacker's page would arrive (the browser sends the attacker's domain as Host).
"""
from __future__ import annotations

import http.client
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from sayso import console, dashboard, hostcheck

SECRET = "s" * 40
PASSWORD = "correct horse battery"


def raw(port: int, lines: list[str], body: bytes = b"") -> tuple[int, str]:
    """Send a hand-written request (any Host headers, or none) and return (status, body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), rest.decode("utf-8", "replace")


class _Served:
    def start(self, srv):
        self.srv = srv
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

    def get(self, host: str | None, path: str = "/", headers: dict | None = None, method: str = "GET",
            body: str | None = None) -> tuple[int, str]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", host)
        data = body.encode() if body is not None else None
        if data is not None:
            c.putheader("Content-Type", "application/x-www-form-urlencoded")
            c.putheader("Content-Length", str(len(data)))
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        out = (r.status, r.read().decode("utf-8", "replace"))
        c.close()
        return out


FORGED = ("attacker.example", "attacker.example:{p}", "evil.test:{p}", "127.0.0.1.nip.io:{p}",
          "localhost.:{p}", "LOCALHOST.:{p}", "127.0.0.1:{q}", "localhost", "127.0.0.1", "[::2]:{p}",
          "::1", "127.0.0.1:{p}@evil.test", "127.0.0.1:{p}/x", "", "[::1", "0x7f000001:{p}")
GOOD = ("127.0.0.1:{p}", "localhost:{p}", "LocalHost:{p}", "[::1]:{p}", "[0:0:0:0:0:0:0:1]:{p}")


class DashboardHostTests(_Served, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = dashboard.build_demo_state(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.start(dashboard.make_server([self.config], "127.0.0.1", 0))

    def test_forged_host_is_refused_before_any_page(self):
        for h in FORGED:
            host = h.format(p=self.port, q=self.port + 1)
            for path in ("/", "/p/demo/", "/p/demo/audit?format=csv"):
                status, body = self.get(host, path)
                self.assertEqual(status, 421, f"{host!r} {path}")
                self.assertIn("Misdirected request", body)
                self.assertNotIn("<html", body.lower())
                self.assertNotIn("sam@school.test", body)

    def test_post_and_head_with_forged_host_are_refused_too(self):
        self.assertEqual(self.get("attacker.example", "/p/demo/", method="POST", body="a=1")[0], 421)
        self.assertEqual(self.get("attacker.example", "/p/demo/", method="HEAD")[0], 421)
        # the right Host still gets the dashboard's own refusal of writes
        self.assertEqual(self.get(f"127.0.0.1:{self.port}", "/p/demo/", method="POST", body="a=1")[0], 405)

    def test_missing_or_doubled_host_is_refused(self):
        p = self.port
        self.assertEqual(raw(p, ["GET /p/demo/ HTTP/1.1", "Connection: close"])[0], 421)
        self.assertEqual(raw(p, ["GET /p/demo/ HTTP/1.1", f"Host: 127.0.0.1:{p}", "Host: attacker.example",
                                 "Connection: close"])[0], 421)
        self.assertEqual(raw(p, ["GET /p/demo/ HTTP/1.0"])[0], 421)

    def test_loopback_names_with_the_bound_port_work(self):
        for h in GOOD:
            status, body = self.get(h.format(p=self.port), "/p/demo/")
            self.assertEqual(status, 200, h)
            self.assertIn("<html", body.lower())

    def test_allow_list_adds_names_for_a_reverse_proxy(self):
        srv = dashboard.make_server([self.config], "127.0.0.1", 0,
                                    allowed_hosts=["dash.example.com", "Tunnel.Example:8443"])
        other = _Probe(srv)
        self.addCleanup(other.close)
        self.assertEqual(other.get("dash.example.com", "/p/demo/")[0], 200)
        self.assertEqual(other.get("DASH.example.com:443", "/p/demo/")[0], 200)
        self.assertEqual(other.get("tunnel.example:8443", "/p/demo/")[0], 200)
        self.assertEqual(other.get("tunnel.example:9443", "/p/demo/")[0], 421)
        self.assertEqual(other.get("tunnel.example", "/p/demo/")[0], 421)
        self.assertEqual(other.get("attacker.example", "/p/demo/")[0], 421)
        self.assertEqual(other.get(f"127.0.0.1:{other.port}", "/p/demo/")[0], 200)

    def test_bad_allow_list_entry_is_an_error_at_start(self):
        for bad in ("a b", "x/y", "[::1", "host:port", "h:1:2"):
            with self.assertRaises(ValueError, msg=bad):
                dashboard.make_server([self.config], "127.0.0.1", 0, allowed_hosts=[bad])


class _Probe(_Served):
    def __init__(self, srv):
        self.srv, self.port = srv, srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ConsoleHostTests(_Served, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dashboard.build_demo_state(Path(self.tmp.name))
        self.op = next(iter(self.config.operators))
        console.add_user(self.config, "Op One", self.op, PASSWORD)
        self.auth = console.Auth(self.config, SECRET)
        self.start(console.make_server(self.config, self.auth, "127.0.0.1", 0))

    def sign_in(self, host: str) -> tuple[int, str]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urlencode({"name": "Op One", "password": PASSWORD})
        c.request("POST", "/login", body=body, headers={"Host": host,
                                                         "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        cookie = (r.getheader("Set-Cookie") or "").split(";")[0].partition("=")[2]
        c.close()
        return r.status, cookie

    def test_forged_host_cannot_sign_in_read_or_vote(self):
        good = f"127.0.0.1:{self.port}"
        status, cookie = self.sign_in(good)
        self.assertEqual(status, 303)
        self.assertTrue(cookie)
        # login form and sign-in POST refused under a rebound name
        self.assertEqual(self.get("attacker.example", "/login")[0], 421)
        self.assertEqual(self.sign_in(f"attacker.example:{self.port}"), (421, ""))
        # even with a valid session cookie, pages and votes are refused
        ck = {"Cookie": f"sayso_session={cookie}"}
        status, body = self.get(f"attacker.example:{self.port}", "/p/demo/", headers=ck)
        self.assertEqual(status, 421)
        self.assertNotIn("sam@school.test", body)
        votes_before = (self.config.state_dir / "web-votes.json").read_bytes() if \
            (self.config.state_dir / "web-votes.json").exists() else b""
        status, _ = self.get(f"attacker.example:{self.port}", "/p/demo/vote", headers=ck, method="POST",
                             body=urlencode({"key": "x", "answer": "yes", "fp": "y", "csrf": "z"}))
        self.assertEqual(status, 421)
        status, _ = self.get(f"attacker.example:{self.port}", "/p/demo/pause", headers=ck, method="POST",
                             body=urlencode({"csrf": self.auth.csrf(self.auth.user_for(cookie))}))
        self.assertEqual(status, 421)
        self.assertFalse((self.config.state_dir / "PAUSED").exists())
        after = (self.config.state_dir / "web-votes.json").read_bytes() if \
            (self.config.state_dir / "web-votes.json").exists() else b""
        self.assertEqual(votes_before, after)
        # the same cookie under the right Host still works (the guard is the only difference)
        self.assertEqual(self.get(good, "/p/demo/", headers=ck)[0], 200)
        status, _ = self.get(good, "/p/demo/pause", headers=ck, method="POST",
                             body=urlencode({"csrf": self.auth.csrf(self.auth.user_for(cookie))}))
        self.assertEqual(status, 303)
        self.assertTrue((self.config.state_dir / "PAUSED").exists())

    def test_every_forged_form_is_refused_on_get_and_post(self):
        for h in FORGED:
            host = h.format(p=self.port, q=self.port + 1)
            self.assertEqual(self.get(host, "/login")[0], 421, host)
            self.assertEqual(self.get(host, "/login", method="POST", body="name=a&password=b")[0], 421, host)
        self.assertEqual(raw(self.port, ["GET /login HTTP/1.1", "Connection: close"])[0], 421)

    def test_allow_list_lets_a_tunnel_name_in(self):
        srv = console.make_server(self.config, self.auth, "127.0.0.1", 0, allowed_hosts=["ops.example.com"])
        p = _Probe(srv)
        self.addCleanup(p.close)
        self.assertEqual(p.get("ops.example.com", "/login")[0], 200)
        self.assertEqual(p.get("ops.example.com:443", "/login")[0], 200)
        self.assertEqual(p.get("other.example.com", "/login")[0], 421)

    def test_wildcard_bind_answers_ip_literals_not_names(self):
        srv = console.make_server(self.config, self.auth, "0.0.0.0", 0)
        p = _Probe(srv)
        self.addCleanup(p.close)
        self.assertEqual(p.get(f"192.0.2.10:{p.port}", "/login")[0], 200)
        self.assertEqual(p.get(f"localhost:{p.port}", "/login")[0], 200)
        self.assertEqual(p.get(f"attacker.example:{p.port}", "/login")[0], 421)
        self.assertEqual(p.get(f"192.0.2.10:{p.port + 1}", "/login")[0], 421)


class PolicyUnitTests(unittest.TestCase):
    def test_named_bind_answers_its_name_only(self):
        pol = hostcheck.HostPolicy("ops.internal", 8080)
        self.assertTrue(pol.allows(["ops.internal:8080"]))
        self.assertFalse(pol.allows(["localhost:8080"]))
        self.assertFalse(pol.allows(["ops.internal:8081"]))

    def test_default_port_80_when_host_has_no_port(self):
        self.assertTrue(hostcheck.HostPolicy("127.0.0.1", 80).allows(["localhost"]))
        self.assertFalse(hostcheck.HostPolicy("127.0.0.1", 8765).allows(["localhost"]))


if __name__ == "__main__":
    unittest.main()
