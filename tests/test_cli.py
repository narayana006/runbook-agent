import json
from pathlib import Path

import pytest

from runbook_agent.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "runbooks"
SAFE = "# Safe\n\n1. Say hi\n\n```bash\necho hi\n```\n\n2. Needs a shell\n\n```bash\necho \"v=$RUNBOOK_AGENT_TEST_UNSET\"\n```\n\n3. After\n\n```bash\necho after\n```\n"


@pytest.fixture
def runbook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "safe.md"
    path.write_text(SAFE)
    return path


def test_parse_example_text_and_json(capsys):
    assert main(["parse", str(EXAMPLES / "disk-full.md")]) == 0
    out = capsys.readouterr().out
    assert "7 steps" in out and "mutating: runs with elevated privileges (sudo)" in out
    assert main(["parse", str(EXAMPLES / "disk-full.md"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["classification"] for s in data["steps"]].count("mutating") == 2


def test_run_defaults_to_dry_run_and_writes_no_log(runbook, capsys):
    assert main(["run", str(runbook)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "0 command(s) executed" in out
    assert not (runbook.parent / "runbook-audit.jsonl").exists()


def test_execute_stops_for_approval_with_exit_3(runbook, capsys):
    assert main(["run", str(runbook), "--execute"]) == 3
    out = capsys.readouterr().out
    assert "| hi" in out and "BLOCKED" in out and "--approve 2" in out
    assert "after" not in out.split("BLOCKED")[1].split("Result")[0]
    log = [json.loads(line) for line in (runbook.parent / "runbook-audit.jsonl").read_text().splitlines()]
    assert [e["action"] for e in log] == ["auto-run", "blocked"]


def test_execute_with_per_step_approval_completes(runbook, capsys):
    assert main(["run", str(runbook), "--execute", "--approve", "2", "--approver", "dana"]) == 0
    out = capsys.readouterr().out
    assert "approved-run approved by dana" in out and "| v=" in out and "| after" in out


def test_interactive_flag_without_a_terminal_does_not_approve(runbook, capsys):
    assert main(["run", str(runbook), "--execute", "--interactive"]) == 3


def test_approve_unknown_step_is_an_error(runbook):
    with pytest.raises(SystemExit):
        main(["run", str(runbook), "--execute", "--approve", "9"])
    with pytest.raises(SystemExit):
        main(["approve", str(runbook), "9", "--yes"])


def test_approve_requires_confirmation_or_yes(runbook, capsys):
    assert main(["approve", str(runbook), "2", "--state-dir", "state"]) == 1  # no tty, no --yes
    assert not (runbook.parent / "state").exists()
    assert main(["approve", str(runbook), "2", "--yes", "--state-dir", "state", "--approver", "dana"]) == 0
    stored = json.loads((runbook.parent / "state" / "approvals.json").read_text())
    assert [v["approver"] for v in stored.values()] == ["dana"]


def test_missing_file_is_a_clean_error():
    with pytest.raises(SystemExit) as exc:
        main(["parse", "does-not-exist.md"])
    assert "cannot read" in str(exc.value)
