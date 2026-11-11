"""Executor tests. Real subprocesses are limited to harmless commands (echo, sleep);
everything that would change state goes through fake runners."""

import json

import pytest

from runbook_agent import classifier
from runbook_agent.executor import AuditLog, Executor, Result, run_readonly, run_shell
from runbook_agent.parser import parse_text

RUNBOOK = """# Demo

1. Look

```bash
echo looking
```

2. Change

```bash
frobnicate --now
echo after-change
```

3. Verify

```bash
echo verified
```
"""


class FakeRunners:
    def __init__(self, shell_exit=0):
        self.readonly, self.shell, self.shell_exit = [], [], shell_exit

    def run_readonly(self, command, timeout):
        self.readonly.append(command)
        return Result(0, f"ro:{command}\n")

    def run_shell(self, command, timeout):
        self.shell.append(command)
        return Result(self.shell_exit, f"sh:{command}\n")


def make(tmp_path=None, **kwargs):
    fakes = FakeRunners(kwargs.pop("shell_exit", 0))
    audit = AuditLog(tmp_path / "audit.jsonl") if tmp_path else None
    ex = Executor(parse_text(RUNBOOK, "demo.md"), audit=audit,
                  readonly_runner=fakes.run_readonly, shell_runner=fakes.run_shell, **kwargs)
    return ex, fakes


def test_dry_run_is_the_default_and_runs_nothing():
    ex, fakes = make()
    outcomes, status = ex.run()
    assert status == "dry-run"
    assert fakes.readonly == [] and fakes.shell == []
    assert [o.action for o in outcomes] == ["dry-run"] * 4
    assert all(o.exit_code is None for o in outcomes)


def test_execute_runs_read_only_then_stops_at_unapproved_change():
    ex, fakes = make(execute=True)
    outcomes, status = ex.run()
    assert status == "awaiting-approval"
    assert fakes.readonly == ["echo looking"]
    assert fakes.shell == []
    # Both commands of the gated step are blocked, including its read-only one.
    assert [(o.step, o.action) for o in outcomes] == [(1, "auto-run"), (2, "blocked"), (2, "blocked")]


def test_approved_step_runs_and_run_completes():
    ex, fakes = make(execute=True, approver=lambda step: "dana" if step.number == 2 else None)
    outcomes, status = ex.run()
    assert status == "completed"
    assert fakes.shell == ["frobnicate --now"]
    assert fakes.readonly == ["echo looking", "echo after-change", "echo verified"]
    changed = outcomes[1]
    assert changed.action == "approved-run" and changed.approved_by == "dana"


def test_approver_is_asked_once_per_gated_step_only():
    asked = []
    ex, _ = make(execute=True, approver=lambda step: asked.append(step.number))
    ex.run()
    assert asked == [2]


def test_approver_is_never_consulted_in_dry_run():
    asked = []
    ex, fakes = make(approver=lambda step: asked.append(step.number) or "dana")
    ex.run()
    assert asked == [] and fakes.shell == []


def test_failed_change_stops_the_runbook():
    ex, fakes = make(execute=True, approver=lambda step: "dana", shell_exit=2)
    outcomes, status = ex.run()
    assert status == "failed"
    assert fakes.readonly == ["echo looking"]  # nothing after the failed change
    assert outcomes[-1].exit_code == 2


def test_audit_log_records_every_decision(tmp_path):
    ex, _ = make(tmp_path, execute=True)
    ex.run()
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [e["action"] for e in events] == ["auto-run", "blocked", "blocked"]
    first, blocked = events[0], events[1]
    assert first["runbook"] == "demo.md" and first["command"] == "echo looking"
    assert first["exit_code"] == 0 and first["output"] == "ro:echo looking\n" and first["ts"]
    assert blocked["classification"] == "unknown" and "allowlist" in blocked["reason"]
    assert blocked["exit_code"] is None


def test_run_readonly_really_runs_echo_pipelines_without_a_shell():
    assert run_readonly("echo hello world") == Result(0, "hello world\n")
    assert run_readonly("echo 'a  b' | tr a-z A-Z").output == "A  B\n"
    assert run_readonly("echo one && echo two; echo three").output == "one\ntwo\nthree\n"
    assert run_readonly("echo one || echo two").output == "one\n"
    # No shell: the literal text is printed, nothing is expanded.
    assert run_readonly("echo '$HOME' '*'").output == "$HOME *\n"


def test_run_readonly_refuses_anything_not_allowlisted():
    for command in ["frobnicate", "echo hi > out.txt", "echo $(echo hi)", "sudo echo hi"]:
        with pytest.raises(PermissionError):
            run_readonly(command)


def test_run_readonly_reports_missing_command_and_timeout(monkeypatch):
    monkeypatch.setitem(classifier.RULES, "no-such-command-for-tests", classifier.Rule())
    result = run_readonly("no-such-command-for-tests")
    assert result.exit_code == 127 and "command not found" in result.output
    assert run_readonly("sleep 5", timeout=0.2).exit_code == 124


def test_run_shell_with_harmless_echo():
    assert run_shell("echo approved | tr a-z A-Z") == Result(0, "APPROVED\n")
    assert run_shell("echo boom; exit 3").exit_code == 3


def test_end_to_end_with_real_echo_only(tmp_path):
    text = '# T\n\n```bash\necho check\n```\n\nExpand a variable\n\n```bash\necho "value=$RUNBOOK_AGENT_TEST_UNSET"\n```\n'
    rb = parse_text(text, "t.md")
    outcomes, status = Executor(rb, execute=True).run()
    assert status == "awaiting-approval" and outcomes[0].output == "check\n"
    outcomes, status = Executor(rb, execute=True, approver=lambda s: "dana").run()
    assert status == "completed"
    assert outcomes[1].action == "approved-run" and outcomes[1].output == "value=\n"
