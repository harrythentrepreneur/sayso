"""Tiny JSON-over-HTTP client with an injectable transport, for reference adapters.

Tests replace ``transport`` with a recorder, so every reference adapter's
request shape is checked without a network or an account.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class Client:
    def __init__(self, base: str, headers: dict[str, str], transport: Transport | None = None,
                 sleep=time.sleep, max_retries: int = 3):
        self.base = base.rstrip("/")
        self.headers = {"User-Agent": "sayso/0.2", **headers}
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.max_retries = max_retries

    def request(self, method: str, path: str, *, json_body: Any = None, form: dict | None = None,
                query: dict | None = None, extra_headers: dict[str, str] | None = None) -> Any:
        url = self.base + path + (("?" + urllib.parse.urlencode(query, doseq=True)) if query else "")
        headers = dict(self.headers, **(extra_headers or {}))
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(self.max_retries + 1):
            status, resp_headers, raw = self.transport(method, url, headers, body)
            if status == 429 and attempt < self.max_retries:
                try:
                    wait = float(json.loads(raw or b"{}").get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                self.sleep(min(wait, 30))
                continue
            if status >= 400:
                raise HttpError(status, raw.decode("utf-8", "replace"))
            return json.loads(raw) if raw else None
        raise HttpError(429, "rate limited")
