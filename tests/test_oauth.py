"""OAuth mailboxes (sayso.adapters.oauth): recorded token responses from the vendors' own docs. No network.

Fixtures are the documented examples:
- Microsoft token response and error shapes:
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- Microsoft AADSTS codes: https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes
- XOAUTH2 format and sample: https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/
  how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
- Google token response: https://developers.google.com/identity/protocols/oauth2/web-server
- PKCE S256 test vector: RFC 7636 appendix B.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import unittest
import urllib.parse
from unittest import mock

from sayso.adapters import oauth, reference
from sayso.config import ConfigError, Section

# --------------------------------------------------------------------------- socket guard
_real_connect = socket.socket.connect
_real_create = socket.create_connection
_LOCAL = ("127.0.0.1", "::1", "localhost")


def _host_of(address) -> str:
    return str(address[0] if isinstance(address, tuple) else address)


def _guard_connect(self, address):
    if _host_of(address) not in _LOCAL and not str(_host_of(address)).startswith("/"):
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


# --------------------------------------------------------------------------- doc fixtures
# Microsoft v2-oauth2-auth-code-flow, "Successful response" (refresh): verbatim fields.
MS_TOKEN = {"access_token": "eyJ0eX...EV1Q...", "token_type": "Bearer", "expires_in": 3599,
            "scope": "https%3A%2F%2Fgraph.microsoft.com%2Fmail.read",
            "refresh_token": "AwABAAAAvPM1KaPlrEqdFSBzjqfTGAMxZGUTdM0t4B4...", "id_token": "eyJ0eX...ctOD..."}
# Same doc, "Error response example".
MS_ERROR = {"error": "invalid_scope",
            "error_description": "AADSTS70011: The provided value for the input parameter 'scope' is not valid. "
                                 "The scope https://foo.microsoft.com/mail.read is not valid.\r\n"
                                 "Trace ID: 0000aaaa-11bb-cccc-dd22-eeeeee333333\r\n"
                                 "Correlation ID: aaaa0000-bb11-2222-33cc-444444dddddd\r\n"
                                 "Timestamp: 2016-01-09 02:02:12Z",
            "error_codes": [70011], "timestamp": "2016-01-09 02:02:12Z",
            "trace_id": "0000aaaa-11bb-cccc-dd22-eeeeee333333",
            "correlation_id": "aaaa0000-bb11-2222-33cc-444444dddddd"}


def ms_grant_error(aadsts: int, error: str = "invalid_grant") -> dict:
    """The documented error shape with a reference-error-codes AADSTS number."""
    return dict(MS_ERROR, error=error, error_codes=[aadsts],
                error_description=f"AADSTS{aadsts}: (from reference-error-codes)\r\nTrace ID: x")


# Google web-server doc, "refresh an access token" response.
GOOGLE_TOKEN = {"access_token": "1/fFAGRNJru1FTz70BzhT3Zg", "expires_in": 3920,
                "scope": "https://www.googleapis.com/auth/drive.metadata.readonly", "token_type": "Bearer"}


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "form": dict(urllib.parse.parse_qsl(body.decode())) if body else {}})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, payload, *rest = item
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, (rest[0] if rest else {}), raw


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def source(rec, clock=None, **kw):
    kw.setdefault("tenant", "contoso.onmicrosoft.com")
    return oauth.TokenSource("microsoft", "client-id-1", "client-secret-1", "refresh-token-1", transport=rec,
                             clock=clock or Clock(), sleep=lambda s: None, **kw)


class SocketGuardTests(unittest.TestCase):
    def test_guard_bites_on_an_outside_connection(self):
        """Seeded outside call: the guard must refuse it before any packet leaves (NOTES.md)."""
        with self.assertRaises(AssertionError) as ctx:
            socket.create_connection(("login.microsoftonline.com", 443), timeout=1)
        self.assertIn("socket guard", str(ctx.exception))
        s = socket.socket()
        try:
            with self.assertRaises(AssertionError):
                s.connect(("93.184.216.34", 443))
        finally:
            s.close()


class XOAuth2Tests(unittest.TestCase):
    def test_matches_the_microsoft_doc_example(self):
        # doc: base64("user=test@contoso.onmicrosoft.com^Aauth=Bearer EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA^A^A")
        raw = oauth.xoauth2("test@contoso.onmicrosoft.com", "EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA")
        self.assertEqual(raw, b"user=test@contoso.onmicrosoft.com\x01auth=Bearer "
                              b"EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA\x01\x01")

    def test_doc_sample_wire_string_decodes_to_our_format(self):
        # doc IMAP sample: "A01 AUTHENTICATE XOAUTH2 dXNlcj1zb21ldXNlckBleGFtcGxlLmNvbQFhdXRoPUJlYXJlciB5YTI5..."
        wire = ("dXNlcj1zb21ldXNlckBleGFtcGxlLmNvbQFhdXRoPUJlYXJlciB5YTI5LnZGOWRmdDRxbVRjMk52YjNSbGNrQmhkSFJoZG1"
                "semRHRXVZMjl0Q2cBAQ==")
        decoded = base64.b64decode(wire)
        user = decoded.split(b"\x01")[0][len(b"user="):].decode()
        token = decoded.split(b"auth=Bearer ")[1].rstrip(b"\x01").decode()
        self.assertEqual(oauth.xoauth2(user, token), decoded)

    def test_control_characters_refused(self):
        for user, token in (("a@b.c\x01auth=Bearer x", "t"), ("a@b.c", "tok\r\nA2 LOGOUT"), ("", "t"), ("a@b.c", "")):
            with self.assertRaises(oauth.OAuthError):
                oauth.xoauth2(user, token)

    def test_imap_authobject_answers_once_then_ends_on_error_challenge(self):
        fn = oauth.imap_auth("a@b.co", "tok12345")
        self.assertEqual(fn(b""), oauth.xoauth2("a@b.co", "tok12345"))
        self.assertEqual(fn(b'{"status":"401"}'), b"", "an error challenge gets the empty reply, never the token again")

    def test_smtp_authobject_initial_response(self):
        fn = oauth.smtp_auth("a@b.co", "tok12345")
        self.assertEqual(fn(), "user=a@b.co\x01auth=Bearer tok12345\x01\x01")
        self.assertEqual(fn(b"eyJzdGF0dXMiOiI0MDEifQ=="), "")


class TokenSourceTests(unittest.TestCase):
    def test_refresh_grant_shape(self):
        rec = Recorder((200, MS_TOKEN))
        self.assertEqual(source(rec).access_token(), "eyJ0eX...EV1Q...")
        call = rec.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token")
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(call["form"]["grant_type"], "refresh_token")
        self.assertEqual(call["form"]["refresh_token"], "refresh-token-1")
        self.assertEqual(call["form"]["client_id"], "client-id-1")
        self.assertEqual(call["form"]["client_secret"], "client-secret-1")
        self.assertEqual(call["form"]["scope"].split(), [oauth.MS_IMAP_SCOPE, oauth.MS_SMTP_SCOPE, "offline_access"])

    def test_default_tenant_is_common(self):
        rec = Recorder((200, MS_TOKEN))
        source(rec, tenant=None).access_token()
        self.assertIn("/common/oauth2/v2.0/token", rec.calls[0]["url"])

    def test_cached_until_five_minutes_before_expiry(self):
        clock = Clock()
        rec = Recorder((200, MS_TOKEN), (200, dict(MS_TOKEN, access_token="second-token-xyz")))
        ts = source(rec, clock)
        self.assertEqual(ts.access_token(), "eyJ0eX...EV1Q...")
        clock.t += 3599 - 301
        self.assertEqual(ts.access_token(), "eyJ0eX...EV1Q...", "still cached")
        self.assertEqual(len(rec.calls), 1)
        clock.t += 2
        self.assertEqual(ts.access_token(), "second-token-xyz", "refreshed inside the 5-minute skew")
        self.assertEqual(ts.refreshes, 2)

    def test_restored_cache_spends_no_call(self):
        clock = Clock()
        rec = Recorder()
        ts = source(rec, clock, cached=("restored-token-1", clock.t + 3000))
        self.assertEqual(ts.access_token(), "restored-token-1")
        self.assertEqual(rec.calls, [])

    def test_invalidate_forces_refresh(self):
        rec = Recorder((200, MS_TOKEN), (200, MS_TOKEN))
        ts = source(rec)
        ts.access_token()
        ts.invalidate()
        ts.access_token()
        self.assertEqual(len(rec.calls), 2)

    def test_rotated_refresh_token_is_handed_over_and_used_next(self):
        rotated, seen = [], []
        rec = Recorder((200, dict(MS_TOKEN, refresh_token="rotated-refresh-2")), (200, MS_TOKEN))
        ts = source(rec, on_rotate=rotated.append, on_token=lambda t, e: seen.append(t))
        ts.access_token()
        self.assertEqual(rotated, ["rotated-refresh-2"])
        self.assertEqual(seen, ["eyJ0eX...EV1Q..."])
        ts.invalidate()
        ts.access_token()
        self.assertEqual(rec.calls[1]["form"]["refresh_token"], "rotated-refresh-2")

    def test_same_refresh_token_is_not_a_rotation(self):
        rotated = []
        rec = Recorder((200, dict(MS_TOKEN, refresh_token="refresh-token-1")))
        source(rec, on_rotate=rotated.append).access_token()
        self.assertEqual(rotated, [])

    def test_google_provider_table(self):
        rec = Recorder((200, GOOGLE_TOKEN))
        ts = oauth.TokenSource("google", "gid", "gsecret", "grefresh", transport=rec, clock=Clock())
        self.assertEqual(ts.access_token(), "1/fFAGRNJru1FTz70BzhT3Zg")
        self.assertEqual(rec.calls[0]["url"], "https://oauth2.googleapis.com/token")
        self.assertNotIn("scope", rec.calls[0]["form"], "Google refresh keeps the granted scopes")
        self.assertEqual(oauth.PROVIDERS["google"]["mail_scopes"], ("https://mail.google.com/",))
        with self.assertRaises(oauth.OAuthError):
            oauth.TokenSource("google", "gid", "gsecret", "grefresh", tenant="contoso.com")

    def test_missing_secret_refused(self):
        for args in (("", "s", "r"), ("c", "", "r"), ("c", "s", " ")):
            with self.assertRaises(oauth.OAuthError):
                oauth.TokenSource("microsoft", *args)

    def test_unknown_provider_refused(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.TokenSource("yahoo", "c", "s", "r")

    def test_repr_has_no_secret(self):
        ts = source(Recorder())
        self.assertNotIn("client-secret-1", repr(ts))
        self.assertNotIn("refresh-token-1", repr(ts))


class TenantTests(unittest.TestCase):
    def test_allowed_forms(self):
        for t in ("common", "organizations", "consumers", "contoso.com", "contoso.onmicrosoft.com",
                  "9188040d-6c67-4c5b-b112-36a304b66dad"):
            self.assertEqual(oauth.check_tenant("microsoft", t), t)

    def test_path_injection_refused(self):
        for t in ("evil.com/../x", "common?x=1", "common#", "a b", "../common", "localhost", "x" * 300,
                  "attacker.example/oauth2"):
            with self.assertRaises(oauth.OAuthError, msg=t):
                oauth.check_tenant("microsoft", t)

    def test_only_listed_token_hosts(self):
        for name in oauth.PROVIDERS:
            host = urllib.parse.urlsplit(oauth.token_url(name, None)).hostname
            self.assertIn(host, ("login.microsoftonline.com", "oauth2.googleapis.com"))
            self.assertTrue(oauth.token_url(name, None).startswith("https://"))


class RefusalTests(unittest.TestCase):
    def assertReconnect(self, payload, status=400):
        rec = Recorder((status, payload))
        with self.assertRaises(oauth.ReconnectNeeded) as ctx:
            source(rec).access_token()
        self.assertEqual(len(rec.calls), 1, "a dead refresh token is never retried")
        return ctx.exception

    def test_revoked_grant_needs_reconnect(self):
        e = self.assertReconnect(ms_grant_error(50173))
        self.assertEqual(e.aadsts, 50173)
        self.assertFalse(e.admin_consent)

    def test_inactive_grant_needs_reconnect(self):
        for code in (70008, 700082, 70000):
            self.assertReconnect(ms_grant_error(code))

    def test_admin_consent_flagged(self):
        e = self.assertReconnect(ms_grant_error(90094, "invalid_grant"))
        self.assertTrue(e.admin_consent)
        e = self.assertReconnect(ms_grant_error(65001, "invalid_grant"))
        self.assertTrue(e.admin_consent)

    def test_interaction_required_needs_reconnect(self):
        self.assertReconnect(dict(MS_ERROR, error="interaction_required", error_codes=[50076]))

    def test_google_invalid_grant(self):
        rec = Recorder((400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
        with self.assertRaises(oauth.ReconnectNeeded):
            oauth.TokenSource("google", "c", "s", "r", transport=rec).access_token()

    def test_other_error_is_not_reconnect_and_has_no_body(self):
        rec = Recorder((400, MS_ERROR))
        with self.assertRaises(oauth.OAuthError) as ctx:
            source(rec).access_token()
        self.assertNotIsInstance(ctx.exception, oauth.ReconnectNeeded)
        self.assertEqual(str(ctx.exception), "token request refused: invalid_scope (AADSTS70011)")
        self.assertNotIn("Trace ID", str(ctx.exception))

    def test_rate_limit_honours_retry_after_then_succeeds(self):
        waits = []
        rec = Recorder((429, {"error": "temporarily_unavailable"}, {"Retry-After": "7"}), (200, MS_TOKEN))
        ts = oauth.TokenSource("microsoft", "c", "s", "r", transport=rec, clock=Clock(), sleep=waits.append)
        self.assertEqual(ts.access_token(), "eyJ0eX...EV1Q...")
        self.assertEqual(waits, [7.0])

    def test_rate_limit_wait_is_capped_and_bounded(self):
        waits = []
        busy = (429, {}, {"retry-after": "3600"})
        rec = Recorder(busy, busy, busy, (200, MS_TOKEN))
        ts = oauth.TokenSource("microsoft", "c", "s", "r", transport=rec, clock=Clock(), sleep=waits.append)
        with self.assertRaises(oauth.OAuthError) as ctx:
            ts.access_token()
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(waits, [30.0, 30.0])
        self.assertEqual(len(rec.calls), 3, "bounded: 1 try + 2 retries")

    def test_server_error_retried_then_unverified(self):
        rec = Recorder((503, b"<html>down</html>"), (503, b""), (503, b""))
        with self.assertRaises(oauth.OAuthError) as ctx:
            source(rec).access_token()
        self.assertNotIsInstance(ctx.exception, oauth.ReconnectNeeded)

    def test_network_error_retried(self):
        rec = Recorder(OSError("reset"), (200, MS_TOKEN))
        self.assertEqual(source(rec).access_token(), "eyJ0eX...EV1Q...")

    def test_not_understood_responses_refused(self):
        bad = [b"not json", [1, 2], dict(MS_TOKEN, token_type="mac"), {k: v for k, v in MS_TOKEN.items()
                                                                       if k != "access_token"},
               dict(MS_TOKEN, access_token=""), dict(MS_TOKEN, access_token="has space in it"),
               dict(MS_TOKEN, expires_in=0), dict(MS_TOKEN, expires_in=-5), dict(MS_TOKEN, expires_in="soon"),
               dict(MS_TOKEN, expires_in=True), dict(MS_TOKEN, expires_in=10 ** 9),
               {k: v for k, v in MS_TOKEN.items() if k != "expires_in"}, dict(MS_TOKEN, refresh_token=5)]
        for payload in bad:
            rec = Recorder((200, payload))
            with self.assertRaises(oauth.OAuthError, msg=repr(payload)[:60]):
                source(rec).access_token()

    def test_string_expires_in_accepted(self):
        rec = Recorder((200, dict(MS_TOKEN, expires_in="3599")))
        self.assertEqual(source(rec).access_token(), "eyJ0eX...EV1Q...")


class AuthCodeTests(unittest.TestCase):
    def test_pkce_rfc7636_vector(self):
        self.assertEqual(oauth.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        v, c = oauth.pkce_pair()
        self.assertGreaterEqual(len(v), 43)
        self.assertEqual(oauth.pkce_challenge(v), c)

    def test_authorize_url(self):
        url = oauth.authorize_url("microsoft", "cid", "https://sayso.example/connect/oauth/microsoft/callback",
                                  "st4te", "chal", "n0nce", login_hint="help@contoso.com")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.hostname, parts.path),
                         ("https", "login.microsoftonline.com", "/common/oauth2/v2.0/authorize"))
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["code_challenge"], "chal")
        self.assertEqual(q["state"], "st4te")
        self.assertEqual(q["nonce"], "n0nce")
        self.assertEqual(q["login_hint"], "help@contoso.com")
        self.assertEqual(q["scope"].split(), ["https://outlook.office.com/IMAP.AccessAsUser.All",
                                              "https://outlook.office.com/SMTP.Send", "offline_access", "openid",
                                              "email"])

    def test_google_authorize_asks_offline_consent(self):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
            oauth.authorize_url("google", "cid", "https://x.example/cb", "s", "c", "n")).query))
        self.assertEqual((q["access_type"], q["prompt"]), ("offline", "consent"))

    def test_exchange_code(self):
        rec = Recorder((200, dict(MS_TOKEN, scope=f"{oauth.MS_IMAP_SCOPE} {oauth.MS_SMTP_SCOPE}")))
        data = oauth.exchange_code("microsoft", "cid", "csec", "M.code-1", "https://x.example/cb", "verifier-1",
                                   transport=rec)
        f = rec.calls[0]["form"]
        self.assertEqual((f["grant_type"], f["code"], f["code_verifier"], f["redirect_uri"]),
                         ("authorization_code", "M.code-1", "verifier-1", "https://x.example/cb"))
        self.assertEqual(oauth.missing_scopes("microsoft", data), [])

    def test_exchange_without_refresh_token_refused(self):
        rec = Recorder((200, {k: v for k, v in MS_TOKEN.items() if k != "refresh_token"}))
        with self.assertRaises(oauth.OAuthError):
            oauth.exchange_code("microsoft", "c", "s", "code", "https://x.example/cb", "v", transport=rec)

    def test_bad_code_refused_before_any_call(self):
        rec = Recorder()
        for code in ("", "a\r\nb", "x" * 5000, "a b"):
            with self.assertRaises(oauth.OAuthError):
                oauth.exchange_code("microsoft", "c", "s", code, "https://x.example/cb", "v", transport=rec)
        self.assertEqual(rec.calls, [])

    def test_missing_scopes_reported(self):
        self.assertEqual(oauth.missing_scopes("microsoft", MS_TOKEN), [oauth.MS_IMAP_SCOPE, oauth.MS_SMTP_SCOPE])
        self.assertEqual(oauth.missing_scopes("microsoft", {"scope": oauth.MS_IMAP_SCOPE}), [oauth.MS_SMTP_SCOPE])

    @staticmethod
    def _jwt(claims):
        b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        return f"{b({'alg': 'RS256'})}.{b(claims)}.sig"

    def test_id_claims_checks_audience_nonce_expiry(self):
        good = {"aud": "cid", "nonce": "n1", "exp": 2_000, "email": "Help@Contoso.com", "tid": "t"}
        self.assertEqual(oauth.id_claims(self._jwt(good), client_id="cid", nonce="n1", now=1_000)["tid"], "t")
        for claims, nonce in ((dict(good, aud="other"), "n1"), (dict(good, nonce="n2"), "n1"), (good, ""),
                              (dict(good, exp=100), "n1"), ({k: v for k, v in good.items() if k != "exp"}, "n1")):
            with self.assertRaises(oauth.OAuthError):
                oauth.id_claims(self._jwt(claims), client_id="cid", nonce=nonce, now=1_000)
        with self.assertRaises(oauth.OAuthError):
            oauth.id_claims("not-a-jwt", client_id="cid", nonce="n1")

    def test_mailbox_address(self):
        self.assertEqual(oauth.mailbox_address({"email": "Help@Contoso.com"}), "help@contoso.com")
        self.assertEqual(oauth.mailbox_address({"preferred_username": "help@contoso.com"}), "help@contoso.com")
        with self.assertRaises(oauth.OAuthError):
            oauth.mailbox_address({"preferred_username": "+15551234567"})


OAUTH_ENV = {"T_CID": "client-id-1", "T_CSEC": "client-secret-1", "T_RT": "refresh-token-1"}


@mock.patch.dict(os.environ, OAUTH_ENV)
class ImapOAuthTests(unittest.TestCase):
    def box(self, conn, rec, **opts):
        options = {"host": "outlook.office365.com", "user": "help@contoso.com", "sent_folder": '"Sent Items"',
                   "auth": "oauth", "provider": "microsoft", "tenant": "contoso.com", "client_id_env": "T_CID",
                   "client_secret_env": "T_CSEC", "refresh_token_env": "T_RT", **opts}
        return reference.ImapMailbox(Section("imap", options), connect=lambda: conn, transport=rec)

    def test_xoauth2_instead_of_login(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b"12"])
        rec = Recorder((200, MS_TOKEN))
        self.assertEqual(self.box(conn, rec).sent_copies("abc@x"), 1)
        conn.login.assert_not_called()
        mech, fn = conn.authenticate.call_args.args
        self.assertEqual(mech, "XOAUTH2")
        self.assertEqual(fn(b""), oauth.xoauth2("help@contoso.com", "eyJ0eX...EV1Q..."))
        self.assertTrue(conn.select.call_args.kwargs["readonly"])
        self.assertIn("/contoso.com/", rec.calls[0]["url"])

    def test_one_token_for_many_reads(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b"1"])
        rec = Recorder((200, MS_TOKEN))
        box = self.box(conn, rec)
        box.sent_copies("a@x")
        box.sent_copies("b@x")
        self.assertEqual(len(rec.calls), 1)

    def test_revoked_sign_in_is_not_retried(self):
        conn = mock.Mock()
        rec = Recorder((400, ms_grant_error(50173)))
        with self.assertRaises(oauth.ReconnectNeeded):
            self.box(conn, rec).sent_copies("abc@x")
        self.assertEqual(len(rec.calls), 1)
        conn.search.assert_not_called()

    def test_password_path_unchanged(self):
        with mock.patch.dict(os.environ, {"T_PW": "pw"}):
            conn = mock.Mock()
            conn.search.return_value = ("OK", [b"1"])
            box = reference.ImapMailbox(Section("imap", {"host": "h", "user": "u", "password_env": "T_PW"}),
                                        connect=lambda: conn)
            self.assertEqual(box.auth, "password")
            box.sent_copies("abc@x")
            conn.login.assert_called_once_with("u", "pw")
            conn.authenticate.assert_not_called()

    def test_unknown_auth_refused(self):
        with self.assertRaises(ConfigError):
            self.box(mock.Mock(), Recorder(), auth="ntlm")

    def test_missing_oauth_env_refused(self):
        with self.assertRaises(ConfigError):
            self.box(mock.Mock(), Recorder(), refresh_token_env="T_NOT_SET")

    def test_bad_tenant_refused_at_config_time(self):
        with self.assertRaises(ConfigError):
            self.box(mock.Mock(), Recorder(), tenant="evil.example/x")

    def test_config_load_accepts_oauth_mailbox(self):
        import tempfile
        from pathlib import Path
        from sayso import config as config_mod
        toml = Path(__file__).resolve().parents[1] / "examples" / "acme-live.toml"
        text = toml.read_text()
        start = text.index("[mailbox]")
        end = text.index("\n[", start + 1)
        block = ('[mailbox]\nadapter = "imap"\nhost = "outlook.office365.com"\nuser = "help@contoso.com"\n'
                 'sent_folder = \'"Sent Items"\'\nauth = "oauth"\nprovider = "microsoft"\ntenant = "contoso.com"\n'
                 'client_id_env = "T_CID"\nclient_secret_env = "T_CSEC"\nrefresh_token_env = "T_RT"\n')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "acme.toml"
            p.write_text(text[:start] + block + text[end:])
            cfg = config_mod.load(p)
            sec = cfg.section("mailbox")
            self.assertEqual(sec.options["auth"], "oauth")
            box = reference.ImapMailbox(sec, connect=mock.Mock(), transport=Recorder())
            self.assertIsNotNone(box.tokens)
            p.write_text(text[:start] + block.replace('client_secret_env = "T_CSEC"',
                                                       'client_secret_env = "hunter2-raw-secret"') + text[end:])
            with self.assertRaises(ConfigError):
                config_mod.load(p)


if __name__ == "__main__":
    unittest.main()
