"""Loop jobs. Each is one timer-driven, idempotent pass."""
from sayso.jobs import close, dev, draft, health, intake, money_job, qa, reconcile, release, sender, stall, tally

JOBS = {
    "intake": intake.run,
    "draft": draft.run,
    "votes": tally.run,
    "sender": sender.run,
    "money": money_job.run,
    "dev": dev.run,
    "qa": qa.run,
    "release": release.run,
    "close": close.run,
    "reconcile": reconcile.run,
    "stall": stall.run,
    "health": health.run,
}

# One full pass in dependency order (used by `sayso tick` and the demo).
ORDER = ("intake", "votes", "release", "money", "sender", "dev", "qa", "draft", "close", "reconcile", "stall", "health")
