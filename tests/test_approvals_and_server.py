import io
import json

import pytest

from runbook_agent.approvals import ApprovalStore
from runbook_agent.executor import AuditLog, Executor, Result
from runbook_agent.parser import parse_file
from runbook_agent.server import RunbookServer

RUNBOOK = "# Cache\n\n1. Look\n\n```bash\necho looking\n```\n\n2. Flush\n\n```bash\nfrobnicate flush\n```\n"


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_approval_is_one_time(tmp_path):
    store = ApprovalStore(tmp_path)
    assert store.consume("abc") is None
    store.grant("abc", "dana")
    assert store.consume("abc") == "dana"
    assert store.consume("abc") is None


def test_approval_expires(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path, ttl=60, clock=clock)
    store.grant("abc", "dana")
    clock.now += 61
    assert store.consume("abc") is None


@pytest.fixture
def setup(tmp_path):
    books = tmp_path / "runbooks"
    books.mkdir()
    (books / "cache.md").write_text(RUNBOOK)
    (tmp_path / "outside.md").write_text(RUNBOOK)
    ran = []

    def factory(rb, **kw):
        def fake(command, timeout):
            ran.append(command)
            return Result(0, "ok\n")
        return Executor(rb, readonly_runner=fake, shell_runner=fake, **kw)

    store = ApprovalStore(tmp_path / "state")

    def server(execute):
        return RunbookServer(books, execute=execute, store=store,
                             audit=AuditLog(tmp_path / "audit.jsonl"), executor_factory=factory)

    return server, store, ran, books


def call(server, name, **arguments):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_initialize_and_tool_list_offer_no_approval_tool(setup):
    server = setup[0](True)
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05"}})["result"]
    assert init["serverInfo"]["name"] == "runbook-agent" and "tools" in init["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["list_runbooks", "get_runbook", "run_step"]
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601


def test_list_and_get_runbook(setup):
    server = setup[0](False)
    listing, _ = call(server, "list_runbooks")
    assert listing == [{"runbook": "cache.md", "title": "Cache", "steps": 2, "steps_needing_approval": 1}]
    detail, _ = call(server, "get_runbook", runbook="cache.md")
    assert [s["needs_approval"] for s in detail["steps"]] == [False, True]
    assert detail["steps"][1]["commands"][0]["command"] == "frobnicate flush"


def test_run_step_is_dry_run_unless_server_started_with_execute(setup):
    server, _, ran, _ = setup
    result, _ = call(server(False), "run_step", runbook="cache.md", step=1)
    assert result["status"] == "dry-run" and ran == []


def test_read_only_step_runs_and_mutating_step_is_blocked(setup):
    server, _, ran, _ = setup
    result, _ = call(server(True), "run_step", runbook="cache.md", step=1)
    assert result["status"] == "ran" and ran == ["echo looking"]
    result, _ = call(server(True), "run_step", runbook="cache.md", step=2)
    assert result["status"] == "blocked" and ran == ["echo looking"]
    assert "runbook-agent approve" in result["message"] and result["message"].endswith("cache.md 2")


def test_human_approval_allows_exactly_one_run(setup):
    server, store, ran, books = setup
    step = parse_file(books / "cache.md").steps[1]
    store.grant(step.fingerprint("cache.md"), "dana")
    result, _ = call(server(True), "run_step", runbook="cache.md", step=2)
    assert result["status"] == "ran" and result["commands"][0]["approved_by"] == "dana"
    assert ran == ["frobnicate flush"]
    result, _ = call(server(True), "run_step", runbook="cache.md", step=2)
    assert result["status"] == "blocked" and ran == ["frobnicate flush"]


def test_editing_the_step_voids_the_approval(setup):
    server, store, ran, books = setup
    step = parse_file(books / "cache.md").steps[1]
    store.grant(step.fingerprint("cache.md"), "dana")
    (books / "cache.md").write_text(RUNBOOK.replace("frobnicate flush", "frobnicate flush --all"))
    result, _ = call(server(True), "run_step", runbook="cache.md", step=2)
    assert result["status"] == "blocked" and ran == []


def test_bad_arguments_are_tool_errors(setup):
    server = setup[0](True)
    for args in [{"runbook": "../outside.md", "step": 1}, {"runbook": "missing.md", "step": 1},
                 {"runbook": "cache.md", "step": 9}, {"runbook": "cache.md"}]:
        result, is_error = call(server, "run_step", **args)
        assert is_error and "error" in result
    _, is_error = call(server, "approve_step", runbook="cache.md", step=2)
    assert is_error


def test_serve_loop_over_stdio(setup):
    server = setup[0](False)
    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
