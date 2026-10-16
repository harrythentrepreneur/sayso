"""Gmail sign-in adapter against Google's OWN documented examples. No network, no account (issue #17).

Fixtures (verbatim from the vendor docs, see the lane's RESEARCH.md):
- token response: developers.google.com/identity/protocols/oauth2/web-server ("access_token": "1/fFAGRNJru1FTz70BzhT3Zg",
  "expires_in": 3920, "token_type": "Bearer", "refresh_token": "1//xEoDL4iW3cxlI7yDbSRFYNG01kVKM2C-259HOF2aQbI");
- XOAUTH2 wire string: developers.google.com/workspace/gmail/imap/xoauth2-protocol;
- LIST reply: developers.google.com/workspace/gmail/imap/imap-extensions.
"""
from __future__ import annotations

import base64
import imaplib
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from tests import oauth_shim

SHIM = oauth_shim.install()

from sayso.adapters import gmail, reference  # noqa: E402
from sayso.config import ConfigError, Section  # noqa: E402

# --------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect
_real_create = socket.create_connection
_LOCAL = ("127.0.0.1", "::1", "localhost")


def _host_of(address) -> str:
    return str(address[0]) if isinstance(address, tuple) else str(address)


def _guard_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and _host_of(address) not in _LOCAL:
        raise AssertionError(f"socket guard: outside connection to {_host_of(address)!r} refused")
    return _real_connect(self, address)


def _guard_create(address, *a, **k):
    if _host_of(address) not in _LOCAL:
        raise AssertionError(f"socket guard: outside connection to {_host_of(address)!r} refused")
    return _real_create(address, *a, **k)


def setUpModule():
    socket.socket.connect = _guard_connect
    socket.create_connection = _guard_create


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


# --------------------------------------------------------------------------- recorded Google answers
DOC_TOKEN = {"access_token": "1/fFAGRNJru1FTz70BzhT3Zg", "expires_in": 3920, "token_type": "Bearer",
             "scope": "https://mail.google.com/ openid https://www.googleapis.com/auth/userinfo.email",
             "refresh_token": "1//xEoDL4iW3cxlI7yDbSRFYNG01kVKM2C-259HOF2aQbI"}
DOC_REFRESH = {"access_token": "1/fFAGRNJru1FTz70BzhT3Zg", "expires_in": 3920,
               "scope": "https://mail.google.com/", "token_type": "Bearer"}
DOC_INVALID_GRANT = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
DOC_LIST = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
            b'(\\HasNoChildren \\Important) "/" "[Gmail]/Important"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
            b'(\\HasNoChildren \\Flagged) "/" "[Gmail]/Starred"', b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']
# Google's sample: user=someuser@example.com, token ya29.vF9dft4qmTc2Nvb3RlckBhdHRhdmlzdGEuY29tCg
DOC_XOAUTH2_B64 = ("dXNlcj1zb21ldXNlckBleGFtcGxlLmNvbQFhdXRoPUJlYXJlciB5YTI5LnZGOWRmdDRxbVRjMk52"
                   "YjNSbGNrQmhkSFJoZG1semRHRXVZMjl0Q2cBAQ==")


class Recorder:
    """An injectable transport: answers from a list and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "form": dict(urllib.parse.parse_qsl(body.decode())) if body else {}})
        status, payload, *rest = self.answers.pop(0)
        return status, (rest[0] if rest else {}), json.dumps(payload).encode()


def fake_imap(list_lines=DOC_LIST, search=b"5", label_reply=b'5 (X-GM-LABELS (\\Sent) UID 5)', select="OK"):
    conn = mock.Mock()
    conn.list.return_value = ("OK", list(list_lines))
    conn.select.return_value = (select, [b"1"])

    def uid(cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [search])
        return ("OK", [(label_reply, b"")])
    conn.uid.side_effect = uid
    return conn


ENV = {"G_ID": "123.apps.googleusercontent.com", "G_SECRET": "doc-secret", "G_RT": DOC_TOKEN["refresh_token"],
       "G_PW": "abcdabcdabcdabcd"}
OAUTH_OPTS = {"user": "help@example.com", "client_id_env": "G_ID", "client_secret_env": "G_SECRET",
              "refresh_token_env": "G_RT"}


class SocketGuardTests(unittest.TestCase):
    def test_guard_bites_on_an_outside_connection(self):
        """Seeded outside call: the guard must refuse it (proves no test can reach Google)."""
        with self.assertRaises(AssertionError) as ctx:
            socket.create_connection(("oauth2.googleapis.com", 443), timeout=1)
        self.assertIn("socket guard", str(ctx.exception))
        s = socket.socket()
        try:
            with self.assertRaises(AssertionError):
                s.connect(("imap.gmail.com", 993))
        finally:
            s.close()


@mock.patch.dict(os.environ, ENV)
class MailboxTests(unittest.TestCase):
    def box(self, conn, *answers, **opts):
        self.rec = Recorder(*(answers or [(200, DOC_REFRESH)]))
        return gmail.GmailMailbox(Section("gmail", {**OAUTH_OPTS, **opts}), connect=lambda: conn, transport=self.rec)

    def test_counts_one_sent_copy_over_xoauth2_read_only(self):
        conn = fake_imap()
        self.assertEqual(self.box(conn).sent_copies("abc@x"), 1)
        mech, cb = conn.authenticate.call_args.args
        self.assertEqual(mech, "XOAUTH2")
        self.assertEqual(cb(b""), b"user=help@example.com\x01auth=Bearer 1/fFAGRNJru1FTz70BzhT3Zg\x01\x01")
        self.assertEqual(cb(b'{"status":"401"}'), b"", "an error challenge gets the empty reply SASL requires")
        conn.login.assert_not_called()
        self.assertEqual(conn.select.call_args.args[0], '"[Gmail]/Sent Mail"')
        self.assertTrue(conn.select.call_args.kwargs["readonly"], "the Sent folder is opened read-only")
        self.assertEqual(conn.uid.call_args_list[0].args, ("SEARCH", None, "HEADER", "Message-ID", '"<abc@x>"'))
        self.assertEqual(self.rec.calls[0]["url"], "https://oauth2.googleapis.com/token")
        self.assertEqual(self.rec.calls[0]["form"]["grant_type"], "refresh_token")
        conn.append.assert_not_called()

    def test_doc_xoauth2_vector(self):
        from sayso.adapters import oauth
        raw = oauth.xoauth2("someuser@example.com", "ya29.vF9dft4qmTc2Nvb3RlckBhdHRhdmlzdGEuY29tCg")
        self.assertEqual(base64.b64encode(raw).decode(), DOC_XOAUTH2_B64)

    def test_two_copies_are_reported_as_two(self):
        conn = fake_imap(search=b"5 9")
        self.assertEqual(self.box(conn).sent_copies("<abc@x>"), 2, "a duplicate send must be visible")

    def test_copy_without_sent_label_is_not_a_receipt(self):
        conn = fake_imap(label_reply=b'5 (X-GM-LABELS (\\Inbox "Customer") UID 5)')
        self.assertEqual(self.box(conn).sent_copies("abc@x"), 0)

    def test_localised_sent_folder_found_by_flag(self):
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Google Mail]/Gesendet"']
        conn = fake_imap(list_lines=lines)
        self.box(conn).sent_copies("abc@x")
        self.assertEqual(conn.select.call_args.args[0], '"[Google Mail]/Gesendet"')

    def test_hidden_sent_folder_refused_not_guessed(self):
        conn = fake_imap(list_lines=[b'(\\HasNoChildren) "/" "INBOX"'])
        with self.assertRaises(gmail.NotUnderstood) as ctx:
            self.box(conn).sent_copies("abc@x")
        self.assertIn("Show in IMAP", str(ctx.exception))
        self.assertEqual(conn.list.call_count, 1, "a shape we do not know is not retried")

    def test_two_sent_folders_refused(self):
        conn = fake_imap(list_lines=DOC_LIST + [b'(\\Sent) "/" "Sent Copy"'])
        with self.assertRaises(gmail.NotUnderstood):
            self.box(conn).sent_copies("abc@x")

    def test_garbled_search_reply_refused(self):
        conn = fake_imap(search=b"5 junk")
        with self.assertRaises(gmail.NotUnderstood):
            self.box(conn).sent_copies("abc@x")

    def test_bad_message_id_refused_before_any_call(self):
        conn = fake_imap()
        with self.assertRaises(gmail.NotUnderstood):
            self.box(conn).sent_copies('a"b OR ALL')
        conn.authenticate.assert_not_called()

    def test_revoked_sign_in_is_not_retried(self):
        conn = fake_imap()
        box = self.box(conn, (400, DOC_INVALID_GRANT))
        with self.assertRaises(gmail.SignInExpired):
            box.sent_copies("abc@x")
        self.assertEqual(len(self.rec.calls), 1, "a dead refresh token is asked about once, never in a loop")
        conn.authenticate.assert_not_called()

    def test_throttled_account_is_not_retried(self):
        conn = fake_imap()
        conn.select.side_effect = None
        conn.list.return_value = ("NO", [b"[THROTTLED] Account exceeded command or bandwidth limits"])
        with self.assertRaises(gmail.Throttled):
            self.box(conn).sent_copies("abc@x")
        self.assertEqual(conn.list.call_count, 1)

    def test_throttle_raised_by_imaplib_is_not_retried(self):
        conn = fake_imap()
        conn.authenticate.side_effect = imaplib.IMAP4.error("[ALERT] Too many simultaneous connections")
        with self.assertRaises(gmail.Throttled):
            self.box(conn).sent_copies("abc@x")
        self.assertEqual(conn.authenticate.call_count, 1)

    def test_dropped_connection_retried_three_times_then_fails(self):
        conn = fake_imap()
        conn.list.side_effect = OSError("peer closed")
        with self.assertRaises(RuntimeError):
            self.box(conn).sent_copies("abc@x")
        self.assertEqual(conn.list.call_count, 3)

    def test_dropped_connection_then_success(self):
        conn = fake_imap()
        conn.list.side_effect = [OSError("x"), ("OK", list(DOC_LIST))]
        self.assertEqual(self.box(conn).sent_copies("abc@x"), 1)

    def test_password_auth_keeps_app_password_path(self):
        conn = fake_imap()
        box = gmail.GmailMailbox(Section("gmail", {"user": "help@example.com", "auth": "password",
                                                   "password_env": "G_PW"}), connect=lambda: conn)
        self.assertEqual(box.sent_copies("abc@x"), 1)
        conn.login.assert_called_once_with("help@example.com", "abcdabcdabcdabcd")
        conn.authenticate.assert_not_called()

    def test_config_refusals(self):
        with self.assertRaises(ConfigError):
            gmail.GmailMailbox(Section("gmail", {**OAUTH_OPTS, "user": "not-an-address"}))
        with self.assertRaises(ConfigError):
            gmail.GmailMailbox(Section("gmail", {**OAUTH_OPTS, "auth": "plain"}))
        with self.assertRaises(ConfigError):
            gmail.GmailMailbox(Section("gmail", {"user": "help@example.com", "client_id_env": "G_ID"}))
        with mock.patch.dict(os.environ, {"G_RT": ""}):
            with self.assertRaises(ConfigError):
                gmail.GmailMailbox(Section("gmail", OAUTH_OPTS))

    def test_repr_has_no_secret(self):
        box = self.box(fake_imap())
        self.assertNotIn(ENV["G_SECRET"], repr(box))
        self.assertNotIn(ENV["G_RT"], repr(box))

    def test_wired_as_mailbox_choice(self):
        from sayso.config import ADAPTER_CHOICES
        self.assertIn("gmail", ADAPTER_CHOICES["mailbox"])
        box = reference.build("mailbox", "gmail", Section("gmail", OAUTH_OPTS), config=None)
        self.assertIsInstance(box, gmail.GmailMailbox)


class LabelTests(unittest.TestCase):
    def test_doc_style_reply(self):
        self.assertEqual(gmail.labels(b'1 (X-GM-LABELS (\\Inbox \\Sent Important "Muy Importante") UID 1)'),
                         ("\\Inbox", "\\Sent", "Important", "Muy Importante"))

    def test_empty_list(self):
        self.assertEqual(gmail.labels(b"1 (X-GM-LABELS () UID 1)"), ())

    def test_missing_list_refused(self):
        with self.assertRaises(gmail.NotUnderstood):
            gmail.labels(b"1 (UID 1)")

    def test_sent_folder_of_doc_list(self):
        self.assertEqual(gmail.sent_folder(DOC_LIST), "[Gmail]/Sent Mail")


class AuthorizeTests(unittest.TestCase):
    def test_offline_consent_pkce_state(self):
        verifier, challenge = gmail.pkce_pair()
        url = gmail.authorize_url("123.apps.googleusercontent.com", "https://sayso.example/cb", "s" * 32, challenge,
                                  login_hint="help@example.com")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", "https://accounts.google.com/o/oauth2/v2/auth")
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(q["access_type"], "offline")
        self.assertEqual(q["prompt"], "consent")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["state"], "s" * 32)
        self.assertEqual(q["scope"].split()[0], "https://mail.google.com/")
        self.assertEqual(q["login_hint"], "help@example.com")
        import hashlib
        self.assertEqual(q["code_challenge"], base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())

    def test_weak_state_refused(self):
        with self.assertRaises(ValueError):
            gmail.authorize_url("id", "https://x/cb", "short", "c")
        with self.assertRaises(ValueError):
            gmail.authorize_url("id", "https://x/cb", "s" * 32, "")


class CheckGrantTests(unittest.TestCase):
    """check_grant is public (Cloud and the sign-in CLI call it): it must refuse on its own, not only through
    the shared oauth.exchange_code."""

    def test_refresh_token_required(self):
        for bad in ({k: v for k, v in DOC_TOKEN.items() if k != "refresh_token"}, dict(DOC_TOKEN, refresh_token=" ")):
            with self.subTest(bad=bad):
                with self.assertRaises(gmail.NotUnderstood):
                    gmail.check_grant(bad)
        self.assertEqual(gmail.check_grant(DOC_TOKEN), DOC_TOKEN["refresh_token"])


class ExchangeTests(unittest.TestCase):
    def ex(self, *answers, code="4/P7q7W91a-oMsCeLvIaQm6bTrgtp7"):
        self.rec = Recorder(*answers)
        self.slept = []
        return gmail.exchange_code("cid", "csecret", code, "https://sayso.example/cb", "v" * 64, transport=self.rec,
                                   sleep=self.slept.append)

    def test_doc_response_accepted(self):
        data = self.ex((200, DOC_TOKEN))
        self.assertEqual(data["refresh_token"], DOC_TOKEN["refresh_token"])
        call = self.rec.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://oauth2.googleapis.com/token"))
        self.assertEqual(call["form"]["grant_type"], "authorization_code")
        self.assertEqual(call["form"]["code_verifier"], "v" * 64)
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_no_refresh_token_refused(self):
        with self.assertRaises(gmail.NotUnderstood):
            self.ex((200, {k: v for k, v in DOC_TOKEN.items() if k != "refresh_token"}))

    def test_mail_scope_unticked_refused(self):
        with self.assertRaises(gmail.NotUnderstood) as ctx:
            self.ex((200, dict(DOC_TOKEN, scope="openid https://www.googleapis.com/auth/userinfo.email")))
        self.assertIn("unticked", str(ctx.exception))

    def test_invalid_grant(self):
        with self.assertRaises(gmail.SignInExpired):
            self.ex((400, {"error": "invalid_grant", "error_description": "Bad Request"}))

    def test_other_refusal_keeps_only_the_code(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.ex((401, {"error": "invalid_client", "error_description": "secret csecret is wrong"}))
        self.assertIn("invalid_client", str(ctx.exception))
        self.assertNotIn("csecret", str(ctx.exception))

    def test_rate_limit_honours_retry_after(self):
        data = self.ex((429, {"error": "rate_limit_exceeded"}, {"Retry-After": "7"}), (200, DOC_TOKEN))
        self.assertEqual(self.slept, [7.0])
        self.assertEqual(data["access_token"], DOC_TOKEN["access_token"])

    def test_rate_limit_bounded_and_capped(self):
        with self.assertRaises(RuntimeError):
            self.ex(*[(503, {}, {"Retry-After": "9999"})] * 3)
        self.assertEqual(self.slept, [30.0, 30.0])
        self.assertEqual(len(self.rec.calls), 3)

    def test_not_understood_answers(self):
        for bad in ({"access_token": "x" * 20, "expires_in": 3920, "token_type": "mac"},
                    dict(DOC_TOKEN, access_token=""), dict(DOC_TOKEN, expires_in="soon"),
                    dict(DOC_TOKEN, expires_in=10 ** 9), dict(DOC_TOKEN, refresh_token="has space"), ["list"]):
            with self.subTest(bad=bad):
                with self.assertRaises(gmail.NotUnderstood):
                    self.ex((200, bad))

    def test_bad_code_refused_before_any_call(self):
        rec = Recorder()
        with self.assertRaises(gmail.NotUnderstood):
            gmail.exchange_code("cid", "s", "a b", "https://x/cb", "v", transport=rec)
        self.assertEqual(rec.calls, [])


class IdTokenTests(unittest.TestCase):
    @staticmethod
    def tok(**claims):
        base = {"iss": "https://accounts.google.com", "aud": "cid", "exp": 2000, "email": "Help@Example.com",
                "email_verified": True}
        base.update(claims)
        body = base64.urlsafe_b64encode(json.dumps(base).encode()).rstrip(b"=").decode()
        return f"e30.{body}.sig"

    def test_email_read(self):
        self.assertEqual(gmail.id_email(self.tok(), "cid", now=1000), "help@example.com")

    def test_refusals(self):
        for claims in ({"aud": "other"}, {"iss": "https://evil.example"}, {"exp": 100}, {"email_verified": False},
                       {"email": "nope"}):
            with self.subTest(claims=claims):
                with self.assertRaises(gmail.NotUnderstood):
                    gmail.id_email(self.tok(**claims), "cid", now=1000)
        with self.assertRaises(gmail.NotUnderstood):
            gmail.id_email("garbage", "cid")


class SignInCliTests(unittest.TestCase):
    def test_loopback_sign_in_checks_state_and_writes_mode_600(self):
        rec = Recorder((200, DOC_TOKEN))
        seen = {}
        statuses = []

        def browser(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            seen.update(q)

            def go():
                bad = q["redirect_uri"] + "/?code=forged-code&state=forged"
                try:
                    urllib.request.urlopen(bad, timeout=5).read()
                    statuses.append(200)
                except urllib.error.HTTPError as exc:
                    statuses.append(exc.code)
                    exc.close()
                urllib.request.urlopen(q["redirect_uri"] + "/?" + urllib.parse.urlencode(
                    {"code": "4/P7q7W91a-oMsCeLvIaQm6bTrgtp7", "state": q["state"]}), timeout=5).read()
            threading.Thread(target=go, daemon=True).start()

        token = gmail.sign_in("cid", "csecret", open_browser=browser, transport=rec, timeout=10)
        self.assertEqual(token, DOC_TOKEN["refresh_token"])
        self.assertTrue(seen["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertEqual(rec.calls[0]["form"]["redirect_uri"], seen["redirect_uri"])
        self.assertEqual(rec.calls[0]["form"]["code"], "4/P7q7W91a-oMsCeLvIaQm6bTrgtp7",
                         "only the request carrying our state may deliver the code")
        self.assertEqual(statuses, [400], "the forged-state request is refused")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "acme.env")
            gmail._write_secret(path, "ACME_GOOGLE_REFRESH_TOKEN", token)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with self.assertRaises(ValueError):
                gmail._write_secret(path, "bad name", token)


if __name__ == "__main__":
    unittest.main()
