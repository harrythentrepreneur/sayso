"""Render systemd user timers from the settings file. Never installs anything.

`sayso timers render --config product.toml --out DIR` writes one .service and
one .timer per job. The operator reviews them, copies them to
~/.config/systemd/user/ and enables them by hand (docs/INSTALL.md). The loop
has no code path that starts, stops or enables a service.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

from sayso.config import Config

SERVICE = """[Unit]
Description=sayso {slug} {job}

[Service]
Type=oneshot
WorkingDirectory={workdir}
EnvironmentFile=-{envfile}
ExecStart={python} -m sayso.cli run {job} --config {config}
TimeoutStartSec={timeout}
"""

TIMER = """[Unit]
Description=sayso {slug} {job} every {every}s

[Timer]
OnBootSec=90
OnUnitInactiveSec={every}
AccuracySec=5
Persistent=true

[Install]
WantedBy=timers.target
"""


def unit_name(config: Config, job: str) -> str:
    return f"sayso-{config.slug}-{job}"


def render(config: Config, *, python: str | None = None, envfile: str | None = None) -> dict[str, str]:
    python = python or sys.executable
    envfile = envfile or str(Path.home() / ".config" / "sayso" / f"{config.slug}.env")
    workdir = str(Path(__file__).resolve().parents[1])
    files = {}
    for job, every in sorted(config.jobs.items()):
        name = unit_name(config, job)
        files[name + ".service"] = SERVICE.format(slug=config.slug, job=job, workdir=workdir, envfile=envfile,
                                                  python=shlex.quote(python), config=shlex.quote(str(config.path)),
                                                  timeout=max(every * 2, 300))
        files[name + ".timer"] = TIMER.format(slug=config.slug, job=job, every=every)
    return files


def write(files: dict[str, str], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in files.items():
        path = out / name
        path.write_text(body, encoding="utf-8")
        written.append(path)
    return written
