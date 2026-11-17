"""Redaction tests. Every secret below is a made-up value in a real token format."""

import json

import pytest

from runbook_agent.executor import MAX_OUTPUT, AuditLog, Executor, Result
from runbook_agent.parser import parse_text
from runbook_agent.redact import redact

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize("text, secret, label", [
    ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "aws-access-key-id"),
    ("using ghp_" + "a" * 36, "ghp_" + "a" * 36, "github-token"),
    ("github_pat_" + "B" * 30, "github_pat_" + "B" * 30, "github-token"),
    ("glpat-" + "c" * 20, "glpat-" + "c" * 20, "gitlab-token"),
    ("SLACK xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef", "slack-token"),
    ("key AIza" + "D" * 35, "AIza" + "D" * 35, "google-api-key"),
    ("sk_live_" + "e" * 24, "sk_live_" + "e" * 24, "stripe-key"),
    ("OPENAI sk-" + "f" * 32, "sk-" + "f" * 32, "api-key"),
    ("cookie " + JWT, JWT, "jwt"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", "auth-header"),
    ("postgres://admin:s3cr3t@db.internal:5432/app", "s3cr3t", "url-password"),
    ("DB_PASSWORD=hunter2", "hunter2", "secret-value"),
    ('{"client_secret": "two words"}', "two words", "secret-value"),
    ("api-key: 'abc123'", "abc123", "secret-value"),
])
def test_known_formats_are_masked(text, secret, label):
    out, count = redact(text)
    assert secret not in out
    assert f"[REDACTED:{label}]" in out
    assert count == 1


def test_context_around_the_secret_is_kept():
    out, _ = redact("Authorization: Bearer abc.def\npostgres://admin:pw@db/app\n")
    assert out == "Authorization: Bearer [REDACTED:auth-header]\npostgres://admin:[REDACTED:url-password]@db/app\n"
    out, _ = redact('{"password": "x1", "user": "dana"}')
    assert out == '{"password": "[REDACTED:secret-value]", "user": "dana"}'


def test_specific_label_wins_and_is_not_masked_twice():
    out, count = redact("GITHUB_TOKEN=ghp_" + "a" * 36 + "\nAuthorization: Bearer " + JWT)
    assert out == "GITHUB_TOKEN=[REDACTED:github-token]\nAuthorization: Bearer [REDACTED:jwt]"
    assert count == 2


def test_private_key_block_including_unterminated():
    pem = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\nafter"
    assert redact(pem) == ("before\n[REDACTED:private-key]\nafter", 1)
    cut = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk"
    assert redact(cut) == ("[REDACTED:private-key]", 1)


def test_ordinary_diagnostics_are_untouched():
    text = ("Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        50G   49G  1.0G  98% /\n"
            "NAME   READY   STATUS    RESTARTS   AGE\napi-7d9   1/1     Running   0          3d\n"
            "https://example.com/path tokens used 5\n")
    assert redact(text) == (text, 0)
    assert redact("") == ("", 0)


def _executor(output, tmp_path):
    rb = parse_text("# T\n\n```bash\necho check\n```\n", "t.md")
    return Executor(rb, execute=True, audit=AuditLog(tmp_path / "audit.jsonl"),
                    readonly_runner=lambda c, t: Result(0, output))


def test_executor_redacts_output_and_audit_log(tmp_path):
    outcomes, status = _executor("export API_TOKEN=supersecret\nok\n", tmp_path).run()
    assert status == "completed"
    assert outcomes[0].output == "export API_TOKEN=[REDACTED:secret-value]\nok\n"
    assert outcomes[0].redacted == 1
    log = (tmp_path / "audit.jsonl").read_text()
    assert "supersecret" not in log
    assert json.loads(log)["redacted"] == 1


def test_redaction_happens_before_truncation(tmp_path):
    # The secret straddles the truncation point; no prefix of it may survive.
    output = "x" * (MAX_OUTPUT - 10) + " password=abcdefghijklmnopqrstuvwxyz"
    outcomes, _ = _executor(output, tmp_path).run()
    assert "abcdefgh" not in outcomes[0].output
    assert len(outcomes[0].output) == MAX_OUTPUT


def test_cli_reports_redaction_count(tmp_path, monkeypatch, capsys):
    from runbook_agent.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rb.md").write_text("# R\n\n1. Show config\n\n```bash\necho db_password=hunter2\n```\n")
    assert main(["run", "rb.md", "--execute"]) == 0
    out = capsys.readouterr().out
    assert "1 secret(s) redacted" in out and "| db_password=[REDACTED:secret-value]" in out
    # The command text is shown and logged as written; only captured output is redacted.
    shown = [line for line in out.splitlines() if line.strip().startswith("|")]
    assert shown == ["      | db_password=[REDACTED:secret-value]"]
    event = json.loads((tmp_path / "runbook-audit.jsonl").read_text())
    assert "hunter2" not in event["output"] and event["redacted"] == 1
