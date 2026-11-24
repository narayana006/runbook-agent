"""Parse a Markdown runbook into ordered steps.

A step is one fenced shell code block. Its title is the closest prose line
(usually a numbered list item) above the block, falling back to the heading.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SHELL_LANGS = {"bash", "sh", "shell", "zsh", "console", "shell-session"}
PROMPT_LANGS = {"console", "shell-session"}

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w-]*)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SCRIPT_LINE = re.compile(r"^\s*(if|for|while|until|case|function)\b|<<|(\bdo|\bthen|\{)\s*$")


@dataclass
class Step:
    number: int
    title: str
    section: str
    commands: list[str] = field(default_factory=list)
    line: int = 0

    def fingerprint(self, runbook: str) -> str:
        """Stable identity of this step's exact commands, used to bind approvals."""
        payload = "\0".join([runbook, str(self.number), *self.commands])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Runbook:
    name: str
    title: str
    steps: list[Step]
    # Placeholders found in shell blocks: name -> default value (None = required).
    parameters: dict[str, str | None] = field(default_factory=dict)


class ParamError(ValueError):
    """A runbook parameter is missing or has an unsafe value."""


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=([^{}]*?))?\s*\}\}")
# Values are spliced into command text, so only plain tokens are accepted: no
# whitespace, quotes, shell metacharacters, and no leading "-" (flag injection).
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/@+,=%-]*$")


def check_value(name: str, value: str) -> str:
    if not _SAFE_VALUE.match(value):
        raise ParamError(
            f"parameter {name}: {value!r} is not allowed (use letters, digits and . _ : / @ + , = % -; "
            "no spaces, quotes or shell characters, and it cannot start with '-')"
        )
    return value


def _substitute(text: str, params: dict[str, str] | None, found: dict[str, str | None]) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        found.setdefault(name, default)
        if params is None:
            return m.group(0)
        if name in params:
            return check_value(name, params[name])
        if default is not None:
            return check_value(name, default)
        raise ParamError(f"missing parameter {name} (pass --param {name}=VALUE)")

    return _PLACEHOLDER.sub(repl, text)


def _clean(text: str) -> str:
    text = _LIST_MARKER.sub("", text.strip())
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]+", "", text)
    return text.strip().rstrip(":").strip()


def extract_commands(lines: list[str], lang: str) -> list[str]:
    """Turn the body of a shell code block into individual command strings."""
    stripped = [ln.strip() for ln in lines]
    if lang in PROMPT_LANGS or any(ln.startswith("$ ") for ln in stripped):
        # Transcript style: only "$ " lines are commands, the rest is sample output.
        return [ln[2:].strip() for ln in stripped if ln.startswith("$ ") and ln[2:].strip()]
    body = [ln for ln in stripped if ln and not ln.startswith("#")]
    if any(_SCRIPT_LINE.search(ln) for ln in body):
        # Heredocs and control flow cannot be split per line; keep the block
        # whole so it is treated as one script that needs approval.
        return ["\n".join(ln.rstrip() for ln in lines if ln.strip())]
    commands, pending = [], ""
    for ln in body:
        if ln.endswith("\\"):
            pending += ln[:-1].rstrip() + " "
            continue
        commands.append(pending + ln)
        pending = ""
    if pending.strip():
        commands.append(pending.strip())
    return commands


def parse_text(text: str, name: str = "runbook", params: dict[str, str] | None = None) -> Runbook:
    """Parse runbook text. ``{{name}}`` / ``{{name=default}}`` placeholders inside shell
    blocks are replaced from ``params``; with ``params=None`` they are left as written."""
    found: dict[str, str | None] = {}
    steps: list[Step] = []
    title = ""
    section = ""
    last_prose = ""
    fence = None  # (marker, lang, start_line, body)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if fence is not None:
            marker, lang, start, body = fence
            if raw.strip().startswith(marker) and not raw.strip().strip(marker[0]):
                fence = None
                if lang in SHELL_LANGS:
                    commands = [_substitute(c, params, found) for c in extract_commands(body, lang)]
                    if commands:
                        number = len(steps) + 1
                        steps.append(
                            Step(number, _substitute(last_prose, params, {}) or section or f"Step {number}",
                                 section, commands, start)
                        )
                last_prose = ""
            else:
                body.append(raw)
            continue
        m = _FENCE.match(raw)
        if m:
            fence = (m.group(1), m.group(2).lower(), lineno, [])
            continue
        h = _HEADING.match(raw)
        if h:
            section = _clean(h.group(1))
            title = title or section
            last_prose = ""
            continue
        if raw.strip():
            last_prose = _clean(raw) or last_prose
    return Runbook(name=name, title=title or name, steps=steps, parameters=found)


def parse_file(path: str | Path, params: dict[str, str] | None = None) -> Runbook:
    path = Path(path)
    return parse_text(path.read_text(encoding="utf-8"), name=path.name, params=params)
