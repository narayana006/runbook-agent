"""Command-line interface for runbook-agent."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .approvals import DEFAULT_TTL, ApprovalStore
from .classifier import READ_ONLY, classify, classify_step
from .executor import AuditLog, Executor, Outcome
from .parser import Runbook, Step, parse_file
from .server import RunbookServer

DEFAULT_LOG = "runbook-audit.jsonl"
DEFAULT_STATE = ".runbook-agent"
EXIT = {"dry-run": 0, "completed": 0, "failed": 1, "awaiting-approval": 3}


def _load(path: str) -> Runbook:
    try:
        return parse_file(path)
    except OSError as exc:
        raise SystemExit(f"runbook-agent: cannot read {path}: {exc.strerror}")


def cmd_parse(args) -> int:
    rb = _load(args.runbook)
    if args.json:
        steps = [
            {**asdict(s), "classification": classify_step(s.commands).kind,
             "commands": [{"command": c, **asdict(classify(c))} for c in s.commands]}
            for s in rb.steps
        ]
        print(json.dumps({"runbook": rb.name, "title": rb.title, "steps": steps}, indent=2))
        return 0
    print(f"{rb.title} ({rb.name}): {len(rb.steps)} steps")
    for s in rb.steps:
        print(f"\n[{s.number}] {s.title}  ({classify_step(s.commands).kind}, line {s.line})")
        for c in s.commands:
            v = classify(c)
            for i, line in enumerate(c.splitlines()):
                print(f"    {'$' if i == 0 else ' '} {line}")
            print(f"      -> {v.kind}: {v.reason}")
    return 0


def _print_outcome(o: Outcome, first: bool) -> None:
    if first:
        print(f"\n[{o.step}] {o.title}")
    for i, line in enumerate(o.command.splitlines()):
        print(f"    {'$' if i == 0 else ' '} {line}")
    gated = o.classification != READ_ONLY
    if o.action == "dry-run":
        note = f"needs approval ({o.reason})" if gated else "would run automatically"
        print(f"      dry-run: {o.classification}, {note}")
    elif o.action == "blocked":
        print(f"      BLOCKED: {o.classification}, not approved ({o.reason})")
    else:
        who = f" approved by {o.approved_by}" if o.action == "approved-run" else ""
        print(f"      {o.action}{who}: exit {o.exit_code} in {o.duration_ms} ms")
        for line in (o.output or "").rstrip().splitlines():
            print(f"      | {line}")


def cmd_run(args) -> int:
    rb = _load(args.runbook)
    approved = set(args.approve or [])
    unknown = approved - {s.number for s in rb.steps}
    if unknown:
        raise SystemExit(f"runbook-agent: --approve refers to missing step(s): {sorted(unknown)}")

    def approver(step: Step) -> str | None:
        if step.number in approved:
            return args.approver
        if args.interactive and sys.stdin.isatty():
            print(f"\nStep {step.number} ({step.title}) changes state:")
            for c in step.commands:
                print(f"    $ {c}")
            if input("Type 'yes' to run this step: ").strip() == "yes":
                return args.approver
        return None

    log = args.log or (DEFAULT_LOG if args.execute else None)
    executor = Executor(rb, execute=args.execute, approver=approver, audit=AuditLog(log), timeout=args.timeout)
    mode = "EXECUTE" if args.execute else "DRY RUN (nothing is executed; pass --execute to run)"
    print(f"{rb.title} ({rb.name}): {len(rb.steps)} steps, mode: {mode}")
    outcomes, status = executor.run()
    seen = set()
    for o in outcomes:
        _print_outcome(o, o.step not in seen)
        seen.add(o.step)
    ran = sum(o.exit_code is not None for o in outcomes)
    gated = len({o.step for o in outcomes if o.classification != READ_ONLY})
    print(f"\nResult: {status}. {ran} command(s) executed, {gated} step(s) need approval"
          + (f", audit log: {log}" if log else "") + ".")
    if status == "awaiting-approval":
        blocked = next(o.step for o in outcomes if o.action == "blocked")
        print(f"Stopped before step {blocked}. Review it, then re-run with --approve {blocked} (or --interactive).")
    return EXIT[status]


def cmd_approve(args) -> int:
    rb = _load(args.runbook)
    step = next((s for s in rb.steps if s.number == args.step), None)
    if step is None:
        raise SystemExit(f"runbook-agent: {rb.name} has no step {args.step}")
    print(f"Approving step {step.number} of {rb.name}: {step.title}")
    for c in step.commands:
        print(f"    $ {c}")
    if not args.yes:
        if not sys.stdin.isatty() or input("Type 'yes' to approve one run of this step: ").strip() != "yes":
            print("Not approved.")
            return 1
    ApprovalStore(args.state_dir, ttl=args.ttl).grant(step.fingerprint(rb.name), args.approver)
    print(f"Approved once for {args.approver}; expires in {int(args.ttl // 60)} min or when the step text changes.")
    return 0


def cmd_serve(args) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        raise SystemExit(f"runbook-agent: not a directory: {directory}")
    log = args.log or (DEFAULT_LOG if args.execute else None)
    server = RunbookServer(directory, execute=args.execute, store=ApprovalStore(args.state_dir),
                           audit=AuditLog(log), timeout=args.timeout)
    mode = "execute" if args.execute else "dry-run"
    print(f"runbook-agent MCP server on stdio: {directory} ({mode} mode)", file=sys.stderr)
    server.serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runbook-agent", description=__doc__)
    p.add_argument("--version", action="version", version=f"runbook-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("parse", help="show a runbook's steps and how each command is classified")
    sp.add_argument("runbook")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("run", help="walk a runbook; dry-run unless --execute is given")
    sp.add_argument("runbook")
    sp.add_argument("--execute", action="store_true", help="actually run commands (default is a dry run)")
    sp.add_argument("--approve", type=int, action="append", metavar="STEP",
                    help="approve one mutating step by number (repeatable)")
    sp.add_argument("--interactive", action="store_true", help="prompt on the terminal before each mutating step")
    sp.add_argument("--approver", default=getpass.getuser(), help="name recorded in the audit log")
    sp.add_argument("--log", help=f"audit log path (default with --execute: {DEFAULT_LOG})")
    sp.add_argument("--timeout", type=float, default=30.0, help="seconds per command (default 30)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("approve", help="record a one-time approval of a step for the MCP server")
    sp.add_argument("runbook")
    sp.add_argument("step", type=int)
    sp.add_argument("--yes", action="store_true", help="do not prompt for confirmation")
    sp.add_argument("--approver", default=getpass.getuser())
    sp.add_argument("--ttl", type=float, default=DEFAULT_TTL, help="seconds the approval stays valid (default 900)")
    sp.add_argument("--state-dir", default=DEFAULT_STATE)
    sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("serve", help="serve a directory of runbooks as an MCP server over stdio")
    sp.add_argument("directory")
    sp.add_argument("--execute", action="store_true", help="allow run_step to execute (default is dry-run)")
    sp.add_argument("--log", help=f"audit log path (default with --execute: {DEFAULT_LOG})")
    sp.add_argument("--state-dir", default=DEFAULT_STATE)
    sp.add_argument("--timeout", type=float, default=30.0)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
