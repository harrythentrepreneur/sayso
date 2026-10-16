"""OAuth 2.0 for mailboxes: refresh-token grant, token cache, XOAUTH2 SASL. Stdlib only.

Microsoft has switched off Basic authentication for IMAP in every Exchange Online tenant
and turns SMTP AUTH Basic off by default at the end of December 2026. Google Workspace
mailboxes use the same SASL mechanism. This module is the one place that talks to a
provider's token endpoint; the IMAP/SMTP code only asks for ``access_token()``.

    tokens = TokenSource("microsoft", client_id, client_secret, refresh_token, tenant="contoso.com")
    conn.authenticate("XOAUTH2", imap_auth(user, tokens.access_token()))
    smtp.auth("XOAUTH2", smtp_auth(user, tokens.access_token()))

Rules (docs/ADAPTERS.md, "OAuth mailboxes"):
- Only the token endpoints in PROVIDERS are ever called. ``tenant`` goes into a URL path,
  so it must be ``common``/``organizations``/``consumers``, a tenant GUID or a domain name.
- A refresh token that no longer works (revoked, expired, consent removed) raises
  ``ReconnectNeeded``. It is never retried: a person must sign in again.
- 429 and 5xx honour Retry-After (capped), then raise ``OAuthError``.
- A token response that is not exactly understood (no Bearer type, no token, no lifetime)
  raises ``OAuthError``. Error text carries only the provider's error code, never the body.
- Refresh tokens rotate: a new one replaces the old through ``on_rotate``.
- Nothing here logs or stores a secret; the host decides where tokens live.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as _secrets
import time
import urllib.parse
from typing import Any, Callable

from sayso.adapters import http as _http

MS_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
MS_SMTP_SCOPE = "https://outlook.office.com/SMTP.Send"
GOOGLE_MAIL_SCOPE = "https://mail.google.com/"

PROVIDERS: dict[str, dict[str, Any]] = {
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "default_tenant": "common",
        "mail_scopes": (MS_IMAP_SCOPE, MS_SMTP_SCOPE),
        "scopes": (MS_IMAP_SCOPE, MS_SMTP_SCOPE, "offline_access", "openid", "email"),
        "refresh_scope": True,      # v2 endpoint: send the mail scopes again on refresh
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
        "sent_folder": "Sent Items",
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "default_tenant": None,
        "mail_scopes": (GOOGLE_MAIL_SCOPE,),
        "scopes": (GOOGLE_MAIL_SCOPE, "openid", "email"),
        "refresh_scope": False,
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl",
        "sent_folder": "[Gmail]/Sent Mail",
    },
}

# Errors that mean "a person must sign in again". Microsoft: AADSTS50173 (grant revoked, e.g. password reset),
# 65001 (no consent), 70000 (invalid grant), 70008/700082 (expired by inactivity), 90094 (admin consent needed),
# 50076/50079 (MFA now required). Google: invalid_grant.
RECONNECT_ERRORS = frozenset({"invalid_grant", "interaction_required", "consent_required", "login_required"})
RECONNECT_AADSTS = frozenset({50173, 65001, 70000, 70008, 700082, 90094, 50076, 50079, 50078})
ADMIN_CONSENT_AADSTS = frozenset({65001, 90094})

EXPIRY_SKEW = 300            # refresh 5 minutes before the provider's expiry
MAX_LIFETIME = 24 * 3600     # an access token claiming to live longer than a day is not understood
RETRIES = 2
RETRY_CAP = 30.0

_TENANT = re.compile(r"^(?:common|organizations|consumers|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                     r"|(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$")
_TOKEN = re.compile(r"^[\x21-\x7e]{8,8192}$")     # printable ASCII, no spaces or control characters
_AADSTS = re.compile(r"AADSTS(\d{5,6})")


class OAuthError(RuntimeError):
    """The token call failed or was not understood. ``code`` is the provider's error code (no secrets)."""

    def __init__(self, message: str, *, code: str = "", aadsts: int | None = None, transient: bool = False,
                 status: int = 0):
        super().__init__(message)
        self.code = code
        self.aadsts = aadsts
        self.transient = transient
        self.status = status      # HTTP status of a refusal by the provider; 0 = not a refusal


class ReconnectNeeded(OAuthError):
    """The refresh token no longer works. Never retried; a person must sign in again.

    ``admin_consent`` is True when the tenant needs an administrator to approve the app once."""

    @property
    def admin_consent(self) -> bool:
        return self.aadsts in ADMIN_CONSENT_AADSTS


# ----------------------------------------------------------------------------- small checks
def provider(name: str) -> dict[str, Any]:
    if name not in PROVIDERS:
        raise OAuthError(f"unknown OAuth provider {name!r}")
    return PROVIDERS[name]


def check_tenant(name: str, tenant: str | None) -> str | None:
    """The tenant for ``name``'s URLs. Microsoft: common/organizations/consumers, a GUID or a domain. Google: None."""
    p = provider(name)
    if p["default_tenant"] is None:
        if tenant not in (None, ""):
            raise OAuthError(f"{name} has no tenant")
        return None
    t = str(tenant or p["default_tenant"]).strip().lower()
    if not _TENANT.match(t):
        raise OAuthError("tenant must be common, organizations, consumers, a tenant id or a domain name")
    return t


def token_url(name: str, tenant: str | None = None) -> str:
    t = check_tenant(name, tenant)
    return provider(name)["token_url"].format(tenant=t or "")


def _no_sasl_breaks(value: str, what: str) -> str:
    if not value or any(c in value for c in "\x01\r\n\x00"):
        raise OAuthError(f"{what} is empty or holds a control character")
    return value


def xoauth2(user: str, token: str) -> bytes:
    """The raw XOAUTH2 SASL string: ``user={user}^Aauth=Bearer {token}^A^A``. imaplib/smtplib add the base64."""
    _no_sasl_breaks(user, "user")
    _no_sasl_breaks(token, "access token")
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


def imap_auth(user: str, token: str) -> Callable[[bytes], bytes]:
    """``authobject`` for ``IMAP4.authenticate("XOAUTH2", ...)``. On a failure the server sends a JSON error as a
    challenge; the empty reply ends the exchange so the server answers NO (and imaplib raises)."""
    sasl = xoauth2(user, token)

    def respond(challenge: bytes) -> bytes:
        return sasl if not challenge else b""
    return respond


def smtp_auth(user: str, token: str) -> Callable[..., str]:
    """``authobject`` for ``SMTP.auth("XOAUTH2", ...)`` (initial response; empty reply to an error challenge)."""
    sasl = xoauth2(user, token).decode("ascii")

    def respond(challenge: bytes | None = None) -> str:
        return sasl if challenge is None else ""
    return respond


# ----------------------------------------------------------------------------- token endpoint
def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if str(k).lower() == name:
            return str(v)
    return ""


def _retry_after(headers: dict) -> float:
    try:
        return max(0.0, min(float(_header(headers, "retry-after") or 1), RETRY_CAP))
    except ValueError:
        return 1.0


def _error_of(raw: bytes) -> tuple[str, int | None]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return "", None
    if not isinstance(data, dict):
        return "", None
    code = data.get("error")
    code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else ""
    aadsts = None
    codes = data.get("error_codes")
    if isinstance(codes, list) and codes and isinstance(codes[0], int):
        aadsts = codes[0]
    else:
        m = _AADSTS.search(str(data.get("error_description") or ""))
        aadsts = int(m.group(1)) if m else None
    return code, aadsts


def post_token(name: str, tenant: str | None, form: dict[str, str], *, transport=None,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """POST one grant to the provider's token endpoint and return the checked JSON. Raises OAuthError."""
    url = token_url(name, tenant)
    send = transport or _http.urllib_transport
    body = urllib.parse.urlencode(form).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
               "User-Agent": "sayso/0.2"}
    for attempt in range(RETRIES + 1):
        try:
            status, resp_headers, raw = send("POST", url, dict(headers), body)
        except OSError as exc:
            if attempt < RETRIES:
                sleep(1.0)
                continue
            raise OAuthError(f"token endpoint unreachable ({type(exc).__name__})", transient=True) from None
        if (status == 429 or status >= 500) and attempt < RETRIES:
            sleep(_retry_after(resp_headers))
            continue
        if status == 429 or status >= 500:
            raise OAuthError(f"token endpoint busy (HTTP {status})", transient=True)
        if status >= 400:
            code, aadsts = _error_of(raw)
            label = code or f"HTTP {status}"
            if aadsts:
                label += f" (AADSTS{aadsts})"
            if code in RECONNECT_ERRORS or (aadsts in RECONNECT_AADSTS):
                raise ReconnectNeeded(f"sign-in no longer valid: {label}", code=code, aadsts=aadsts, status=status)
            raise OAuthError(f"token request refused: {label}", code=code, aadsts=aadsts, status=status)
        try:
            data = json.loads(raw or b"")
        except ValueError:
            raise OAuthError("token response is not JSON") from None
        return _check_token_response(data)
    raise OAuthError("token endpoint busy", transient=True)   # pragma: no cover - loop always returns or raises


def _check_token_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OAuthError("token response is not an object")
    if str(data.get("token_type", "")).lower() != "bearer":
        raise OAuthError("token response is not a Bearer token")
    token = data.get("access_token")
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise OAuthError("token response has no usable access_token")
    life = data.get("expires_in")
    if isinstance(life, str) and life.isdigit():
        life = int(life)
    if isinstance(life, bool) or not isinstance(life, int) or not 0 < life <= MAX_LIFETIME:
        raise OAuthError("token response has no usable expires_in")
    rt = data.get("refresh_token")
    if rt is not None and (not isinstance(rt, str) or not _TOKEN.match(rt)):
        raise OAuthError("token response has an unusable refresh_token")
    return dict(data, expires_in=life)


def granted_scopes(data: dict) -> frozenset[str]:
    """The scopes a token response says were granted (space separated; URL-encoded in some doc examples)."""
    return frozenset(urllib.parse.unquote(str(data.get("scope") or "")).split())


def missing_scopes(name: str, data: dict) -> list[str]:
    """Mail scopes the provider did NOT grant. Empty list = IMAP and SMTP are both allowed."""
    got = {s.lower() for s in granted_scopes(data)}
    return [s for s in provider(name)["mail_scopes"] if s.lower() not in got]


class TokenSource:
    """Access tokens for ONE mailbox from a refresh token, cached until shortly before they expire.

    ``on_rotate(new_refresh_token)`` is called when the provider hands out a new refresh token (Microsoft
    rotates on every use); the host stores it and drops the old one. ``on_token(token, expires_at)`` lets a
    host keep the access token between runs; ``cached=(token, expires_at)`` restores it."""

    def __init__(self, provider_name: str, client_id: str, client_secret: str, refresh_token: str,
                 tenant: str | None = None, transport=None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, on_rotate: Callable[[str], None] | None = None,
                 on_token: Callable[[str, float], None] | None = None,
                 cached: tuple[str, float] | None = None):
        self.provider = provider_name
        provider(provider_name)
        self.tenant = check_tenant(provider_name, tenant)
        for value, what in ((client_id, "client id"), (client_secret, "client secret"),
                            (refresh_token, "refresh token")):
            if not isinstance(value, str) or not value.strip():
                raise OAuthError(f"OAuth {what} is missing")
        self.client_id, self._secret, self._refresh = client_id.strip(), client_secret, refresh_token.strip()
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.on_rotate, self.on_token = on_rotate, on_token
        self._token, self._expires_at = (cached if cached and cached[0] else (None, 0.0))
        self.refreshes = 0

    def __repr__(self) -> str:   # never the secrets
        return f"TokenSource({self.provider}, tenant={self.tenant})"

    def valid(self) -> bool:
        return bool(self._token) and self.clock() < self._expires_at - EXPIRY_SKEW

    def invalidate(self) -> None:
        """Forget the cached access token (e.g. the server refused it early); the next call refreshes."""
        self._token, self._expires_at = None, 0.0

    def access_token(self) -> str:
        if self.valid():
            return str(self._token)
        p = provider(self.provider)
        form = {"grant_type": "refresh_token", "client_id": self.client_id, "client_secret": self._secret,
                "refresh_token": self._refresh}
        if p["refresh_scope"]:
            form["scope"] = " ".join((*p["mail_scopes"], "offline_access"))
        data = post_token(self.provider, self.tenant, form, transport=self.transport, sleep=self.sleep)
        self.refreshes += 1
        self._token = data["access_token"]
        self._expires_at = self.clock() + int(data["expires_in"])
        new_rt = data.get("refresh_token")
        if new_rt and new_rt != self._refresh:
            self._refresh = new_rt
            if self.on_rotate is not None:
                self.on_rotate(new_rt)
        if self.on_token is not None:
            self.on_token(self._token, self._expires_at)
        return self._token


# ----------------------------------------------------------------------------- authorization code (hosts)
def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) with method S256 (RFC 7636)."""
    verifier = _secrets.token_urlsafe(48)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def authorize_url(name: str, client_id: str, redirect_uri: str, state: str, code_challenge: str, nonce: str = "",
                  tenant: str | None = None, login_hint: str = "", prompt: str = "select_account") -> str:
    """The sign-in page URL (authorization code + PKCE S256 + state, plus a nonce when the host checks one).

    One flow for every provider: a random state of 16+ characters and a PKCE challenge are required."""
    p = provider(name)
    t = check_tenant(name, tenant)
    if not client_id or not state or len(state) < 16 or not code_challenge:
        raise ValueError("client_id, a random state and a PKCE challenge are required")
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
         "scope": " ".join(p["scopes"]), "state": state,
         "code_challenge": code_challenge, "code_challenge_method": "S256"}
    if nonce:
        q["nonce"] = nonce
    if name == "microsoft":
        q["response_mode"] = "query"
    if name == "google":
        q["access_type"] = "offline"
        q["include_granted_scopes"] = "false"
        prompt = "consent"           # Google returns a refresh token only on consent
    if prompt:
        q["prompt"] = prompt
    if login_hint:
        q["login_hint"] = login_hint
    return p["authorize_url"].format(tenant=t or "") + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def exchange_code(name: str, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str,
                  tenant: str | None = None, *, transport=None, sleep=time.sleep) -> dict[str, Any]:
    """Redeem an authorization code. The response must carry a refresh token (offline access)."""
    if not code or len(code) > 4096 or any(c in code for c in "\r\n\x00 "):
        raise OAuthError("authorization code is not valid")
    form = {"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
            "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}
    if provider(name)["refresh_scope"]:
        form["scope"] = " ".join(provider(name)["scopes"])
    data = post_token(name, tenant, form, transport=transport, sleep=sleep)
    if not data.get("refresh_token"):
        raise OAuthError("no refresh token was returned (offline access was not granted)")
    return data


def id_claims(id_token: str, *, client_id: str, nonce: str, now: float | None = None) -> dict[str, Any]:
    """Claims of an id_token received DIRECTLY from the token endpoint over TLS (OpenID Connect Core 3.1.3.7
    allows TLS in place of the signature check there). Checks audience, nonce and expiry; raises OAuthError."""
    claims = decode_id_token(id_token, client_id=client_id, now=now)
    if not nonce or claims.get("nonce") != nonce:
        raise OAuthError("id_token nonce does not match this sign-in")
    return claims


def decode_id_token(id_token: str, *, client_id: str, now: float | None = None) -> dict[str, Any]:
    """Claims of an id_token from the token endpoint, after the audience and expiry checks (no nonce check:
    for flows that do not send one, where the PKCE-bound code already ties the answer to this sign-in)."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        raise OAuthError("id_token is not understood") from None
    if not isinstance(claims, dict):
        raise OAuthError("id_token is not understood")
    aud = claims.get("aud")
    if not client_id or (aud != client_id and not (isinstance(aud, list) and client_id in aud)):
        raise OAuthError("id_token is for another app")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or (now if now is not None else time.time()) > exp + 300:
        raise OAuthError("id_token has expired")
    return claims


def mailbox_address(claims: dict) -> str:
    """The signed-in mailbox address: ``email``, else Microsoft's ``preferred_username`` (the UPN)."""
    for key in ("email", "preferred_username", "upn"):
        v = str(claims.get(key) or "").strip().lower()
        if re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9.-]{1,252}\.[a-z]{2,63}", v):
            return v
    raise OAuthError("the sign-in did not say which mailbox it is")
