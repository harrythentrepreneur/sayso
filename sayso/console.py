"""The operator console: the dashboard plus sign-in and approved actions.

    sayso console add-user --config acme.toml --name "Operator One" --operator 1001
    sayso console serve    --config acme.toml [--host 0.0.0.0 --port 8080]

What it adds to the read-only dashboard:
- Sign-in. Users live in <state>/console-users.json with PBKDF2 password
  hashes; each user maps to ONE operator id from [operators]. Nobody else can
  sign in, and nobody can act as a different operator.
- Web votes. Yes / No on a decision is recorded in <state>/web-votes.json,
  bound to the vote's fingerprint at the moment the operator saw it. The
  console NEVER sends, charges or merges. The next tally counts the web vote
  exactly like a board vote (only operators count, any No declines), and the
  normal executors act with every existing safety check.
- Pause and resume, recorded in the audit log with the operator's name.
- "Run the loop once" only on dry-run products (fake adapters), for demos.

Security:
- Session cookie: HMAC-signed, HttpOnly, SameSite=Strict, 12 h; Secure unless
  the console is on loopback. The signing secret comes from the environment
  variable named by --secret-env (default HCML_CONSOLE_SECRET); it is never
  stored in the state dir.
- Every POST needs a CSRF token tied to the session.
- 10 failed sign-ins from one address in 15 minutes locks that address out.
- A different auth provider (for example Clerk) can replace `Auth.user_for`
  without touching the action code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sayso import dashboard, votes
from sayso.config import Config
from sayso.store import load_json, locked, save_json

SESSION_HOURS = 12
PBKDF2_ROUNDS = 600_000
MAX_FAILS, FAIL_WINDOW = 10, 15 * 60
ANSWERS = ("yes", "no")


class ConsoleError(RuntimeError):
    """A console action was refused. Nothing changed."""


# -- users ------------------------------------------------------------------------

def users_path(config: Config) -> Path:
    return config.state_dir / "console-users.json"


def web_votes_path(config: Config) -> Path:
    return config.state_dir / "web-votes.json"


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ConsoleError("password must be at least 10 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def add_user(config: Config, name: str, operator_id: str, password: str) -> dict:
    if str(operator_id) not in config.operators:
        raise ConsoleError(f"operator id {operator_id!r} is not in [operators]; only operators can sign in")
    path = users_path(config)
    with locked(path):
        users = load_json(path)
        users[name.strip().lower()] = {"name": name.strip(), "operator_id": str(operator_id),
                                       "password": hash_password(password),
                                       "version": secrets.token_hex(4)}
        save_json(path, users)
    return users[name.strip().lower()]


INVITE_MINUTES = 60


def invites_path(config: Config) -> Path:
    return config.state_dir / "console-invites.json"


def make_invite(config: Config, name: str, operator_id: str, minutes: int = INVITE_MINUTES) -> str:
    """A single-use link token that lets ONE named operator set their own password.
    Only its sha256 is stored; the token itself is shown once."""
    if str(operator_id) not in config.operators:
        raise ConsoleError(f"operator id {operator_id!r} is not in [operators]")
    token = secrets.token_urlsafe(24)
    path = invites_path(config)
    with locked(path):
        inv = load_json(path)
        inv[hashlib.sha256(token.encode()).hexdigest()] = {"name": name.strip(), "operator_id": str(operator_id),
                                                           "exp": int(time.time()) + minutes * 60}
        save_json(path, inv)
    return token


def redeem_invite(config: Config, token: str, password: str | None, *, peek: bool = False) -> dict | None:
    """Check (peek) or use an invite. Used or expired invites never work again."""
    path = invites_path(config)
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    with locked(path):
        inv = load_json(path)
        rec = inv.get(digest)
        if rec is None or rec["exp"] < time.time():
            return None
        if peek:
            return rec
        user = add_user(config, rec["name"], rec["operator_id"], password or "")  # raises if too short
        del inv[digest]
        save_json(path, inv)
    return user


def invite_page(name: str, error: str = "") -> str:
    err = f"<div class=err>{html.escape(error)}</div>" if error else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><meta name=robots content=noindex><title>Set password</title>"
            f"<style>{LOGIN_CSS}</style></head><body><form method=post><div class=logo>{dashboard.SAYSO_MARK}</div>"
            f"<h1>Welcome, {html.escape(name)}</h1><p>Choose your password (10+ characters). This link works once.</p>"
            f"<label for=p>New password</label><input id=p name=password type=password minlength=10 "
            f"autocomplete=new-password required autofocus>{err}<button>Save and sign in</button></form></body></html>")


# -- sessions -----------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    def __init__(self, config: Config, secret: str):
        if not secret or len(secret) < 32:
            raise ConsoleError("console secret must be at least 32 characters (set it in the environment)")
        self.config = config
        self.key = secret.encode()
        self.fails: dict[str, list[float]] = {}

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self.key, payload, hashlib.sha256).digest())

    def make_cookie(self, user: dict, now: float | None = None) -> str:
        exp = int((now or time.time()) + SESSION_HOURS * 3600)
        payload = json.dumps({"u": user["name"].lower(), "v": user["version"], "exp": exp,
                              "n": secrets.token_hex(8)}, separators=(",", ":")).encode()
        return _b64(payload) + "." + self._sign(payload)

    def user_for(self, cookie: str | None, now: float | None = None) -> dict | None:
        """The signed-in user, or None. Swap this for another provider (e.g. Clerk) if wanted."""
        if not cookie or "." not in cookie:
            return None
        body, sig = cookie.rsplit(".", 1)
        try:
            payload = _unb64(body)
        except ValueError:
            return None
        if not hmac.compare_digest(self._sign(payload), sig):
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if data.get("exp", 0) < (now or time.time()):
            return None
        user = load_json(users_path(self.config)).get(data.get("u", ""))
        if not user or user.get("version") != data.get("v") or user["operator_id"] not in self.config.operators:
            return None  # removed, password changed, or no longer an operator
        return {**user, "session": cookie}

    def csrf(self, user: dict) -> str:
        return self._sign(("csrf|" + user["session"]).encode())

    def csrf_ok(self, user: dict, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf(user), token)

    def locked_out(self, addr: str, now: float | None = None) -> bool:
        now = now or time.time()
        recent = [t for t in self.fails.get(addr, []) if now - t < FAIL_WINDOW]
        self.fails[addr] = recent
        return len(recent) >= MAX_FAILS

    def sign_in(self, addr: str, name: str, password: str) -> dict | None:
        if self.locked_out(addr):
            return None
        user = load_json(users_path(self.config)).get((name or "").strip().lower())
        # Always run one hash so a missing user takes as long as a wrong password.
        ok = check_password(password or "", user["password"] if user else hash_password("x" * 10))
        if not user or not ok or user["operator_id"] not in self.config.operators:
            self.fails.setdefault(addr, []).append(time.time())
            return None
        self.fails.pop(addr, None)
        return user


# -- actions (record only; the loop acts) ---------------------------------------------

def record_web_vote(config: Config, journal, *, user: dict, key: str, answer: str, fingerprint: str) -> str:
    """Record one operator's Yes/No for one exact revision. Never acts."""
    if answer not in ANSWERS:
        raise ConsoleError("answer must be yes or no")
    rec = load_json(config.state_dir / "votes.json").get(key)
    if rec is None:
        raise ConsoleError("no such decision")
    if rec.get("status") != votes.OPEN:
        raise ConsoleError(f"this decision is already {rec.get('status')}; nothing recorded")
    if not hmac.compare_digest(str(rec.get("fingerprint")), str(fingerprint)):
        raise ConsoleError("what you saw has changed since the page loaded; reload and look again")
    path = web_votes_path(config)
    with locked(path):
        web = load_json(path)
        web.setdefault(key, {})[user["operator_id"]] = {
            "answer": answer, "fingerprint": fingerprint, "by": user["name"],
            "at": datetime.now(timezone.utc).isoformat()}
        save_json(path, web)
    journal.append("web_vote", key=key, answer=answer, by=user["name"])
    return answer


def web_votes_for(ctx, rec: dict) -> dict[str, str]:
    """Web votes that still match this vote's fingerprint, as {operator id: answer}. Used by tally."""
    path = web_votes_path(ctx.config)
    if not path.exists():
        return {}
    return {uid: v["answer"] for uid, v in load_json(path).get(rec["key"], {}).items()
            if v.get("fingerprint") == rec["fingerprint"] and v.get("answer") in ANSWERS}


# -- web -------------------------------------------------------------------------------

LOGIN_CSS = """body{margin:0;min-height:100vh;display:grid;place-items:center;background:#27212d;
font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2433}
form{background:#fffdf8;padding:32px 30px;border-radius:14px;width:min(360px,90vw);box-shadow:0 20px 60px #0006}
h1{font-size:20px;margin:0 0 4px}p{color:#5b6478;margin:0 0 18px;font-size:13.5px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 5px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #cfd5e3;border-radius:8px;font:inherit}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:#e8653b;color:#fff;
font-weight:600;font:inherit;cursor:pointer}.err{color:#c92a2a;font-size:13px;margin-top:10px}
.logo{width:44px;height:44px;margin-bottom:10px}.logo svg{width:44px;height:44px;display:block}"""


def login_page(error: str = "") -> str:
    err = f"<div class=err>{html.escape(error)}</div>" if error else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><meta name=robots content=noindex><title>Sign in</title>"
            f"<style>{LOGIN_CSS}</style></head><body><form method=post action=/login><div class=logo>{dashboard.SAYSO_MARK}</div>"
            f"<h1>Sayso</h1><p>Nothing goes out without your say-so. Operators only.</p>"
            f"<label for=u>Name</label><input id=u name=name autocomplete=username required autofocus>"
            f"<label for=p>Password</label><input id=p name=password type=password "
            f"autocomplete=current-password required>{err}<button>Sign in</button></form></body></html>")


def make_server(config: Config, auth: Auth, host: str = "127.0.0.1", port: int = 8080,
                *, secure_cookie: bool | None = None, label: str | None = None, allow_tick: bool = False,
                vote_off_reason: str | None = None) -> ThreadingHTTPServer:
    from sayso.runtime import Journal, Clock
    journal = Journal(config.state_dir / "journal.jsonl", Clock())
    if secure_cookie is None:
        secure_cookie = host not in ("127.0.0.1", "localhost", "::1")
    products = [config.slug]
    reader = dashboard.board_reader_for(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "sayso-console"

        def _send(self, status, ctype, body, extra=(), head=False):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _redirect(self, to, extra=()):
            self._send(303, "text/plain", "", [("Location", to), *extra])

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "sayso_session":
                    return v
            return None

        def _set_cookie(self, value, max_age):
            flags = "; Secure" if secure_cookie else ""
            return ("Set-Cookie", f"sayso_session={value}; Path=/; HttpOnly; SameSite=Strict; "
                                  f"Max-Age={max_age}{flags}")

        def _addr(self):
            return (self.headers.get("CF-Connecting-IP") if self.headers.get("CF-Connecting-IP") and
                    self.client_address[0] in ("127.0.0.1", "::1") else self.client_address[0])

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10_000:
                raise ConsoleError("form too large")
            return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}

        def _snapshot(self, user):
            s = dashboard.Snapshot(config, reader)
            s.label = label
            s.console = {"user": user["name"], "csrf": auth.csrf(user),
                         "can_tick": allow_tick and not config.live,
                         "can_vote": vote_off_reason is None, "vote_off_reason": vote_off_reason}
            return s

        def _get(self, head):
            path = urlparse(self.path).path
            if path == "/login":
                return self._send(200, "text/html", login_page(), head=head)
            if path.startswith("/invite/"):
                rec = redeem_invite(config, path.split("/", 2)[2], None, peek=True)
                if rec is None:
                    return self._send(410, "text/html", login_page("That invite link is used or expired."), head=head)
                return self._send(200, "text/html", invite_page(rec["name"]), head=head)
            user = auth.user_for(self._cookie())
            if user is None:
                return self._redirect("/login")
            if path == "/":
                return self._redirect(f"/p/{config.slug}/")
            try:
                status, ctype, body = dashboard.route(self.path, lambda _slug: self._snapshot(user), products)
            except Exception as exc:  # noqa: BLE001 - corrupt state is shown, never hidden
                status, ctype, body = 500, "text/plain", f"state could not be read: {type(exc).__name__}"
            self._send(status, ctype, body, head=head)

        def do_GET(self):  # noqa: N802
            self._get(False)

        def do_HEAD(self):  # noqa: N802
            self._get(True)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                form = self._form()
            except (ConsoleError, ValueError):
                return self._send(413, "text/plain", "form too large")
            if path == "/login":
                user = auth.sign_in(self._addr(), form.get("name", ""), form.get("password", ""))
                if user is None:
                    msg = ("Too many attempts. Try again in 15 minutes." if auth.locked_out(self._addr())
                           else "Wrong name or password.")
                    return self._send(401, "text/html", login_page(msg))
                return self._redirect(f"/p/{config.slug}/", [self._set_cookie(auth.make_cookie(user),
                                                                              SESSION_HOURS * 3600)])
            if path.startswith("/invite/"):
                token = path.split("/", 2)[2]
                rec = redeem_invite(config, token, None, peek=True)
                if rec is None:
                    return self._send(410, "text/html", login_page("That invite link is used or expired."))
                try:
                    user = redeem_invite(config, token, form.get("password", ""))
                except ConsoleError as exc:
                    return self._send(400, "text/html", invite_page(rec["name"], str(exc)))
                if user is None:
                    return self._send(410, "text/html", login_page("That invite link is used or expired."))
                return self._redirect(f"/p/{config.slug}/", [self._set_cookie(auth.make_cookie(user),
                                                                              SESSION_HOURS * 3600)])
            user = auth.user_for(self._cookie())
            if user is None:
                return self._send(401, "text/plain", "sign in first")
            if not auth.csrf_ok(user, form.get("csrf")):
                return self._send(403, "text/plain", "form expired; reload the page")
            if path == "/logout":
                return self._redirect("/login", [self._set_cookie("", 0)])
            base = f"/p/{config.slug}"
            try:
                if path == f"{base}/vote":
                    if vote_off_reason is not None:
                        raise ConsoleError(f"voting is switched off here: {vote_off_reason}")
                    record_web_vote(config, journal, user=user, key=form.get("key", ""),
                                    answer=form.get("answer", ""), fingerprint=form.get("fp", ""))
                    return self._redirect(f"{base}/?done={form.get('answer')}")
                if path == f"{base}/pause":
                    flag = config.state_dir / "PAUSED"
                    flag.write_text(f"paused by {user['name']} from the console\n", encoding="utf-8")
                    journal.append("paused", reason=f"by {user['name']} (console)")
                    return self._redirect(f"{base}/health")
                if path == f"{base}/resume":
                    (config.state_dir / "PAUSED").unlink(missing_ok=True)
                    journal.append("resumed", by=user["name"])
                    return self._redirect(f"{base}/health")
                if path == f"{base}/tick":
                    if config.live or not allow_tick:
                        raise ConsoleError("running the loop from the web is only allowed on dry-run products")
                    run_tick(config, user["name"], journal)
                    return self._redirect(f"{base}/")
            except ConsoleError as exc:
                return self._send(409, "text/plain", str(exc))
            self._send(404, "text/plain", "not found")

        def _refuse(self):
            self._send(405, "text/plain", "not allowed")

        do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

        def log_message(self, fmt, *args):  # keep paths and form data out of logs
            pass

    return ThreadingHTTPServer((host, port), Handler)


def run_tick(config: Config, who: str, journal) -> None:
    """One full pass of the loop, for dry-run (fake) products only."""
    if config.live:
        raise ConsoleError("refused: not a dry-run product")
    from sayso import runtime
    from sayso.jobs import JOBS, ORDER
    ctx = runtime.build(config)
    journal.append("console_tick", by=who)
    for name in ORDER:
        try:
            JOBS[name](ctx)
        except runtime.Paused:
            pass


def serve(config: Config, host: str, port: int, secret_env: str, label: str | None = None,
          allow_tick: bool = False, vote_off_reason: str | None = None, behind_https: bool = False) -> None:
    """behind_https: served through an HTTPS tunnel on loopback; cookies get the Secure flag."""
    secret = os.environ.get(secret_env, "")
    auth = Auth(config, secret)
    if not load_json(users_path(config)):
        raise ConsoleError("no console users yet; run `sayso console add-user` first")
    srv = make_server(config, auth, host, port, label=label, allow_tick=allow_tick,
                      vote_off_reason=vote_off_reason, secure_cookie=True if behind_https else None)
    print(f"console: http://{host}:{srv.server_address[1]}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
