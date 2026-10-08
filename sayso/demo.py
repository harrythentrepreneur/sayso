"""End-to-end demo on fakes: a fake customer goes from first email to close.

Run:  python -m sayso.cli demo
It builds a throwaway product in a temp directory, uses only fake adapters,
and prints each step. The same scenario is asserted in tests/test_e2e.py.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sayso import cases, config as config_mod, runtime, votes
from sayso.jobs import JOBS, ORDER, dev, money_job

OPERATOR = "1001"

DEMO_TOML = """
[product]
name = "Demo Product"
slug = "demo"
mode = "dry-run"
support_address = "support@example.com"
labels = ["High value"]

[operators]
"1001" = "Operator One"

[state]
dir = "{state}"

[board]
adapter = "fake"
[helpdesk]
adapter = "fake"
[mailbox]
adapter = "fake"
[payments]
adapter = "fake"
[codehost]
adapter = "fake"
[drafter]
adapter = "fake"
[dev_runner]
adapter = "fake"
[qa_runner]
adapter = "fake"

[policy]
quiet_close_days = 3
readback_seconds = 1
readback_interval = 1
refund_max_minor = 5000
currencies = ["usd"]
"""


def tick(ctx, say=print) -> None:
    for name in ORDER:
        result = JOBS[name](ctx, **({"sleep": lambda s: None} if name == "sender" else {}))
        if result and any(result.values() if isinstance(result, dict) else [result]):
            say(f"    {name:9s} {result}")


def only_case(ctx) -> tuple[str, dict]:
    (key, rec), = cases.load(ctx).items()
    return key, rec


def approve_open(ctx, kind: str) -> str:
    live = [v for v in votes.load(ctx).values() if v["kind"] == kind and v["status"] == votes.OPEN]
    assert len(live) == 1, f"expected one open {kind} vote, found {len(live)}"
    ctx.world.operator_votes(live[0]["poll_id"], OPERATOR, "yes")
    return live[0]["key"]


def build(state_dir: Path) -> runtime.Context:
    path = state_dir / "demo.toml"
    path.write_text(DEMO_TOML.format(state=state_dir / "state"), encoding="utf-8")
    clock = runtime.FixedClock(datetime(2030, 1, 6, 9, 0, tzinfo=timezone.utc))
    return runtime.build(config_mod.load(path), clock)


def scenario(ctx, say=print) -> dict:
    w, board = ctx.world, ctx.board
    say("1. Customer writes in.")
    w.customer_writes(sender="Pat@Customer.test", subject="Export button does nothing",
                      body="Hi, when I press Export nothing happens. Also I was charged twice this month.")
    tick(ctx, say)
    key, rec = only_case(ctx)
    card = rec["card_id"]
    say(f"   card {card}: tags={board.get_tags(card)}")

    say("2. Support hands the bug to dev; the drafted reply vote is voided.")
    dev.request(ctx, key)
    tick(ctx, say)
    run_id = cases.load(ctx)[key]["dev_run"]
    w.data["runs"][run_id]  # exists
    ctx.dev_runner.finish(run_id, pr=42, head="a" * 40)
    tick(ctx, say)
    say(f"   tags={board.get_tags(card)}")

    say("2b. Independent QA checks the exact head: CI, a test that fails on the old code, then a QA run.")
    qa_run = cases.load(ctx)[key]["qa"]["run"]
    ctx.qa_runner.finish(qa_run, "Checked out the head and ran the export tests.\n"
                                 "CUSTOMER-FIX: YES pressing Export now downloads the file\n"
                                 f"VERDICT: PASS {'a' * 40}")
    tick(ctx, say)
    say(f"   tags={board.get_tags(card)}")

    say("3. Operator approves the refund of the duplicate charge (exact amount in the vote).")
    money_job.request(ctx, case_key=key, subject="Pat - charged twice this month",
                      spec={"action": "refund", "charge": "ch_demo0001", "amount": 1900, "currency": "usd",
                            "customer": "cus_demo0001"})
    approve_open(ctx, "money")
    tick(ctx, say)

    say("4. Operator approves the merge of the exact PR head; the loop re-drafts the reply.")
    w.data["next_draft"] = ("Hi Pat,\n\nExport is fixed in today's update. We have refunded the duplicate "
                            "charge of $19.00; it can take 5-10 days to appear.\n\nBest wishes,\nSupport")
    w.save()
    approve_open(ctx, "merge")
    tick(ctx, say)
    say(f"   PR state={ctx.codehost.pull_request(42).state}, tags={board.get_tags(card)}")

    say("5. Operator approves the exact reply; it is sent once and verified.")
    approve_open(ctx, "reply")
    tick(ctx, say)
    say(f"   tags={board.get_tags(card)}")

    say("6. Four quiet days later a close vote opens; operator approves.")
    ctx.clock.advance(days=4)
    tick(ctx, say)
    approve_open(ctx, "close")
    tick(ctx, say)
    final = board.get_tags(card)
    say(f"   final tags={final}")
    return {"case_key": key, "card": card, "final_tags": final, "outbox": w.data["outbox"],
            "refunds": w.data["refunds"], "alerts": w.data["alerts"]}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sayso-demo-") as tmp:
        ctx = build(Path(tmp))
        result = scenario(ctx)
        ok = result["final_tags"] == ["Done"] and len(result["outbox"]) == 1 and len(result["refunds"]) == 1
        print("\nDEMO", "PASS" if ok else "FAIL", f"- emails sent: {len(result['outbox'])}, refunds: "
              f"{len(result['refunds'])}, alerts: {len(result['alerts'])}")
        return 0 if ok else 1
