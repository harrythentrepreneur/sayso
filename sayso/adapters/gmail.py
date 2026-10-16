"""Gmail / Google Workspace mailbox with Google sign-in (OAuth 2.0 + XOAUTH2). Stdlib only.

App passwords need 2-Step Verification and many Workspace admins block them. With
``[mailbox] adapter = "gmail"`` the mailbox signs in with a refresh token instead:

    [mailbox]
    adapter = "gmail"
    user = "help@yourshop.com"
    client_id_env = "ACME_GOOGLE_CLIENT_ID"
    client_secret_env = "ACME_GOOGLE_CLIENT_SECRET"
    refresh_token_env = "ACME_GOOGLE_REFRESH_TOKEN"

Get the refresh token once with ``python -m sayso.adapters.gmail`` (a sign-in on this
machine, docs/ONBOARDING.md). ``auth = "password"`` + ``password_env`` keeps the app
password path.

The token endpoint, refresh and the XOAUTH2 string come from ``sayso.adapters.oauth``
(shared with Microsoft). This module holds what is Gmail's own:

- The Sent folder is found by its ``\\Sent`` special-use flag, not by name: Gmail calls it
  ``[Gmail]/Sent Mail`` in most accounts and ``[Google Mail]/Sent Mail`` in some (UK/DE),
  and the owner can hide it from IMAP. Zero or two flagged folders are refused.
- Gmail labels are folders. A message counts as a sent copy only when it is in the Sent
  folder AND its ``X-GM-LABELS`` carry ``\\Sent``.
- Gmail files SMTP mail in Sent by itself. Nothing here APPENDs; an APPEND would make the
  second copy that the sender job reads as "sent twice".
- A refresh token that Google refuses (``invalid_grant``: revoked, password changed,
  unused for six months, 7-day Testing-mode expiry) is never retried: a person must sign
  in again. A throttled account (``[THROTTLED]``, bandwidth ``[ALERT]``) ends the read
  for this run without retries.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import imaplib
import re
import secrets as _secrets
import threading
import urllib.parse
from typing import Any, Callable

from sayso.config import ConfigError, Section

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SENT_FOLDER = "[Gmail]/Sent Mail"          # the usual name; the \Sent flag decides
SCOPE = "https://mail.google.com/"         # the ONLY scope Google accepts for IMAP/SMTP (restricted)
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
READ_ATTEMPTS = 3

_THROTTLED = re.compile(rb"\[(?:THROTTLED|ALERT)\]|bandwidth|too many|rate limit", re.I)
_MSGID = re.compile(r"^<[^\s<>\"\\()]{1,250}>$")


class NotUnderstood(RuntimeError):
    """Gmail answered in a shape this adapter does not know. Never guessed around."""


class SignInExpired(RuntimeError):
    """Google refused the refresh token (invalid_grant). A person must sign in again; never retried."""


class Throttled(RuntimeError):
    """Gmail is limiting this account. The run stops reading; the next run tries again."""


def _oauth():
    try:
        from sayso.adapters import oauth
    except ImportError:  # pragma: no cover - only before the shared module is merged
        raise ConfigError("[mailbox] gmail sign-in needs sayso.adapters.oauth") from None
    return oauth


def is_sign_in_expired(exc: BaseException) -> bool:
    """True when the token call said the grant is dead (``invalid_grant``)."""
    if isinstance(exc, SignInExpired):
        return True
    oauth = _oauth()
    if isinstance(exc, getattr(oauth, "ReconnectNeeded", ())):
        return True
    return getattr(exc, "code", "") == "invalid_grant"


# ----------------------------------------------------------------------------- IMAP replies
def _text(line: Any) -> str:
    return line.decode("utf-8", "replace") if isinstance(line, (bytes, bytearray)) else str(line)


_LIST = re.compile(r'^\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$')


def sent_folder(list_lines) -> str:
    """The single folder whose LIST flags include ``\\Sent``. None or several -> NotUnderstood."""
    found = []
    for line in list_lines or []:
        m = _LIST.match(_text(line).strip())
        if not m:
            continue
        if "\\sent" in m.group(1).lower().split():
            name = m.group(2).strip()
            if name.startswith('"') and name.endswith('"') and len(name) >= 2:
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            found.append(name)
    if len(found) != 1:
        raise NotUnderstood("no Sent folder in IMAP: in Gmail settings > Labels, tick 'Show in IMAP' for Sent Mail"
                            if not found else f"{len(found)} folders are flagged \\Sent")
    return found[0]


_LABELS = re.compile(r"X-GM-LABELS \(((?:[^()\"]|\"(?:[^\"\\]|\\.)*\")*)\)", re.I)
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s"]+)')


def labels(fetch_meta) -> tuple[str, ...]:
    """The labels in one ``FETCH (X-GM-LABELS (...))`` reply, e.g. ``("\\\\Inbox", "Billing")``."""
    m = _LABELS.search(_text(fetch_meta))
    if not m:
        raise NotUnderstood("FETCH reply has no X-GM-LABELS list")
    out = []
    for q, atom in _TOKEN.findall(m.group(1)):
        out.append(q.replace('\\"', '"').replace("\\\\", "\\") if q or not atom else atom)
    return tuple(out)


def _quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------------------- mailbox adapter
class GmailMailbox:
    """Mailbox protocol for Gmail: copies of one Message-ID in the account's own Sent folder.

    Read only. Signs in with XOAUTH2 (default) or an app password (``auth = "password"``)."""

    def __init__(self, section: Section, connect: Callable | None = None, transport=None, tokens=None):
        self.user = str(section.options.get("user") or "").strip()
        if not re.fullmatch(r"[^@\s\x00-\x1f]{1,64}@[A-Za-z0-9.-]{1,252}\.[A-Za-z]{2,63}", self.user):
            raise ConfigError("[mailbox] gmail needs user = the full Gmail or Workspace address")
        self.auth = str(section.options.get("auth", "oauth"))
        self.tokens = tokens
        self.password = ""
        if self.auth == "oauth":
            if self.tokens is None:
                oauth = _oauth()
                try:
                    self.tokens = oauth.TokenSource("google", section.secret("client_id"),
                                                    section.secret("client_secret"),
                                                    section.secret("refresh_token"), tenant=None, transport=transport)
                except ConfigError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the shared module's own error type
                    raise ConfigError(f"[mailbox] gmail sign-in: {type(exc).__name__}") from None
        elif self.auth == "password":
            self.password = section.secret("password")
        else:
            raise ConfigError('[mailbox] gmail auth must be "oauth" or "password"')
        self.folder = section.options.get("sent_folder")    # optional override; else the \Sent flag
        self.connect = connect or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT))

    def __repr__(self) -> str:   # never a secret
        return f"GmailMailbox(user={self.user}, auth={self.auth})"

    def _login(self, conn) -> None:
        if self.tokens is None:
            conn.login(self.user, self.password)
            return
        oauth = _oauth()
        try:
            token = self.tokens.access_token()
        except Exception as exc:  # noqa: BLE001
            if is_sign_in_expired(exc):
                raise SignInExpired("Google refused the saved sign-in (invalid_grant): sign in again") from None
            raise
        sasl = oauth.xoauth2(self.user, token)
        # imaplib base64-encodes what the callback returns. On failure Gmail sends a JSON challenge
        # ({"status":"401",...}); the SASL rules want an empty reply, after which the server says NO.
        conn.authenticate("XOAUTH2", lambda challenge: b"" if challenge else sasl)

    @staticmethod
    def _ok(res, what: str):
        status, data = res
        if status != "OK":
            blob = b" ".join(d if isinstance(d, bytes) else str(d).encode() for d in (data or []) if d)
            if _THROTTLED.search(blob):
                raise Throttled(f"Gmail is limiting this account ({what})")
            raise RuntimeError(f"IMAP {what} returned {status}")
        return data

    def sent_copies(self, message_id: str) -> int:
        mid = str(message_id or "").strip()
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        if not _MSGID.match(mid):
            raise NotUnderstood("message id is not valid")
        last: BaseException | None = None
        for _ in range(READ_ATTEMPTS):
            try:
                return self._count(mid)
            except (SignInExpired, Throttled, NotUnderstood):
                raise                      # retrying cannot help
            except imaplib.IMAP4.error as exc:
                if _THROTTLED.search(str(exc).encode()):
                    raise Throttled("Gmail is limiting this account") from None
                last = exc
            except (OSError, RuntimeError) as exc:
                last = exc
        raise RuntimeError(f"Gmail read failed {READ_ATTEMPTS} times: {type(last).__name__}") from last

    def _count(self, mid: str) -> int:
        conn = self.connect()
        try:
            self._login(conn)
            folder = self.folder or sent_folder(self._ok(conn.list(), "list"))
            self._ok(conn.select(_quote_mailbox(folder), readonly=True), "select")
            data = self._ok(conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid}"'), "search")
            raw = _text((data or [b""])[0] or b"").split()
            if not all(x.isdigit() for x in raw):
                raise NotUnderstood("SEARCH reply is not a list of message numbers")
            count = 0
            for uid in raw:
                meta = self._ok(conn.uid("FETCH", uid, "(X-GM-LABELS)"), "fetch labels")
                first = meta[0][0] if meta and isinstance(meta[0], tuple) else (meta[0] if meta else b"")
                if "\\Sent" in labels(first):
                    count += 1
            return count
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------------- one-time sign-in (self-hosters)
def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge), RFC 7636."""
    verifier = _secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str, login_hint: str = "") -> str:
    """Google's sign-in page for mailbox access: code flow, PKCE S256, offline access, consent every time
    (Google returns a refresh token only on consent)."""
    if not client_id or not state or len(state) < 16 or not code_challenge:
        raise ValueError("client_id, a random state and a PKCE challenge are required")
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
         "scope": f"{SCOPE} openid email", "access_type": "offline", "prompt": "consent",
         "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",
         "include_granted_scopes": "false"}
    if login_hint:
        q["login_hint"] = login_hint
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def check_grant(data: dict) -> str:
    """The refresh token from a code-exchange response, after checking Google granted the mail scope.
    Granular consent lets a person untick a scope: then IMAP and SMTP would fail later, so refuse now."""
    granted = set(urllib.parse.unquote(str(data.get("scope") or "")).split())
    if SCOPE not in granted:
        raise NotUnderstood("Google did not grant mail access (the Gmail box was unticked on the Allow screen)")
    rt = data.get("refresh_token")
    if not isinstance(rt, str) or not rt.strip():
        raise NotUnderstood("Google returned no refresh token (offline access was not granted)")
    return rt


TOKEN_RETRIES = 2
RETRY_CAP = 30.0
_CODE_TOKEN = re.compile(r"^[\x21-\x7e]{8,8192}$")


def _header(headers, name: str) -> str:
    for k, v in (headers or {}).items():
        if str(k).lower() == name:
            return str(v)
    return ""


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str,
                  transport=None, sleep: Callable[[float], None] | None = None) -> dict:
    """Redeem an authorization code at Google's token endpoint and check the answer.

    Only TOKEN_URL is called. 429/5xx honour Retry-After (capped) twice, then fail. ``invalid_grant`` (code
    used twice, expired, wrong verifier) raises SignInExpired. Any other refusal carries only Google's
    error code, never the body. A response without a Bearer access token, a lifetime, a refresh token and
    the mail scope is NotUnderstood."""
    import json
    import time
    from sayso.adapters import http as _http
    if not code or len(code) > 4096 or any(c in code for c in " \r\n\x00"):
        raise NotUnderstood("the sign-in code is not valid")
    send = transport or _http.urllib_transport
    wait = sleep or time.sleep
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                   "client_secret": client_secret, "redirect_uri": redirect_uri,
                                   "code_verifier": verifier}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
               "User-Agent": "sayso/0.2"}
    for attempt in range(TOKEN_RETRIES + 1):
        try:
            status, resp_headers, raw = send("POST", TOKEN_URL, dict(headers), body)
        except OSError as exc:
            if attempt < TOKEN_RETRIES:
                wait(1.0)
                continue
            raise RuntimeError(f"Google token endpoint unreachable ({type(exc).__name__})") from None
        if status == 429 or status >= 500:
            if attempt < TOKEN_RETRIES:
                try:
                    wait(max(0.0, min(float(_header(resp_headers, "retry-after") or 1), RETRY_CAP)))
                except ValueError:
                    wait(1.0)
                continue
            raise RuntimeError(f"Google token endpoint busy (HTTP {status})")
        try:
            data = json.loads(raw or b"")
        except ValueError:
            raise NotUnderstood("Google's token answer is not JSON") from None
        if status >= 400:
            err = data.get("error") if isinstance(data, dict) else ""
            err = err if isinstance(err, str) and re.fullmatch(r"[a-z_]{1,64}", err) else f"HTTP {status}"
            if err == "invalid_grant":
                raise SignInExpired("the sign-in code was refused (invalid_grant): start again")
            raise RuntimeError(f"Google refused the sign-in: {err}")
        if not isinstance(data, dict) or str(data.get("token_type", "")).lower() != "bearer":
            raise NotUnderstood("Google's token answer is not a Bearer token")
        if not isinstance(data.get("access_token"), str) or not _CODE_TOKEN.match(data["access_token"]):
            raise NotUnderstood("Google's token answer has no usable access_token")
        life = data.get("expires_in")
        if isinstance(life, bool) or not isinstance(life, int) or not 0 < life <= 86400:
            raise NotUnderstood("Google's token answer has no usable expires_in")
        rt = data.get("refresh_token")
        if rt is not None and (not isinstance(rt, str) or not _CODE_TOKEN.match(rt)):
            raise NotUnderstood("Google's token answer has an unusable refresh_token")
        check_grant(data)
        return data
    raise RuntimeError("Google token endpoint busy")  # pragma: no cover


def id_email(id_token: str, client_id: str, now: float | None = None) -> str:
    """The verified address in an id_token that came DIRECTLY from TOKEN_URL over TLS (OpenID Connect Core
    3.1.3.7 lets TLS stand in for the signature there). Audience, issuer, expiry and email_verified are checked."""
    import json
    import time
    try:
        part = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        raise NotUnderstood("id_token is not understood") from None
    if not isinstance(claims, dict) or claims.get("aud") != client_id:
        raise NotUnderstood("id_token is for another app")
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise NotUnderstood("id_token is not from Google")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or (time.time() if now is None else now) > exp + 300:
        raise NotUnderstood("id_token has expired")
    if claims.get("email_verified") is not True and str(claims.get("email_verified")).lower() != "true":
        raise NotUnderstood("Google has not verified this address")
    email = str(claims.get("email") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9.-]{1,252}\.[a-z]{2,63}", email):
        raise NotUnderstood("the sign-in did not say which mailbox it is")
    return email


def _write_secret(path: str, var: str, value: str) -> None:
    """Append ``VAR=value`` to an env file that only the owner can read (mode 600, created that way)."""
    import os
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", var):
        raise ValueError("variable name must be UPPER_CASE")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{var}={value}\n".encode())
    finally:
        os.close(fd)


def sign_in(client_id: str, client_secret: str, *, open_browser: Callable[[str], Any] | None = None,
            transport=None, timeout: float = 300, port: int = 0, login_hint: str = "") -> str:
    """One loopback sign-in (Google "Desktop app" client): returns the refresh token. Listens on 127.0.0.1
    only, for one request whose ``state`` matches; anything else is refused."""
    verifier, challenge = pkce_pair()
    state = _secrets.token_urlsafe(24)
    got: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            ok = _secrets.compare_digest(q.get("state", ""), state)
            if ok and not done.is_set():
                got.update(q)
                done.set()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab." if ok else b"This sign-in link is not valid.")

        def log_message(self, *a):   # the query holds the code: never log it
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = authorize_url(client_id, redirect, state, challenge, login_hint)
        if open_browser is None:
            print("Open this address in a browser and allow access:\n" + url, flush=True)
        else:
            open_browser(url)
        if not done.wait(timeout):
            raise TimeoutError("no sign-in arrived in time")
    finally:
        srv.shutdown()
        srv.server_close()
    if got.get("error"):
        raise NotUnderstood(f"Google said: {re.sub(r'[^a-z_]', '', got['error'])[:40] or 'error'}")
    data = exchange_code(client_id, client_secret, got.get("code", ""), redirect, verifier, transport=transport)
    return check_grant(data)


def main(argv=None) -> int:
    import argparse
    import os
    p = argparse.ArgumentParser("python -m sayso.adapters.gmail",
                                description="Sign in to Gmail once and save the refresh token to an env file.")
    p.add_argument("--client-id-env", required=True)
    p.add_argument("--client-secret-env", required=True)
    p.add_argument("--out", required=True, help="env file to append to (created with mode 600)")
    p.add_argument("--var", required=True, help="variable name, e.g. ACME_GOOGLE_REFRESH_TOKEN")
    p.add_argument("--user", default="", help="the mailbox address (pre-fills Google's account chooser)")
    a = p.parse_args(argv)
    cid, csec = os.environ.get(a.client_id_env, ""), os.environ.get(a.client_secret_env, "")
    if not cid or not csec:
        print(f"set {a.client_id_env} and {a.client_secret_env} first")
        return 2
    token = sign_in(cid, csec, login_hint=a.user)
    _write_secret(a.out, a.var, token)
    print(f"Saved {a.var} to {a.out} (mode 600). The token itself is not shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
