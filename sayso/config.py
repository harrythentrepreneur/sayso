"""One settings file per product, validated before anything runs.

The file is TOML. Unknown keys are refused (a typo must not silently fall back
to a default). Real adapters need BOTH ``mode = "live"`` and their secrets in
the environment; secrets are named by environment variable, never stored in
the file.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODES = ("dry-run", "live")
ADAPTER_CHOICES = {
    "board": ("fake", "discord"),
    "helpdesk": ("fake", "frappe"),
    "mailbox": ("fake", "imap"),
    "payments": ("none", "fake", "stripe"),
    "codehost": ("none", "fake", "github"),
    "drafter": ("fake", "hermes"),
    "dev_runner": ("none", "fake", "hermes"),
    "qa_runner": ("none", "fake", "hermes"),
}
DEFAULT_JOBS = {
    "intake": 60,
    "draft": 120,
    "sender": 120,
    "money": 120,
    "votes": 60,
    "dev": 300,
    "qa": 300,
    "stall": 300,
    "release": 300,
    "close": 900,
    "reconcile": 600,
    "health": 600,
}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigError(ValueError):
    """The settings file is wrong. The loop refuses to start."""


@dataclass(frozen=True)
class Section:
    adapter: str
    options: dict[str, Any] = field(default_factory=dict)

    def secret(self, key: str) -> str:
        """Read a secret named by ``<key>_env``. Missing is an error, never empty."""
        name = self.options.get(f"{key}_env")
        if not name:
            raise ConfigError(f"{self.adapter}: '{key}_env' is not set")
        value = os.environ.get(name, "")
        if not value:
            raise ConfigError(f"{self.adapter}: environment variable {name} is empty")
        return value


SMALL_FIX_SENSITIVE = (
    r"(?i)(stripe|billing|payment|checkout|subscri|pric|refund|invoice|coupon|discount|"
    r"webhook|auth|login|logout|signin|signup|session|password|token|oauth|middleware|"
    r"account|secret|credential|\.env|migration|schema\.prisma|/sql/|\.sql$|"
    r"\.github/|deploy|docker|terraform|\.tf$|k8s|helm)")


def validate_small_fix(policy, *, qa_runner: str) -> None:
    """The small-fix release policy fails closed: on needs QA and a usable sensitive-path rule."""
    if not isinstance(policy.small_fix_release, bool):
        raise ConfigError("[policy] small_fix_release must be true or false")
    for name in ("small_fix_max_lines", "small_fix_max_files"):
        value = getattr(policy, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"[policy] {name} must be a whole number >= 1")
    if not policy.small_fix_release:
        return
    if qa_runner == "none":
        raise ConfigError("[policy] small_fix_release needs a [qa_runner]: only a QA pass may stand in for a vote")
    if not str(policy.small_fix_sensitive_paths).strip():
        raise ConfigError("[policy] small_fix_sensitive_paths cannot be empty while small_fix_release is on")
    try:
        re.compile(policy.small_fix_sensitive_paths)
    except re.error as exc:
        raise ConfigError(f"[policy] small_fix_sensitive_paths is not a valid regular expression: {exc}") from exc


@dataclass(frozen=True)
class Policy:
    quiet_close_days: int = 3
    readback_seconds: float = 120.0
    readback_interval: float = 10.0
    unsent_alert_hours: int = 24
    vote_expiry_hours: int = 72
    max_new_cases_per_run: int = 40
    refund_max_minor: int = 0
    currencies: tuple[str, ...] = ()
    max_dev_rounds: int = 2
    max_parallel_dev_runs: int = 2
    vote_remind_minutes: int = 30
    orphan_minutes: int = 15
    stall_new_hours: int = 1
    stall_support_hours: int = 2
    stall_dev_hours: int = 2
    stall_qa_hours: int = 1
    stall_awaiting_hours: int = 2
    stall_blocked_hours: int = 4
    stall_repeat_hours: int = 24
    parked_label: str = "Parked"
    red_proof_max_tries: int = 3
    qa_limit_retries: int = 6
    qa_ui_paths: str = ""
    small_fix_release: bool = False
    small_fix_max_lines: int = 400
    small_fix_max_files: int = 15
    small_fix_sensitive_paths: str = SMALL_FIX_SENSITIVE


@dataclass(frozen=True)
class Plugin:
    name: str
    enabled: bool
    options: dict[str, Any]


@dataclass(frozen=True)
class Config:
    path: Path
    slug: str
    name: str
    mode: str
    support_address: str
    own_addresses: frozenset[str]
    operators: dict[str, str]
    state_dir: Path
    labels: tuple[str, ...]
    sections: dict[str, Section]
    policy: Policy
    jobs: dict[str, int]
    plugins: tuple[Plugin, ...]

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def section(self, name: str) -> Section:
        return self.sections[name]


def _take(table: dict, allowed: set[str], where: str) -> dict:
    if not isinstance(table, dict):
        raise ConfigError(f"[{where}] must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"[{where}] unknown keys: {sorted(unknown)}")
    return table


def _need(table: dict, key: str, where: str) -> Any:
    if key not in table or table[key] in ("", None):
        raise ConfigError(f"[{where}] '{key}' is required")
    return table[key]


def parse(raw: dict, path: Path) -> Config:
    top = _take(raw, {"product", "operators", "state", "board", "helpdesk", "mailbox", "payments",
                      "codehost", "drafter", "dev_runner", "qa_runner", "policy", "jobs", "plugins"}, "top level")
    product = _take(_need(top, "product", "top level"),
                    {"name", "slug", "mode", "support_address", "own_addresses", "labels"}, "product")
    slug = _need(product, "slug", "product")
    if not _SLUG.match(str(slug)):
        raise ConfigError("[product] slug must be lower-case letters, digits and dashes")
    mode = product.get("mode", "dry-run")
    if mode not in MODES:
        raise ConfigError(f"[product] mode must be one of {MODES}")
    support = str(_need(product, "support_address", "product")).lower()
    if not _EMAIL.match(support):
        raise ConfigError("[product] support_address is not an email address")
    own = {support} | {str(a).lower() for a in product.get("own_addresses", [])}
    for addr in own:
        if not _EMAIL.match(addr):
            raise ConfigError(f"[product] own_addresses has a bad address: {addr!r}")

    operators = _need(top, "operators", "top level")
    if not isinstance(operators, dict) or not operators:
        raise ConfigError("[operators] needs at least one approver: <board user id> = <name>")
    for uid, who in operators.items():
        if not str(uid).strip() or not str(who).strip():
            raise ConfigError("[operators] ids and names must be non-empty")

    state = _take(_need(top, "state", "top level"), {"dir"}, "state")
    state_dir = Path(str(_need(state, "dir", "state"))).expanduser()
    if not state_dir.is_absolute():
        state_dir = (path.parent / state_dir).resolve()

    sections: dict[str, Section] = {}
    for name, choices in ADAPTER_CHOICES.items():
        table = dict(top.get(name) or {"adapter": choices[0]})
        adapter = table.pop("adapter", choices[0])
        if adapter not in choices:
            raise ConfigError(f"[{name}] adapter must be one of {choices}")
        for key, value in table.items():
            if key.endswith("_env") and not _ENV.match(str(value)):
                raise ConfigError(f"[{name}] {key} must name an environment variable, not hold a secret")
        if adapter not in ("fake", "none") and mode != "live":
            raise ConfigError(f"[{name}] real adapter {adapter!r} needs [product] mode = \"live\"")
        sections[name] = Section(adapter, table)

    pol = _take(top.get("policy", {}), set(Policy.__dataclass_fields__), "policy")
    pol = dict(pol)
    if "currencies" in pol:
        pol["currencies"] = tuple(str(c).lower() for c in pol["currencies"])
    policy = Policy(**pol)  # type: ignore[arg-type]
    if policy.quiet_close_days < 1 or policy.readback_seconds <= 0 or policy.max_new_cases_per_run < 1:
        raise ConfigError("[policy] values must be positive")
    if min(policy.max_dev_rounds, policy.max_parallel_dev_runs, policy.red_proof_max_tries,
           policy.qa_limit_retries) < 1:
        raise ConfigError("[policy] max_dev_rounds, max_parallel_dev_runs, red_proof_max_tries "
                          "and qa_limit_retries must be >= 1")
    stall = [policy.vote_remind_minutes, policy.orphan_minutes, policy.stall_new_hours,
             policy.stall_support_hours, policy.stall_dev_hours, policy.stall_qa_hours,
             policy.stall_awaiting_hours, policy.stall_blocked_hours, policy.stall_repeat_hours]
    if any(not isinstance(v, int) or v < 1 for v in stall):
        raise ConfigError("[policy] stall and reminder times must be whole numbers >= 1")
    if policy.qa_ui_paths:
        try:
            re.compile(policy.qa_ui_paths)
        except re.error as exc:
            raise ConfigError(f"[policy] qa_ui_paths is not a valid regular expression: {exc}") from exc
    validate_small_fix(policy, qa_runner=sections["qa_runner"].adapter)
    if sections["qa_runner"].adapter != "none" and sections["codehost"].adapter == "none":
        raise ConfigError("[qa_runner] needs a [codehost]: QA reads the PR it checks")
    if policy.refund_max_minor < 0:
        raise ConfigError("[policy] refund_max_minor cannot be negative")
    if sections["payments"].adapter != "none" and (policy.refund_max_minor == 0 or not policy.currencies):
        raise ConfigError("[policy] payments are on: set refund_max_minor and currencies")

    jobs = dict(DEFAULT_JOBS)
    for job, seconds in _take(top.get("jobs", {}), set(DEFAULT_JOBS), "jobs").items():
        if not isinstance(seconds, int) or seconds < 30:
            raise ConfigError(f"[jobs] {job} must be a whole number of seconds >= 30")
        jobs[job] = seconds

    plugins = []
    for i, item in enumerate(top.get("plugins", [])):
        item = _take(item, {"name", "enabled", "options"}, f"plugins #{i}")
        plugins.append(Plugin(str(_need(item, "name", "plugins")), bool(item.get("enabled", False)),
                              dict(item.get("options", {}))))

    labels = tuple(str(x) for x in product.get("labels", []))
    from sayso.stages import SYSTEM_TAGS
    clash = set(labels) & set(SYSTEM_TAGS)
    if clash:
        raise ConfigError(f"[product] labels reuse stage names: {sorted(clash)}")

    return Config(path=path, slug=slug, name=str(product.get("name", slug)), mode=mode,
                  support_address=support, own_addresses=frozenset(own),
                  operators={str(k): str(v) for k, v in operators.items()}, state_dir=state_dir,
                  labels=labels, sections=sections, policy=policy, jobs=jobs, plugins=tuple(plugins))


def load(path: str | Path) -> Config:
    p = Path(path).resolve()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    return parse(raw, p)
