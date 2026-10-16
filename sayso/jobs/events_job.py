"""Events: deliver queued outgoing webhooks (sayso.adapters.events).

Runs last in a pass, so the events the other jobs queued in this pass go out at once.
Delivery problems are recorded per row and never raise; the job only beats.
"""
from __future__ import annotations


def run(ctx) -> dict:
    if getattr(ctx, "events", None) is None:
        ctx.beat("events", "off")
        return {"off": True}
    ctx.require_running()
    out = ctx.events.flush()
    ctx.beat("events", str(out))
    return out
