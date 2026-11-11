import pytest

from runbook_agent.classifier import MUTATING, READ_ONLY, UNKNOWN, classify, classify_step

READ_ONLY_COMMANDS = [
    "df -h",
    "echo hello world",
    "kubectl get pods -n shop -o wide",
    "kubectl -n shop --context prod-eu logs deploy/api --since=10m | grep -c ERROR",
    "kubectl rollout status deploy/api",
    "kubectl config current-context",
    "git -C /srv/app log --oneline -5",
    "docker compose ps",
    "systemctl status nginx",
    "journalctl -u nginx --since '1 hour ago' | tail -n 50",
    "aws ec2 describe-instances --region eu-west-1",
    "aws s3 ls",
    "curl -sS https://status.example.com/healthz",
    "curl -X GET https://status.example.com/healthz",
    "grep 'timeout|refused' /var/log/app.log",
    "grep 'error$' /var/log/app.log",
    "find /var/log -name app.log -mtime -1",
    "ps aux | sort -k3 -r | head -5",
    "echo a && echo b; echo c || echo d",
    "sort -u names.txt # trailing comment",
]

MUTATING_COMMANDS = [
    ("rm -rf /var/cache/app", UNKNOWN),
    ("sudo journalctl --disk-usage", MUTATING),
    ("kubectl delete pod api-0", MUTATING),
    ("kubectl rollout restart deploy/api", MUTATING),
    ("kubectl -n shop apply -f fix.yaml", MUTATING),
    ("kubectl get secret db-creds -o yaml", UNKNOWN),
    ("kubectl", MUTATING),
    ("git push origin main", MUTATING),
    ("git log --output=/etc/passwd", MUTATING),
    ("docker restart api", MUTATING),
    ("systemctl restart nginx", MUTATING),
    ("journalctl --vacuum-size=500M", MUTATING),
    ("aws ec2 terminate-instances --instance-ids i-0abc", MUTATING),
    ("aws secretsmanager get-secret-value --secret-id db", UNKNOWN),
    ("aws ssm get-parameter --name /db/pw --with-decryption", MUTATING),
    ("curl -X POST https://api.example.com/restart", MUTATING),
    ("curl -XDELETE https://api.example.com/item/1", MUTATING),
    ("curl --data-raw '{}' https://api.example.com", MUTATING),
    ("curl -sSLo /usr/local/bin/tool https://example.com/tool", MUTATING),
    ("find /var/log -name '*.gz' -delete", MUTATING),
    ("find . -exec rm '{}' ';'", MUTATING),
    ("find . -exec rm {} ;", UNKNOWN),
    ("sort -o names.txt names.txt", MUTATING),
    ("date -s '2020-01-01'", MUTATING),
    ("hostname new-name", MUTATING),
    ("uniq in.txt out.txt", MUTATING),
    ("echo hi > /etc/motd", MUTATING),
    ("echo hi >> notes.txt", MUTATING),
    ("df -h | tee /tmp/df.txt", UNKNOWN),
    ("echo ok && rm -rf /srv/data", UNKNOWN),
    ("echo ok; shutdown -h now", UNKNOWN),
    ("echo $(rm -rf /srv/data)", UNKNOWN),
    ("echo `whoami`", UNKNOWN),
    ('echo "$HOME"', UNKNOWN),
    ("cat /var/log/*.log", UNKNOWN),
    ("ls ~/secrets", UNKNOWN),
    ("cat < /etc/hosts", UNKNOWN),
    ("sleep 1000 &", UNKNOWN),
    ("(echo a)", UNKNOWN),
    ("LD_PRELOAD=/tmp/x.so ls", UNKNOWN),
    ("/tmp/ls -l", UNKNOWN),
    ("echo 'unterminated", UNKNOWN),
    ("echo a | | echo b", UNKNOWN),
    ("| echo b", UNKNOWN),
    ("echo a &&", UNKNOWN),
    ("", UNKNOWN),
    ("xargs rm", UNKNOWN),
    ("bash -c 'echo hi'", UNKNOWN),
    ("env echo hi", UNKNOWN),
    ("echo a\necho b", UNKNOWN),
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only(command):
    verdict = classify(command)
    assert verdict.kind == READ_ONLY, verdict.reason
    assert not verdict.needs_approval


@pytest.mark.parametrize("command,kind", MUTATING_COMMANDS)
def test_needs_approval(command, kind):
    verdict = classify(command)
    assert verdict.needs_approval
    assert verdict.kind == kind, verdict.reason
    assert verdict.reason


def test_quoted_operators_are_plain_arguments():
    assert classify("echo 'a | rm -rf /' \"b && c\" d\\;e").kind == READ_ONLY


def test_single_quotes_hide_dollar_but_double_quotes_do_not():
    assert classify("echo '$HOME'").kind == READ_ONLY
    assert classify('echo "\\$HOME"').kind == READ_ONLY
    assert classify('echo "$(id)"').kind == UNKNOWN


def test_reasons_name_the_problem():
    assert "sudo" in classify("sudo systemctl status nginx").reason
    assert "kubectl rollout restart" in classify("kubectl rollout restart deploy/api").reason
    assert "allowlist" in classify("terraform apply").reason


def test_step_is_gated_if_any_command_is():
    assert classify_step(["echo a", "df -h"]).kind == READ_ONLY
    assert classify_step(["echo a", "frobnicate", "rm x > y"]).kind == MUTATING
    assert classify_step(["echo a", "frobnicate"]).kind == UNKNOWN
