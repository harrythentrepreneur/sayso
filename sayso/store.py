"""Durable state: atomic JSON files, cross-process locks and an append-only journal.

Every writer takes the lock for its whole read/modify/write window. An atomic
rename alone does not stop a stale snapshot from overwriting a newer edit.
A corrupt file raises instead of being treated as empty: an empty ledger would
let the loop redo work it has already done (a second email, a second refund).
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class LockTimeout(RuntimeError):
    """A ledger was busy. No state change was attempted."""


class CorruptState(RuntimeError):
    """A state file exists but cannot be read. The loop stops rather than guess."""


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime refused; pass an aware UTC time")
    return value.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def load_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return {} if default is None else default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorruptState(f"{p}: {exc}") from exc


def save_json(path: str | Path, value: Any) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def locked(*paths: str | Path | None, timeout: float = 30) -> Iterator[None]:
    """Lock every named file in canonical order before any of them is read."""
    names = sorted({str(Path(p).resolve()) + ".lock" for p in paths if p})
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        for name in names:
            lock_path = Path(name)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(lock_path.open("a+"))
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"{lock_path} busy; no action taken") from None
                    time.sleep(0.05)
            stack.callback(fcntl.flock, stream.fileno(), fcntl.LOCK_UN)
        yield


@contextmanager
def single_flight(path: str | Path) -> Iterator[bool]:
    """Yield True if this process holds the job lock, False if another run holds it."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Journal:
    """Append-only JSON lines. One line per state change, never rewritten."""

    def __init__(self, path: str | Path, clock):
        self.path = Path(path)
        self.clock = clock

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        row = {"at": utc_iso(self.clock.now()), "event": event, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return row

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
