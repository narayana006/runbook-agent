from runbook_agent.parser import extract_commands, parse_text

DOC = """# Restart the widget service

Intro text.

## Diagnose

1. Check the **service** status:

   ```bash
   # a comment
   echo status
   echo one \\
     two
   ```

Sample output, not a step:

```
all good
```

```json
{"not": "shell"}
```

## Fix

- Restart it

```console
$ echo restarting
restarting
$ echo done
```
"""


def test_steps_titles_and_sections():
    rb = parse_text(DOC, name="widget.md")
    assert rb.title == "Restart the widget service"
    assert [s.number for s in rb.steps] == [1, 2]
    first, second = rb.steps
    assert first.title == "Check the service status"
    assert first.section == "Diagnose"
    assert first.commands == ["echo status", "echo one two"]
    assert second.title == "Restart it"
    assert second.section == "Fix"


def test_console_blocks_keep_only_prompt_lines():
    assert parse_text(DOC).steps[1].commands == ["echo restarting", "echo done"]


def test_non_shell_blocks_are_ignored():
    rb = parse_text("# T\n\n```python\nprint('hi')\n```\n\n```\necho untagged\n```\n")
    assert rb.steps == []


def test_title_falls_back_to_heading_then_step_number():
    rb = parse_text("## Section A\n\n```sh\necho a\n```\n```sh\necho b\n```\n")
    assert [s.title for s in rb.steps] == ["Section A", "Section A"]
    assert parse_text("```sh\necho a\n```\n").steps[0].title == "Step 1"


def test_heredoc_and_control_flow_stay_one_script():
    block = ["cat <<EOF", "echo not a separate command", "EOF"]
    assert extract_commands(block, "bash") == ["\n".join(block)]
    loop = ["for i in 1 2; do", "  echo $i", "done"]
    assert len(extract_commands(loop, "bash")) == 1


def test_tilde_fences_and_empty_blocks():
    rb = parse_text("~~~bash\necho tilde\n~~~\n\n```bash\n# only a comment\n```\n")
    assert [s.commands for s in rb.steps] == [["echo tilde"]]


def test_fingerprint_changes_with_command_text():
    a = parse_text("```sh\necho a\n```\n").steps[0]
    b = parse_text("```sh\necho b\n```\n").steps[0]
    assert a.fingerprint("x.md") != b.fingerprint("x.md")
    assert a.fingerprint("x.md") != a.fingerprint("y.md")
    assert a.fingerprint("x.md") == parse_text("```sh\necho a\n```\n").steps[0].fingerprint("x.md")
