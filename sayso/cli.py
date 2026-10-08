"""Command line: `sayso <command>` or `python -m sayso.cli <command>`.

  validate --config F            check a settings file; exit 1 on any problem
  run JOB --config F             one pass of one job (what each timer calls)
  tick --config F                one pass of every job in order
  status --config F              stages, open votes, heartbeats, pause state
  pause|resume --config F        stop/start all mutating jobs (reads still run)
  timers render --config F --out DIR   write systemd units for review; installs nothing
  demo                           run the fake end-to-end scenario
  cases --config F               list cases with their live stage
  handoff-dev CASE --config F    send a case to dev (voids its pending reply vote)
  set-owner CASE --owner WHO --by WHO --config F
                                 whose decision this case's votes are (a label; anyone can still vote)
  money-request CASE --config F --spec FILE --subject TEXT
                                 open a money vote for an exact refund/cancel spec
  fake write-in --config F --from ADDR --subject S --body B   (fake adapters only)
  fake vote --config F --key VOTE --user ID --answer yes|no    (fake adapters only)
  dashboard --config F [--config G] | --demo   read-only web view on 127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from sayso import config as config_mod
from sayso import runtime, stages, systemd, votes
from sayso.store import load_json, single_flight


def _ctx(path: str) -> runtime.Context:
    return runtime.build(config_mod.load(path))


def cmd_validate(a) -> int:
    cfg = config_mod.load(a.config)
    on = {k: s.adapter for k, s in cfg.sections.items()}
    print(json.dumps({"ok": True, "product": cfg.slug, "mode": cfg.mode, "adapters": on,
                      "plugins": [p.name for p in cfg.plugins if p.enabled]}, indent=1))
    return 0


def cmd_run(a) -> int:
    from sayso.jobs import JOBS
    ctx = _ctx(a.config)
    extra = {}
    for plugin in ctx.plugins:
        if hasattr(plugin, "extra_jobs"):
            extra.update(plugin.extra_jobs())
    jobs = {**JOBS, **extra}
    if a.job not in jobs:
        print(f"unknown job {a.job}; known: {sorted(jobs)}", file=sys.stderr)
        return 2
    with single_flight(ctx.paths.locks / f"{a.job}.run") as mine:
        if not mine:
            print(f"{a.job}: another run holds the lock; skipped")
            return 0
        try:
            print(json.dumps(jobs[a.job](ctx), default=str))
        except runtime.Paused as exc:
            print(str(exc))
    return 0


def cmd_tick(a) -> int:
    from sayso.jobs import JOBS, ORDER
    ctx = _ctx(a.config)
    for name in ORDER:
        try:
            print(name, json.dumps(JOBS[name](ctx), default=str))
        except runtime.Paused as exc:
            print(name, "paused:", exc)
    return 0


def cmd_status(a) -> int:
    ctx = _ctx(a.config)
    stage_count: Counter = Counter()
    for rec in load_json(ctx.paths.cases).values():
        try:
            stage_count[stages.stage_of(ctx.board.get_tags(rec["card_id"]))] += 1
        except Exception:  # noqa: BLE001
            stage_count["unreadable"] += 1
    open_votes = [v["key"] for v in votes.load(ctx).values() if v["status"] in votes.LIVE]
    print(json.dumps({"paused": ctx.paused, "stages": stage_count, "live_votes": open_votes,
                      "heartbeats": load_json(ctx.paths.heartbeats)}, indent=1, default=str))
    return 0


def cmd_pause(a) -> int:
    ctx = _ctx(a.config)
    ctx.paths.pause_flag.write_text(a.reason or "paused by operator\n", encoding="utf-8")
    ctx.journal.append("paused", reason=a.reason)
    print(f"paused: {ctx.paths.pause_flag}")
    return 0


def cmd_resume(a) -> int:
    ctx = _ctx(a.config)
    ctx.paths.pause_flag.unlink(missing_ok=True)
    ctx.journal.append("resumed")
    print("resumed")
    return 0


def cmd_timers(a) -> int:
    cfg = config_mod.load(a.config)
    files = systemd.render(cfg, python=a.python)
    for path in systemd.write(files, Path(a.out)):
        print(path)
    print("Review these files, then follow docs/INSTALL.md to install them. Nothing was installed.")
    return 0


def cmd_demo(a) -> int:
    from sayso import demo
    return demo.main()


def cmd_cases(a) -> int:
    ctx = _ctx(a.config)
    for key, rec in sorted(load_json(ctx.paths.cases).items()):
        try:
            tags = ctx.board.get_tags(rec["card_id"])
        except Exception:  # noqa: BLE001
            tags = ["unreadable"]
        print(f"{key}\t{rec['customer']}\t{rec['title']}\t{', '.join(tags)}")
    return 0


def cmd_handoff_dev(a) -> int:
    from sayso.jobs import dev
    ctx = _ctx(a.config)
    ctx.require_running()
    dev.request(ctx, a.case)
    print(f"{a.case}: handed to dev")
    return 0


def cmd_set_owner(a) -> int:
    from sayso import owners
    ctx = _ctx(a.config)
    try:
        value = owners.set_override(ctx, a.case, a.owner, a.by)
    except owners.OwnerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"case_key": a.case, **value, "name": owners.name_of(ctx, value["owner"]) or "any operator"}))
    return 0


def cmd_money_request(a) -> int:
    from sayso.jobs import money_job
    ctx = _ctx(a.config)
    ctx.require_running()
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    rec = money_job.request(ctx, case_key=a.case, spec=spec, subject=a.subject)
    print(f"money vote opened: {rec['key']} ({rec['status']})")
    return 0


def cmd_dashboard(a) -> int:
    """Read-only web view. Never builds the runtime, so it never writes state."""
    import tempfile
    from sayso import dashboard
    dashboard.check_host(a.host)
    if a.demo == bool(a.config):
        print("give --demo or at least one --config", file=sys.stderr)
        return 2
    if a.demo:
        with tempfile.TemporaryDirectory(prefix="sayso-dash-demo-") as tmp:
            dashboard.serve([dashboard.build_demo_state(Path(tmp))], a.host, a.port)
        return 0
    configs = [config_mod.load(p) for p in a.config]
    if len({c.slug for c in configs}) != len(configs):
        print("two settings files share a slug", file=sys.stderr)
        return 2
    readers = {c.slug: dashboard.board_reader_for(c) for c in configs}
    dashboard.serve(configs, a.host, a.port, readers)
    return 0


def _fake_ctx(path):
    ctx = _ctx(path)
    if ctx.world is None or ctx.config.live:
        print("fake commands need fake adapters in dry-run mode", file=sys.stderr)
        raise SystemExit(2)
    return ctx


def cmd_fake_write_in(a) -> int:
    ctx = _fake_ctx(a.config)
    msg = ctx.world.customer_writes(sender=a.sender, subject=a.subject, body=a.body,
                                    to=ctx.config.support_address)
    print(f"fake email {msg.message_id} on ticket {msg.ticket}")
    return 0


def cmd_fake_vote(a) -> int:
    ctx = _fake_ctx(a.config)
    rec = votes.load(ctx)[a.key]
    ctx.world.operator_votes(rec["poll_id"], a.user, a.answer)
    print(f"{a.user} voted {a.answer} on {a.key}")
    return 0


def cmd_console_add_user(a) -> int:
    import getpass
    import os
    from sayso import console
    cfg = config_mod.load(a.config)
    password = os.environ.get(a.password_env, "") if a.password_env else getpass.getpass("New password: ")
    user = console.add_user(cfg, a.name, a.operator, password)
    print(f"console user {user['name']} -> operator {user['operator_id']} saved in {console.users_path(cfg)}")
    return 0


def cmd_console_invite(a) -> int:
    from sayso import console
    cfg = config_mod.load(a.config)
    token = console.make_invite(cfg, a.name, a.operator, a.minutes)
    print(f"{a.base_url.rstrip('/')}/invite/{token}")
    return 0


def cmd_console_serve(a) -> int:
    from sayso import console
    console.serve(config_mod.load(a.config), a.host, a.port, a.secret_env, label=a.label,
                  allow_tick=a.allow_tick, vote_off_reason=a.votes_off, behind_https=a.behind_https)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sayso", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("validate", cmd_validate), ("tick", cmd_tick), ("status", cmd_status),
                     ("resume", cmd_resume)):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
        s.set_defaults(fn=fn)
    s = sub.add_parser("run")
    s.add_argument("job")
    s.add_argument("--config", required=True)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("pause")
    s.add_argument("--config", required=True)
    s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_pause)
    t = sub.add_parser("timers").add_subparsers(dest="action", required=True).add_parser("render")
    t.add_argument("--config", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--python", default=None)
    t.set_defaults(fn=cmd_timers)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    s = sub.add_parser("cases")
    s.add_argument("--config", required=True)
    s.set_defaults(fn=cmd_cases)
    s = sub.add_parser("handoff-dev")
    s.add_argument("case")
    s.add_argument("--config", required=True)
    s.set_defaults(fn=cmd_handoff_dev)
    s = sub.add_parser("set-owner")
    s.add_argument("case")
    s.add_argument("--owner", required=True, help="operator id or name, or 'all'")
    s.add_argument("--by", required=True, help="the operator who asked for this owner")
    s.add_argument("--config", required=True)
    s.set_defaults(fn=cmd_set_owner)
    s = sub.add_parser("money-request")
    s.add_argument("case")
    s.add_argument("--config", required=True)
    s.add_argument("--spec", required=True)
    s.add_argument("--subject", required=True)
    s.set_defaults(fn=cmd_money_request)
    s = sub.add_parser("dashboard")
    s.add_argument("--config", action="append", default=[])
    s.add_argument("--demo", action="store_true", help="serve fake demo data")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_dashboard)
    co = sub.add_parser("console").add_subparsers(dest="action", required=True)
    s = co.add_parser("add-user")
    s.add_argument("--config", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--operator", required=True, help="an id from [operators]")
    s.add_argument("--password-env", default=None, help="read the password from this env var (else prompt)")
    s.set_defaults(fn=cmd_console_add_user)
    s = co.add_parser("invite")
    s.add_argument("--config", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--operator", required=True)
    s.add_argument("--base-url", required=True)
    s.add_argument("--minutes", type=int, default=60)
    s.set_defaults(fn=cmd_console_invite)
    s = co.add_parser("serve")
    s.add_argument("--config", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--secret-env", default="HCML_CONSOLE_SECRET")
    s.add_argument("--label", default=None)
    s.add_argument("--allow-tick", action="store_true", help="dry-run products only: a 'run the loop' button")
    s.add_argument("--votes-off", default=None, metavar="REASON", help="show decisions but refuse web votes")
    s.add_argument("--behind-https", action="store_true", help="loopback behind an HTTPS tunnel: Secure cookies")
    s.set_defaults(fn=cmd_console_serve)
    fk = sub.add_parser("fake").add_subparsers(dest="action", required=True)
    s = fk.add_parser("write-in")
    s.add_argument("--config", required=True)
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.set_defaults(fn=cmd_fake_write_in)
    s = fk.add_parser("vote")
    s.add_argument("--config", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--user", required=True)
    s.add_argument("--answer", choices=("yes", "no"), required=True)
    s.set_defaults(fn=cmd_fake_vote)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except runtime.Paused as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ValueError as exc:
        from sayso.dashboard import RemoteBindRefused
        if not isinstance(exc, RemoteBindRefused):
            raise
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
