import json

import pytest

from runbook_agent.classifier import (
    MUTATING,
    READ_ONLY,
    RULES,
    UNKNOWN,
    classify,
    classify_step,
)
from runbook_agent.cli import main
from runbook_agent.executor import Executor, Result
from runbook_agent.parser import parse_text
from runbook_agent.rules import ConfigError, build_rules, load_rules
from runbook_agent.server import RunbookServer

GCLOUD = {"allow": {"gcloud": {"subcommands": ["compute instances list", "config list"],
                               "value_flags": ["--project", "--zone"], "deny_long": ["--log-http"]}}}


def test_new_program_is_read_only_only_for_listed_subcommands():
    rules = build_rules(GCLOUD)
    assert classify("gcloud --project p compute instances list --zone z", rules).kind == READ_ONLY
    assert classify("gcloud config list | grep account", rules).kind == READ_ONLY
    v = classify("gcloud compute instances delete web-1", rules)
    assert v.kind == MUTATING and "gcloud compute instances is not a read-only subcommand" in v.reason
    assert classify("gcloud config list --log-http", rules).kind == MUTATING
    # Without the config the program is unknown, and the built-in table is untouched.
    assert classify("gcloud config list").kind == UNKNOWN
    assert "gcloud" not in RULES


def test_extending_a_builtin_keeps_its_restrictions():
    rules = build_rules({"allow": {"kubectl": {"subcommands": ["get-contexts"]},
                                   "uname": {"deny_short": "n"}, "sort": {"deny_long": ["--compress-program"]}}})
    assert classify("kubectl get-contexts", rules).kind == READ_ONLY
    assert classify("kubectl get pods", rules).kind == READ_ONLY
    assert classify("kubectl delete pod x", rules).kind == MUTATING
    assert classify("kubectl get secret db", rules).kind == UNKNOWN
    assert classify("uname -an", rules).kind == MUTATING
    assert classify("sort -o out.txt in.txt", rules).kind == MUTATING
    assert classify("sort --compress-program=gzip in.txt", rules).kind == MUTATING
    assert RULES["kubectl"].subcommands != rules["kubectl"].subcommands


def test_deny_removes_a_program():
    rules = build_rules({"deny": ["ping", "not-a-builtin"]})
    assert classify("ping -c 1 example.com", rules).kind == UNKNOWN
    assert classify_step(["df -h", "ping -c 1 example.com"], rules).kind == UNKNOWN
    assert classify("ping -c 1 example.com").kind == READ_ONLY


@pytest.mark.parametrize("config, message", [
    ({"allow": {"bash": {}}}, "can never be auto-run"),
    ({"allow": {"xargs": {}}}, "can never be auto-run"),
    ({"allow": {"/usr/bin/df": {}}}, "not a plain command name"),
    ({"allow": {"FOO=1": {}}}, "not a plain command name"),
    ({"allow": {"cat": {"subcommands": ["x"]}}}, "already allows any arguments"),
    ({"allow": {"aws": {"prefixes": ["put-"]}}}, "cannot be changed for a built-in"),
    ({"allow": {"tool": {"subcomands": ["x"]}}}, "unknown field(s) subcomands"),
    ({"allow": {"tool": {"subcommands": "list"}}}, "must be a list"),
    ({"allow": {"tool": {"deny_short": "-o"}}}, "flag letters"),
    ({"allow": {"tool": {"max_positional": -1}}}, "non-negative integer"),
    ({"allow": {"tool": {"skip_words": True}}}, "non-negative integer"),
    ({"allow": {"tool": {}}, "deny": ["tool"]}, "both allow and deny"),
    ({"deny": "ping"}, "deny must be a list"),
    ({"allowed": {}}, "unknown top-level"),
    ([], "must be a JSON object"),
])
def test_invalid_configs_are_rejected(config, message):
    with pytest.raises(ConfigError) as exc:
        build_rules(config)
    assert message in str(exc.value)


def test_load_rules_reports_file_errors(tmp_path):
    assert load_rules(None) == RULES
    bad = tmp_path / "rules.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_rules(bad)
    with pytest.raises(ConfigError, match="cannot read"):
        load_rules(tmp_path / "missing.json")
    bad.write_text(json.dumps({"allow": {"sh": {}}}))
    with pytest.raises(ConfigError, match=r"rules.json: allow: sh"):
        load_rules(bad)


def test_executor_and_server_use_custom_rules(tmp_path):
    rules = build_rules(GCLOUD)
    rb = parse_text("# T\n\n1. List\n\n```bash\ngcloud config list\n```\n", name="t.md")
    calls = []

    def fake(cmd, timeout):
        calls.append(cmd)
        return Result(0, "ok\n")

    outcomes, status = Executor(rb, execute=True, readonly_runner=fake, shell_runner=fake, rules=rules).run()
    assert status == "completed" and outcomes[0].action == "auto-run"
    outcomes, status = Executor(rb, execute=True, readonly_runner=fake, shell_runner=fake).run()
    assert status == "awaiting-approval"

    (tmp_path / "t.md").write_text("# T\n\n1. List\n\n```bash\ngcloud config list\n```\n")
    server = RunbookServer(tmp_path, rules=rules)
    assert server.get_runbook({"runbook": "t.md"})["steps"][0]["classification"] == READ_ONLY
    assert RunbookServer(tmp_path).list_runbooks({})[0]["steps_needing_approval"] == 1


def test_cli_rules_flag(tmp_path, capsys):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(GCLOUD))
    book = tmp_path / "t.md"
    book.write_text("# T\n\n1. List\n\n```bash\ngcloud config list\n```\n")
    assert main(["parse", str(book)]) == 0
    assert "unknown: gcloud is not on the read-only allowlist" in capsys.readouterr().out
    assert main(["parse", str(book), "--rules", str(rules)]) == 0
    assert "read-only: only read-only commands" in capsys.readouterr().out
    assert main(["run", str(book), "--rules", str(rules)]) == 0
    assert "would run automatically" in capsys.readouterr().out
    rules.write_text(json.dumps({"allow": {"bash": {}}}))
    with pytest.raises(SystemExit, match="bad rules file"):
        main(["run", str(book), "--rules", str(rules)])
