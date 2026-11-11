"""Run runbook steps: dry-run by default, read-only automatically, changes only with approval."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .classifier import READ_ONLY, Unsafe, classify, parse_command
from .parser import Runbook, Step

MAX_OUTPUT = 4000


@dataclass
class Result:
    exit_code: int
    output: str


def run_readonly(command: str, timeout: float = 30.0, rules: dict | None = None) -> Result:
    """Run a read-only command without a shell.

    The command is re-validated here, so nothing reaches ``subprocess`` unless
    it passes the allowlist. Pipes and ``&&``/``||``/``;`` are handled directly.
    """
    try:
        segments = parse_command(command, rules)
    except Unsafe as exc:
        raise PermissionError(f"refusing to auto-run: {exc.reason}") from exc
    pipelines: list[tuple[str | None, list[list[str]]]] = []
    for connector, argv in segments:
        if connector == "|":
            pipelines[-1][1].append(argv)
        else:
            pipelines.append((connector, [argv]))
    deadline = time.monotonic() + timeout
    code, chunks = 0, []
    for connector, pipeline in pipelines:
        if (connector == "&&" and code != 0) or (connector == "||" and code == 0):
            continue
        code, out = _run_pipeline(pipeline, max(0.1, deadline - time.monotonic()))
        chunks.append(out)
        if code == 124:
            break
    return Result(code, "".join(chunks))


def _run_pipeline(pipeline: list[list[str]], timeout: float) -> tuple[int, str]:
    procs: list[subprocess.Popen] = []
    with tempfile.TemporaryFile() as err:
        try:
            prev = subprocess.DEVNULL
            for argv in pipeline:
                proc = subprocess.Popen(argv, stdin=prev, stdout=subprocess.PIPE, stderr=err)
                if procs:
                    procs[-1].stdout.close()  # let the upstream process see SIGPIPE
                procs.append(proc)
                prev = proc.stdout
            out, _ = procs[-1].communicate(timeout=timeout)
            for proc in procs[:-1]:
                proc.wait(timeout=5)
            code = procs[-1].returncode
        except FileNotFoundError as exc:
            out, code = f"command not found: {exc.filename}\n".encode(), 127
        except subprocess.TimeoutExpired:
            out, code = f"timed out after {timeout:.0f}s\n".encode(), 124
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stdout and not proc.stdout.closed:
                    proc.stdout.close()
        err.seek(0)
        return code, (out + err.read()).decode(errors="replace")


def run_shell(command: str, timeout: float = 30.0) -> Result:
    """Run an approved command through /bin/sh. Only called after explicit approval."""
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(124, f"timed out after {timeout:.0f}s\n")
    return Result(proc.returncode, proc.stdout.decode(errors="replace"))


class AuditLog:
    """Append-only JSON Lines record of every decision and execution."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def write(self, **event) -> dict:
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        return event


@dataclass
class Outcome:
    """What happened to one command of one step."""

    step: int
    title: str
    command: str
    classification: str
    reason: str
    action: str  # dry-run | auto-run | approved-run | blocked
    approved_by: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output: str | None = None


# An approver is asked once per mutating step and returns the approver's name, or None.
Approver = Callable[[Step], "str | None"]


def deny_all(step: Step) -> None:
    return None


class Executor:
    def __init__(
        self,
        runbook: Runbook,
        *,
        execute: bool = False,
        approver: Approver = deny_all,
        audit: AuditLog | None = None,
        timeout: float = 30.0,
        readonly_runner: Callable[[str, float], Result] | None = None,
        shell_runner: Callable[[str, float], Result] = run_shell,
        rules: dict | None = None,
    ):
        self.runbook = runbook
        self.execute = execute
        self.approver = approver
        self.audit = audit or AuditLog(None)
        self.timeout = timeout
        self.rules = rules
        self._readonly_runner = readonly_runner or (lambda c, t: run_readonly(c, t, rules=rules))
        self._shell_runner = shell_runner

    def run_step(self, step: Step) -> tuple[list[Outcome], bool]:
        """Run one step. Returns its outcomes and whether the run may continue."""
        verdicts = [classify(c, self.rules) for c in step.commands]
        gated = any(v.needs_approval for v in verdicts)
        approved_by = None
        if self.execute and gated:
            # One decision per step: a step is approved as a whole, exactly as written.
            approved_by = self.approver(step)
        outcomes, proceed = [], True
        for command, verdict in zip(step.commands, verdicts):
            out = Outcome(step.number, step.title, command, verdict.kind, verdict.reason, "dry-run")
            if self.execute:
                if gated and not approved_by:
                    out.action, proceed = "blocked", False
                else:
                    out.action = "approved-run" if gated else "auto-run"
                    out.approved_by = approved_by
                    runner = self._readonly_runner if verdict.kind == READ_ONLY else self._shell_runner
                    started = time.monotonic()
                    result = runner(command, self.timeout)
                    out.duration_ms = int((time.monotonic() - started) * 1000)
                    out.exit_code = result.exit_code
                    out.output = result.output[:MAX_OUTPUT]
                    if gated and result.exit_code != 0:
                        proceed = False  # a failed change stops the runbook
            self.audit.write(runbook=self.runbook.name, **asdict(out))
            outcomes.append(out)
            if not proceed and out.action != "blocked":
                break
        return outcomes, proceed

    def run(self) -> tuple[list[Outcome], str]:
        """Run steps in order. Status is ``dry-run``, ``completed``, ``awaiting-approval`` or ``failed``."""
        outcomes: list[Outcome] = []
        for step in self.runbook.steps:
            step_outcomes, proceed = self.run_step(step)
            outcomes.extend(step_outcomes)
            if not proceed:
                blocked = any(o.action == "blocked" for o in step_outcomes)
                return outcomes, "awaiting-approval" if blocked else "failed"
        return outcomes, "completed" if self.execute else "dry-run"
