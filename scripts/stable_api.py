#!/usr/bin/env python3
"""The stable API: every sayso name a downstream host (Sayso Cloud) builds on.

`sayso/stable_api.json` lists each name with its shape (call signature, public
methods, or a small constant value). `tests/test_stable_api.py` fails when any
listed name disappears or changes shape, so a change that would break a host is
caught in THIS repo, before it merges.

    python scripts/stable_api.py            # check, exit 1 on drift
    python scripts/stable_api.py --write    # re-record after a deliberate change
    python scripts/stable_api.py --add sayso.store.locked [...]   # list a new name

Changing a listed name is allowed, but deliberate: re-record with --write, say so
in the PR, and expect Sayso Cloud's compat check to tell you whether it still works.
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "sayso" / "stable_api.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))   # check THIS checkout, not an installed copy


def _resolve(dotted: str):
    """Import the longest module prefix, then walk attributes."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(dotted)


def _sig(obj):
    """Parameters as [name, kind, has_default]. Annotations are left out on purpose:
    they are documentation, and a re-spelt hint must not read as a break."""
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return "?"
    return [[p.name, p.kind.name, p.default is not inspect.Parameter.empty] for p in sig.parameters.values()]


_POSITIONAL = {"POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD"}


def sig_compatible(was, now) -> bool:
    """A caller written against `was` still works against `now`.
    Allowed: new parameters WITH defaults, new *args/**kwargs, a required param gaining a default.
    Refused: a removed or renamed parameter, a reorder of positional ones, a new required one."""
    if was == "?" or now == "?":
        return was == now
    var = ("VAR_POSITIONAL", "VAR_KEYWORD")
    for kind in var:                    # *args / **kwargs: a caller never names them
        if any(p[1] == kind for p in was) and not any(p[1] == kind for p in now):
            return False
    was = [p for p in was if p[1] not in var]
    now = [p for p in now if p[1] not in var]
    old = {p[0]: p for p in was}
    new = {p[0]: p for p in now}
    for name, (_, kind, had_default) in old.items():
        if name not in new:
            return False
        if new[name][1] != kind or (had_default and not new[name][2]):
            return False
    old_pos = [p[0] for p in was if p[1] in _POSITIONAL]
    new_pos = [p[0] for p in now if p[1] in _POSITIONAL]
    if new_pos[:len(old_pos)] != old_pos:
        return False
    return all(p[2] for p in now if p[0] not in old)


def shape(obj) -> dict:
    """What a host relies on: how to call it, what it has, or its value."""
    if inspect.ismodule(obj):
        return {"kind": "module"}
    if inspect.isclass(obj) and issubclass(obj, BaseException):
        return {"kind": "exception", "bases": [b.__module__ + "." + b.__qualname__ for b in obj.__mro__[1:]]}
    if inspect.isclass(obj):
        methods = {}
        for name, member in sorted(vars(obj).items()):
            if name.startswith("_") and name != "__init__":
                continue
            if callable(member) or isinstance(member, (staticmethod, classmethod, property)):
                fn = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
                methods[name] = "property" if isinstance(member, property) else _sig(fn)
        return {"kind": "class", "init": _sig(obj), "members": methods}
    if callable(obj):
        return {"kind": "function", "signature": _sig(obj)}
    if isinstance(obj, (bool, int, float)) or (
            isinstance(obj, (list, tuple, frozenset, set)) and all(isinstance(x, (str, int)) for x in obj)):
        value = sorted(obj) if isinstance(obj, (set, frozenset)) else list(obj) if isinstance(obj, (list, tuple)) else obj
        return {"kind": "value", "type": type(obj).__name__, "value": value}
    if isinstance(obj, str) and len(obj) <= 64:        # vote states, stage names: hosts compare against them
        return {"kind": "value", "type": "str", "value": obj}
    if isinstance(obj, dict):                           # a host looks entries up by key
        return {"kind": "value", "type": "dict", "keys": sorted(map(str, obj))}
    return {"kind": "value", "type": type(obj).__name__}


def current(names) -> dict:
    out = {}
    for name in sorted(names):
        try:
            out[name] = shape(_resolve(name))
        except (ImportError, AttributeError) as e:
            out[name] = {"kind": "missing", "error": f"{type(e).__name__}: {e}"}
    return out


def load() -> dict:
    return json.loads(API.read_text())


def compatible(was: dict, now: dict) -> bool:
    """True when `now` still offers everything `was` promised. Additions are fine
    (a new method, a new dict key); removals and changed signatures are not."""
    if now.get("kind") != was.get("kind"):
        return False
    if was["kind"] == "class":
        def member_ok(k, v):
            got = now["members"].get(k)
            if v == "property" or got == "property":
                return got == v
            return got is not None and sig_compatible(v, got)
        return sig_compatible(was["init"], now["init"]) and all(member_ok(k, v) for k, v in was["members"].items())
    if was["kind"] == "function":
        return sig_compatible(was["signature"], now["signature"])
    if was["kind"] == "value" and "keys" in was:
        return now.get("type") == was["type"] and set(was["keys"]) <= set(now.get("keys", ()))
    if was["kind"] == "exception":
        return set(was["bases"]) <= set(now["bases"])
    return now == was


def drift(recorded: dict) -> list[str]:
    now = current(recorded["names"])
    problems = []
    for name, was in recorded["names"].items():
        got = now[name]
        if got.get("kind") == "missing":
            problems.append(f"{name}: REMOVED ({got['error']})")
        elif not compatible(was, got):
            problems.append(f"{name}: shape changed\n    was {json.dumps(was, sort_keys=True)}\n    now {json.dumps(got, sort_keys=True)}")
    return problems


def write(names) -> None:
    data = {
        "about": "Names a downstream host builds on. Changing one is a breaking change: "
                 "re-record with `python scripts/stable_api.py --write` and say so in the PR.",
        "names": current(names),
    }
    missing = [n for n, s in data["names"].items() if s.get("kind") == "missing"]
    if missing:
        sys.exit(f"cannot record names that do not exist: {missing}")
    API.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def main(argv: list[str]) -> int:
    if argv[:1] == ["--write"]:
        write(load()["names"])
        print(f"recorded {len(load()['names'])} names in {API.relative_to(ROOT)}")
        return 0
    if argv[:1] == ["--add"]:
        names = set(load()["names"]) if API.exists() else set()
        write(names | set(argv[1:]))
        print(f"recorded {len(load()['names'])} names in {API.relative_to(ROOT)}")
        return 0
    problems = drift(load())
    for p in problems:
        print(p)
    print(f"STABLE API: {'DRIFT ' + str(len(problems)) if problems else 'OK'} ({len(load()['names'])} names)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
