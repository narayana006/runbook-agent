# runbook-agent

Turn Markdown runbooks into executable steps: read-only diagnostics run automatically, anything that changes state waits for a human to approve that exact step. Works as a command-line tool and as an MCP server.

**Status: first working version (0.1.0).** Parsing, command classification, dry-run, approval-gated execution, the audit log and a stdio MCP server all work today. The classifier is a fixed allowlist that cannot yet be extended from a config file; see [Limitations](#limitations).

## Safety model

The tool is built so that the dangerous path is the one that takes effort.

- **Dry-run by default.** `run` and `serve` execute nothing unless you pass `--execute`.
- **Allowlist, not blocklist.** A command auto-runs only if every part of it is a known inspection command (`df`, `kubectl get`, `journalctl`, ...) used without flags that write. Unknown commands are treated the same as mutating ones.
- **No shell for auto-run commands.** Read-only commands are executed directly as argument lists. Anything that would need a shell to interpret (`$VAR`, `$(...)`, globs, redirection, `sudo`, environment assignments, multi-line scripts) is never auto-run.
- **Approval is per step and explicit.** A mutating step runs only when a human names it (`--approve 3`), types `yes` at an interactive prompt, or records a one-time approval for the MCP server. A blocked or failed change stops the runbook.
- **The agent cannot approve itself.** The MCP server has no approval tool. Approvals are recorded out of band, are single-use, expire after 15 minutes and are bound to the exact command text, so editing the step voids them.
- **Everything is logged.** Each command, its classification, the reason, who approved it, exit code and output go to a JSON Lines audit log.

## Install

```bash
pip install git+https://github.com/narayana006/runbook-agent
```

Python 3.10 or newer. No dependencies outside the standard library.

## Quick start

Clone the repo for the example runbooks, then dry-run one. Nothing is executed:

```console
$ runbook-agent run examples/runbooks/disk-full.md
Disk full on a checkout-api node (disk-full.md): 7 steps, mode: DRY RUN (nothing is executed; pass --execute to run)

[1] Confirm which filesystem is full.
    $ df -h
      dry-run: read-only, would run automatically

[2] Find the pods on the affected node and check for restarts.
    $ kubectl get pods -n shop -o wide
      dry-run: read-only, would run automatically
    $ kubectl describe node worker-07.example.com
      dry-run: read-only, would run automatically

[3] Look for a log flood in the last few minutes.
    $ kubectl logs -n shop deploy/checkout-api --since=10m | grep -c ERROR
      dry-run: read-only, would run automatically

[4] Check how much space old journal entries are using.
    $ journalctl --disk-usage
      dry-run: read-only, would run automatically

[5] Trim the journal to free space quickly.
    $ sudo journalctl --vacuum-size=500M
      dry-run: mutating, needs approval (runs with elevated privileges (sudo))

[6] If a single pod is flooding the logs, restart the deployment.
    $ kubectl rollout restart -n shop deploy/checkout-api
      dry-run: mutating, needs approval (kubectl rollout restart is not a read-only subcommand)
    $ kubectl rollout status -n shop deploy/checkout-api
      dry-run: read-only, would run automatically

[7] Confirm disk usage is back under the alert threshold.
    $ df -h
      dry-run: read-only, would run automatically

Result: dry-run. 0 command(s) executed, 2 step(s) need approval.
```

`examples/runbooks/hello.md` only uses `echo`, `uname`, `tr`, `sort` and `cat`, so it is safe to execute for real. With `--execute` the diagnostics run and the tool stops at the first change (exit code 3):

```console
$ runbook-agent run examples/runbooks/hello.md --execute
Hello runbook (hello.md): 4 steps, mode: EXECUTE

[1] Print the kernel name.
    $ uname -s
      auto-run: exit 0 in 12 ms
      | Darwin

[2] Pipes between read-only commands are fine.
    $ echo "three two one" | tr " " "\n" | sort
      auto-run: exit 0 in 21 ms
      | one
      | three
      | two

[3] Write a marker file (redirection writes to disk, so this needs approval).
    $ echo "runbook-agent was here" > runbook-agent-demo.txt
      BLOCKED: mutating, not approved (redirects output to a file)

Result: awaiting-approval. 2 command(s) executed, 1 step(s) need approval, audit log: runbook-audit.jsonl.
Stopped before step 3. Review it, then re-run with --approve 3 (or --interactive).
```

Approve that one step and the run completes:

```console
$ runbook-agent run examples/runbooks/hello.md --execute --approve 3 --approver dana
Hello runbook (hello.md): 4 steps, mode: EXECUTE

[1] Print the kernel name.
    $ uname -s
      auto-run: exit 0 in 32 ms
      | Darwin

[2] Pipes between read-only commands are fine.
    $ echo "three two one" | tr " " "\n" | sort
      auto-run: exit 0 in 69 ms
      | one
      | three
      | two

[3] Write a marker file (redirection writes to disk, so this needs approval).
    $ echo "runbook-agent was here" > runbook-agent-demo.txt
      approved-run approved by dana: exit 0 in 57 ms

[4] Read the marker file back.
    $ cat runbook-agent-demo.txt
      auto-run: exit 0 in 16 ms
      | runbook-agent was here

Result: completed. 4 command(s) executed, 1 step(s) need approval, audit log: runbook-audit.jsonl.
```

The matching audit log entry for step 3:

```json
{"ts": "2026-11-11T02:00:36+00:00", "runbook": "hello.md", "step": 3, "title": "Write a marker file (redirection writes to disk, so this needs approval).", "command": "echo \"runbook-agent was here\" > runbook-agent-demo.txt", "classification": "mutating", "reason": "redirects output to a file", "action": "approved-run", "approved_by": "dana", "exit_code": 0, "duration_ms": 57, "output": ""}
```

## How runbooks are read

- Each fenced code block tagged `bash`, `sh`, `shell`, `zsh` or `console` is one step. Untagged blocks and other languages are ignored, so sample output and YAML snippets never become commands.
- The step title is the nearest prose line above the block (usually the numbered list item), falling back to the section heading.
- Each line of a block is one command. Comment lines are dropped and trailing-backslash continuations are joined. In `console` blocks only `$ ` lines are commands.
- A block containing a heredoc or shell control flow (`for`, `if`, `while`, ...) is kept whole as one script, which always needs approval.

## Commands

| Command | What it does |
| --- | --- |
| `runbook-agent parse FILE [--json]` | List steps and show how every command is classified, with the reason. |
| `runbook-agent run FILE` | Dry-run. Add `--execute` to run read-only steps and stop at the first unapproved change. |
| `runbook-agent run FILE --execute --approve N` | Also run mutating step `N`. Repeat `--approve` for more steps. `--interactive` prompts on the terminal instead and requires typing `yes`. |
| `runbook-agent approve FILE N` | Record a one-time approval of step `N` for the MCP server (prompts for `yes`; `--yes` skips the prompt). |
| `runbook-agent serve DIR [--execute]` | Serve the `*.md` runbooks in `DIR` as an MCP server over stdio. |

Other options: `--log PATH` (audit log; defaults to `runbook-audit.jsonl` when `--execute` is given, no log is written for a dry run unless you ask), `--timeout SECONDS` per command (default 30), `--approver NAME` (recorded in the log, defaults to the current user), `--state-dir DIR` (where approvals are stored, default `.runbook-agent`).

Exit codes for `run`: `0` dry-run finished or runbook completed, `1` an approved change failed, `3` stopped waiting for approval.

### Classification

Three labels: `read-only` runs automatically; `mutating` (recognised as changing state) and `unknown` (not recognised, or needs a shell) both wait for approval.

Read-only today: common inspection commands (`cat`, `ls`, `grep`, `head`, `tail`, `df`, `du`, `ps`, `dig`, `ping`, `jq`, `stat`, ...), `find` without `-delete`/`-exec`, `sort` without `-o`, `journalctl` without maintenance flags, `curl` for plain GET/HEAD with no upload or output-file flags, and the read subcommands of `kubectl` (`get`, `describe`, `logs`, `top`, `rollout status`, ...), `git`, `docker`, `systemctl`, `helm` and `aws` (`describe-*`, `list-*`, `get-*`, `s3 ls`). Commands that would print secrets (`kubectl get secret`, `aws ... get-secret-value`) need approval. Pipes and `&&`, `||`, `;` between read-only commands are allowed. The full table is in [`src/runbook_agent/classifier.py`](src/runbook_agent/classifier.py).

## MCP server

```bash
runbook-agent serve ./runbooks            # dry-run: run_step reports what it would do
runbook-agent serve ./runbooks --execute  # read-only steps run; changes need approval
```

The server speaks JSON-RPC 2.0 over stdio (one message per line) and offers three tools:

- `list_runbooks`: runbooks in the directory with step counts.
- `get_runbook`: steps, commands and classifications for one runbook.
- `run_step`: run one step. A mutating step returns `"status": "blocked"` with the command a human should run:

```text
This step changes state and has not been approved. Ask a human to run: runbook-agent approve /srv/runbooks/hello.md 3
```

After a person runs `runbook-agent approve ...` in the same working directory as the server (or with the same `--state-dir`), the next `run_step` call for that step executes once and the approval is consumed.

To register it with an MCP client, point the client at the command `runbook-agent` with arguments `serve /path/to/runbooks`.

## Features

- [x] Parse Markdown runbooks into structured steps
- [x] Classify steps as read-only vs mutating (allowlist; unknown commands need approval)
- [x] Auto-run diagnostics, require approval for changes (dry-run by default)
- [x] Full audit log of what ran and why (JSON Lines)
- [x] MCP server so any agent client can use it (stdio transport, tools only)
- [ ] User-defined allowlist rules from a config file
- [ ] Redaction of secrets in captured output
- [ ] Variables and parameters passed into runbook steps
- [ ] HTTP transport for the MCP server

## Limitations

- The allowlist is built in. A read-only command it does not know (for example `gcloud ... list`) is treated as needing approval; there is no config file to extend it yet.
- "Read-only" means "does not change state", not "harmless to disclose". `cat` will happily print a sensitive file, and captured output (first 4000 characters per command) is written to the audit log unredacted.
- A GET request can still trigger side effects on a badly designed endpoint. `curl` GETs are treated as read-only.
- Commands are found through `PATH`. If `PATH` contains a hostile `df`, it will be run.
- Because auto-run commands do not go through a shell, diagnostics that use variables, globs or `2>&1` are classified `unknown` and wait for approval even when they are harmless.
- Following commands (`tail -f`, `kubectl logs -f`, `ping` without `-c`) are stopped by the per-command timeout rather than detected up front.
- The approval store is a file. It stops an agent that only talks to the MCP server; it is not a security boundary against a process that already has shell access to the same machine.
- `run` executes steps strictly in order with no branching; "if X then do Y" prose is not interpreted.
- The MCP server implements `initialize`, `ping`, `tools/list` and `tools/call` only, and has been tested with scripted JSON-RPC messages rather than against a specific client.

## Development

```bash
git clone https://github.com/narayana006/runbook-agent
cd runbook-agent
pip install -e ".[test]"
pytest
```

The test suite only ever executes harmless commands (`echo`, `tr`, `sleep`); everything else goes through fake runners.

## Contributing

Issues and pull requests are welcome. The most useful contributions are classifier cases: a command that was auto-run and should not have been (please report these first), or a common read-only diagnostic that is needlessly gated. Include the exact command line.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
