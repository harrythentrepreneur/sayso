"""Health: is every job still beating? A silent job is a broken job.

A job is stale when its last heartbeat is older than three of its intervals.
Paused loops report as paused, not healthy.
"""
from __future__ import annotations

from datetime import timedelta

from sayso.store import load_json, parse_iso

ACTIVE_JOBS = ("intake", "draft", "votes", "sender", "close", "reconcile")


def check(ctx) -> dict:
    beats = load_json(ctx.paths.heartbeats)
    now = ctx.clock.now()
    report = {"paused": ctx.paused, "stale": [], "never": [], "ok": []}
    for job in ACTIVE_JOBS:
        beat = beats.get(job)
        if not beat:
            report["never"].append(job)
            continue
        if now - parse_iso(beat["at"]) > timedelta(seconds=3 * ctx.config.jobs[job]):
            report["stale"].append(job)
        else:
            report["ok"].append(job)
    return report


def run(ctx) -> dict:
    report = check(ctx)
    if report["stale"] and not report["paused"]:
        ctx.alert("stale:" + ",".join(report["stale"]), f"jobs not running: {', '.join(report['stale'])}")
    ctx.beat("health", str(report))
    return report
