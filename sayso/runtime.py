"""The runtime context every job receives: config, adapters, clock, state paths.

Jobs never build adapters themselves. That keeps one place where a product's
settings turn into live connections, and one place tests replace with fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sayso.config import Config, ConfigError
from sayso.store import Journal, load_json, locked, save_json


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """A clock tests and the demo can move forward by hand."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        from datetime import timedelta
        self._now = self._now + timedelta(**delta)


class Paused(RuntimeError):
    """The operator paused the loop. Mutating jobs do nothing."""


@dataclass
class Paths:
    root: Path

    @property
    def cases(self) -> Path: return self.root / "cases.json"
    @property
    def votes(self) -> Path: return self.root / "votes.json"
    @property
    def attempts(self) -> Path: return self.root / "attempts.json"
    @property
    def cursors(self) -> Path: return self.root / "cursors.json"
    @property
    def heartbeats(self) -> Path: return self.root / "heartbeats.json"
    @property
    def journal(self) -> Path: return self.root / "journal.jsonl"
    @property
    def drafts(self) -> Path: return self.root / "drafts"
    @property
    def receipts(self) -> Path: return self.root / "receipts"
    @property
    def pause_flag(self) -> Path: return self.root / "PAUSED"
    @property
    def fake_world(self) -> Path: return self.root / "fake-world.json"
    @property
    def locks(self) -> Path: return self.root / "locks"


@dataclass
class Context:
    config: Config
    clock: Clock
    board: Any
    helpdesk: Any
    mailbox: Any
    payments: Any = None
    codehost: Any = None
    drafter: Any = None
    dev_runner: Any = None
    qa_runner: Any = None
    world: Any = None  # the shared fake world, when fakes are in use
    plugins: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.paths = Paths(self.config.state_dir)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.paths.journal, self.clock)

    # -- pause -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.paths.pause_flag.exists()

    def require_running(self) -> None:
        if self.paused:
            raise Paused(f"{self.config.slug} is paused ({self.paths.pause_flag}); no action taken")

    # -- alerts ------------------------------------------------------------
    def alert(self, key: str, text: str) -> None:
        """Alert once per key. Repeats of the same problem stay quiet."""
        self.board.alert(f"[{self.config.name}] {text}", idempotency_key=key)
        self.journal.append("alert", key=key, text=text)

    # -- heartbeats ----------------------------------------------------------
    def beat(self, job: str, result: str) -> None:
        with locked(self.paths.heartbeats):
            beats = load_json(self.paths.heartbeats)
            beats[job] = {"at": self.clock.now().isoformat(), "result": result}
            save_json(self.paths.heartbeats, beats)


def build(config: Config, clock: Clock | None = None) -> Context:
    """Turn a validated config into live or fake adapters."""
    from sayso import plugins as plugin_registry
    from sayso.adapters import fake

    clock = clock or Clock()
    state_dir = config.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    needs_world = any(s.adapter == "fake" for s in config.sections.values())
    world = fake.World(state_dir / "fake-world.json", clock) if needs_world else None

    def make(name: str):
        section = config.section(name)
        kind = section.adapter
        if kind == "none":
            return None
        if kind == "fake":
            return {
                "board": lambda: fake.FakeBoard(world),
                "helpdesk": lambda: fake.FakeHelpdesk(world, config.support_address),
                "mailbox": lambda: fake.FakeMailbox(world),
                "payments": lambda: fake.FakePayments(world),
                "codehost": lambda: fake.FakeCodeHost(world),
                "drafter": lambda: fake.FakeDrafter(world),
                "dev_runner": lambda: fake.FakeDevRunner(world),
                "qa_runner": lambda: fake.FakeQaRunner(world),
            }[name]()
        if not config.live:
            raise ConfigError(f"{name}: real adapter needs live mode")
        from sayso.adapters import reference
        return reference.build(name, kind, section, config)

    ctx = Context(config=config, clock=clock, board=make("board"), helpdesk=make("helpdesk"),
                  mailbox=make("mailbox"), payments=make("payments"), codehost=make("codehost"),
                  drafter=make("drafter"), dev_runner=make("dev_runner"), qa_runner=make("qa_runner"), world=world)
    ctx.plugins = plugin_registry.load_enabled(config)
    return ctx
