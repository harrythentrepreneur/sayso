"""Stand-in for ``sayso.adapters.oauth`` until the microsoft lane's module is merged (issue #17 NOTES).

Installed by tests ONLY when the real module cannot be imported. It follows the agreed interface:
``TokenSource(provider, client_id, client_secret, refresh_token, tenant=None, transport=None).access_token()``
and ``xoauth2(user, token) -> bytes``; a refused grant raises ``ReconnectNeeded`` with ``code="invalid_grant"``.
"""
from __future__ import annotations

import json
import sys
import types
import urllib.parse


def install() -> bool:
    try:
        import sayso.adapters.oauth  # noqa: F401
        return False
    except ImportError:
        pass
    mod = types.ModuleType("sayso.adapters.oauth")

    class OAuthError(RuntimeError):
        def __init__(self, message, *, code=""):
            super().__init__(message)
            self.code = code

    class ReconnectNeeded(OAuthError):
        pass

    def xoauth2(user, token):
        if not user or not token or any(c in user + token for c in "\x01\r\n\x00"):
            raise OAuthError("bad user or token")
        return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()

    class TokenSource:
        URL = {"google": "https://oauth2.googleapis.com/token"}

        def __init__(self, provider, client_id, client_secret, refresh_token, tenant=None, transport=None):
            if provider not in self.URL or not (client_id and client_secret and refresh_token):
                raise OAuthError("bad token source")
            self.provider, self._f = provider, (client_id, client_secret, refresh_token)
            self.transport, self._token = transport, None

        def access_token(self):
            if self._token:
                return self._token
            cid, sec, rt = self._f
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": cid, "client_secret": sec,
                                           "refresh_token": rt}).encode()
            status, _h, raw = self.transport("POST", self.URL[self.provider],
                                             {"Content-Type": "application/x-www-form-urlencoded"}, body)
            data = json.loads(raw or b"{}")
            if status >= 400:
                code = str(data.get("error") or "")
                if code == "invalid_grant":
                    raise ReconnectNeeded("sign-in no longer valid", code=code)
                raise OAuthError("token request refused", code=code)
            self._token = data["access_token"]
            return self._token

    mod.OAuthError, mod.ReconnectNeeded, mod.xoauth2, mod.TokenSource = OAuthError, ReconnectNeeded, xoauth2, TokenSource
    sys.modules["sayso.adapters.oauth"] = mod
    import sayso.adapters as pkg
    pkg.oauth = mod
    return True
