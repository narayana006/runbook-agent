from pathlib import Path

import pytest

from runbook_agent.cli import main
from runbook_agent.parser import ParamError, parse_text
from runbook_agent.server import RunbookServer, ToolError

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "runbooks" / "restart-service.md"
TEXT = "# T\n\n1. Check {{svc}}\n\n```bash\necho {{svc}} {{n=3}}\n```\n\n```yaml\nimage: {{ .Values.image }}\n```\n"


def test_placeholders_are_collected_and_left_alone_without_params():
    rb = parse_text(TEXT)
    assert rb.parameters == {"svc": None, "n": "3"}
    assert rb.steps[0].commands == ["echo {{svc}} {{n=3}}"]


def test_substitution_uses_value_then_default():
    rb = parse_text(TEXT, params={"svc": "api"})
    assert rb.steps[0].commands == ["echo api 3"]
    assert rb.steps[0].title == "Check api"
    assert parse_text(TEXT, params={"svc": "api", "n": "9"}).steps[0].commands == ["echo api 9"]


def test_missing_required_parameter_is_an_error():
    with pytest.raises(ParamError, match="missing parameter svc"):
        parse_text(TEXT, params={})


@pytest.mark.parametrize("value", ["a b", "x;rm -rf /", "$(id)", "--delete", "a'b", "", "x\ny", "`id`"])
def test_unsafe_values_are_rejected(value):
    with pytest.raises(ParamError):
        parse_text(TEXT, params={"svc": value})


def test_unsafe_default_is_rejected():
    with pytest.raises(ParamError):
        parse_text("```bash\necho {{x=a;b}}\n```\n", params={})


def test_non_shell_blocks_are_not_touched():
    parse_text(TEXT, params={"svc": "api"})  # the yaml {{ .Values.image }} must not raise


def test_cli_parse_lists_parameters_and_run_substitutes(capsys):
    assert main(["parse", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "Parameters: deployment, namespace, lines (default 20)" in out
    assert main(["run", str(EXAMPLE), "--param", "namespace=shop", "--param", "deployment=checkout-api"]) == 0
    out = capsys.readouterr().out
    assert "kubectl get deploy checkout-api -n shop" in out and "--tail=20" in out
    assert "kubectl rollout restart deploy/checkout-api -n shop" in out and "needs approval" in out


def test_cli_errors(capsys):
    with pytest.raises(SystemExit, match="missing parameter"):
        main(["run", str(EXAMPLE)])
    with pytest.raises(SystemExit, match="not allowed"):
        main(["run", str(EXAMPLE), "--param", "namespace=a b", "--param", "deployment=x"])
    with pytest.raises(SystemExit, match="NAME=VALUE"):
        main(["run", str(EXAMPLE), "--param", "oops"])
    with pytest.raises(SystemExit, match="no parameter"):
        main(["run", str(EXAMPLE), "--param", "namespace=a", "--param", "deployment=b", "--param", "zzz=1"])


def test_approval_is_bound_to_parameter_values(tmp_path):
    from runbook_agent.parser import parse_file
    a = parse_file(EXAMPLE, {"namespace": "shop", "deployment": "api"}).steps[2].fingerprint("r.md")
    b = parse_file(EXAMPLE, {"namespace": "shop", "deployment": "web"}).steps[2].fingerprint("r.md")
    assert a != b


def test_server_params(tmp_path):
    (tmp_path / "r.md").write_text(EXAMPLE.read_text())
    srv = RunbookServer(tmp_path)
    meta = srv.get_runbook({"runbook": "r.md"})
    assert meta["parameters"] == {"deployment": None, "namespace": None, "lines": "20"}
    with pytest.raises(ToolError, match="missing parameter"):
        srv.run_step({"runbook": "r.md", "step": 1})
    with pytest.raises(ToolError, match="not allowed"):
        srv.run_step({"runbook": "r.md", "step": 1, "params": {"namespace": "a;b", "deployment": "x"}})
    res = srv.run_step({"runbook": "r.md", "step": 3, "params": {"namespace": "shop", "deployment": "api"}})
    assert res["commands"][0]["command"] == "kubectl rollout restart deploy/api -n shop"
    assert res["status"] == "dry-run"
