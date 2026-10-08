"""Plugins: product-only logic that the shared loop does not need.

A plugin is a module in ``sayso.plugins`` (or any importable module
named ``package.module``) that exposes ``make(options) -> object``. The object
may implement any of these hooks; missing hooks are skipped:

  classify(ctx, message) -> {"kind": str, "labels": [str]} | None
      Claim an inbound message before the default correspondence path.
  check_reply(ctx, case, text) -> str | None
      Refuse a reply draft by returning a reason. Called before a vote opens
      and again before the send.
  extra_jobs() -> {name: callable(ctx) -> dict}
      Extra timer jobs this plugin needs.

Plugins are OFF unless the settings file lists them with ``enabled = true``.
"""
from __future__ import annotations

import importlib

from sayso.config import Config, ConfigError


def load_enabled(config: Config) -> list:
    loaded = []
    for plugin in config.plugins:
        if not plugin.enabled:
            continue
        name = plugin.name if "." in plugin.name else f"sayso.plugins.{plugin.name}"
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            raise ConfigError(f"plugin {plugin.name!r} cannot be imported: {exc}") from exc
        if not hasattr(module, "make"):
            raise ConfigError(f"plugin {plugin.name!r} has no make(options)")
        loaded.append(module.make(plugin.options))
    return loaded
