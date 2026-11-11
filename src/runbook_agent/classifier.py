"""Decide whether a shell command is safe to run without a human.

The classifier is an allowlist. A command is ``read-only`` only when every
part of it is a known inspection command, used without flags that write, and
free of shell features (variables, substitution, globs, redirection) that
would need a shell to interpret. Everything else is ``mutating`` (known to
change something) or ``unknown`` (not recognised); both require approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READ_ONLY = "read-only"
MUTATING = "mutating"
UNKNOWN = "unknown"

OPERATORS = {"|", "||", "&&", ";"}


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str

    @property
    def needs_approval(self) -> bool:
        return self.kind != READ_ONLY


class Unsafe(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


@dataclass(frozen=True)
class Token:
    text: str
    op: bool = False


def lex(command: str) -> list[Token]:
    """Split a command into words and operators, refusing shell features.

    Raises ``Unsafe`` for anything that only a real shell could interpret, so
    a command that lexes cleanly can be executed directly as argv lists.
    """
    tokens: list[Token] = []
    word: list[str] | None = None
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal word
        if word is not None:
            tokens.append(Token("".join(word)))
            word = None

    while i < n:
        c = command[i]
        if c == "\n":
            raise Unsafe(UNKNOWN, "multi-line script")
        if c in " \t":
            flush()
        elif c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise Unsafe(UNKNOWN, "unterminated quote")
            word = (word or []) + [command[i + 1 : end]]
            i = end
        elif c == '"':
            word = word or []
            i += 1
            while i < n and command[i] != '"':
                if command[i] in "$`":
                    raise Unsafe(UNKNOWN, "uses shell variable or command substitution")
                if command[i] == "\\" and i + 1 < n and command[i + 1] in '"\\$`':
                    i += 1
                word.append(command[i])
                i += 1
            if i >= n:
                raise Unsafe(UNKNOWN, "unterminated quote")
        elif c == "\\":
            if i + 1 >= n:
                raise Unsafe(UNKNOWN, "trailing backslash")
            word = (word or []) + [command[i + 1]]
            i += 1
        elif c in "$`":
            raise Unsafe(UNKNOWN, "uses shell variable or command substitution")
        elif c in "*?[{":
            raise Unsafe(UNKNOWN, "uses shell globbing or brace expansion")
        elif c == "~" and word is None:
            raise Unsafe(UNKNOWN, "uses shell tilde expansion")
        elif c == "#" and word is None:
            break
        elif c == ">":
            raise Unsafe(MUTATING, "redirects output to a file")
        elif c in "<()":
            raise Unsafe(UNKNOWN, "uses shell redirection or a subshell")
        elif c in "|&;":
            flush()
            two = command[i : i + 2]
            if two in ("||", "&&"):
                tokens.append(Token(two, op=True))
                i += 1
            elif c == "&":
                raise Unsafe(UNKNOWN, "runs a background job")
            else:
                tokens.append(Token(c, op=True))
        else:
            word = (word or []) + [c]
        i += 1
    flush()
    return tokens


def split_segments(tokens: list[Token]) -> list[tuple[str | None, list[str]]]:
    """Group tokens into ``(connector, argv)`` pairs; connector is the operator before it."""
    segments: list[tuple[str | None, list[str]]] = []
    connector: str | None = None
    argv: list[str] = []
    for tok in tokens:
        if tok.op:
            if not argv:
                raise Unsafe(UNKNOWN, "empty command around operator")
            segments.append((connector, argv))
            connector, argv = tok.text, []
        else:
            argv.append(tok.text)
    if argv:
        segments.append((connector, argv))
    elif connector not in (None, ";"):
        raise Unsafe(UNKNOWN, "empty command around operator")
    return segments


@dataclass(frozen=True)
class Rule:
    """How one program may be used read-only."""

    subcommands: tuple[str, ...] = ()  # allowed "sub" or "sub sub2" prefixes; empty = any args
    prefixes: tuple[str, ...] = ()  # allowed prefixes of the operation word (aws describe-*)
    deny_short: str = ""  # single-letter flags that write, matched inside clusters
    deny_long: tuple[str, ...] = ()  # exact flags or --flag=value prefixes that write
    value_flags: tuple[str, ...] = ()  # global flags that take a value, skipped to find the subcommand
    max_positional: int | None = None
    skip_words: int = 0  # words before the operation (aws <service> <operation>)


_PLAIN = Rule()
RULES: dict[str, Rule] = {
    name: _PLAIN
    for name in (
        "echo printf cat ls head tail grep egrep fgrep wc cut tr df du free uptime whoami id "
        "uname ps pwd which stat file dig nslookup host ping nproc lsblk netstat ss jq true "
        "false sleep lsof vmstat iostat traceroute tracepath basename dirname nl tac"
    ).split()
}
RULES.update(
    {
        "sort": Rule(deny_short="o", deny_long=("--output",)),
        "uniq": Rule(max_positional=1),
        "hostname": Rule(max_positional=0, deny_short="bF", deny_long=("--file", "--boot")),
        "date": Rule(deny_short="s", deny_long=("--set",)),
        "dmesg": Rule(deny_short="cCDEn", deny_long=("--clear", "--read-clear", "--console")),
        "journalctl": Rule(deny_long=("--vacuum", "--rotate", "--flush", "--sync", "--relinquish", "--setup-keys")),
        "find": Rule(deny_long=("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls")),
        "curl": Rule(
            deny_short="dFToOKXcD",
            deny_long=("--data", "--form", "--upload-file", "--output", "--remote-name", "--config",
                       "--request", "--json", "--cookie-jar", "--dump-header", "--trace", "--stderr",
                       "--libcurl", "--hsts", "--alt-svc", "--netrc-file", "--etag-save"),
        ),
        "kubectl": Rule(
            subcommands=("get", "describe", "logs", "top", "version", "explain", "api-resources",
                         "api-versions", "cluster-info", "events", "rollout status", "rollout history",
                         "config view", "config get-contexts", "config current-context",
                         "auth can-i", "auth whoami"),
            value_flags=("-n", "--namespace", "--context", "--kubeconfig", "--cluster", "-s", "--server", "--user"),
        ),
        "git": Rule(
            subcommands=("status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "blame", "shortlog"),
            deny_long=("--output", "--ext-diff"),
            value_flags=("-C", "--git-dir", "--work-tree"),
        ),
        "docker": Rule(
            subcommands=("ps", "logs", "inspect", "images", "stats", "top", "version", "info", "port",
                         "diff", "history", "compose ps", "compose logs", "compose top"),
            value_flags=("-H", "--host", "--context", "-c"),
        ),
        "systemctl": Rule(
            subcommands=("status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
                         "list-timers", "list-dependencies", "show", "cat"),
        ),
        "helm": Rule(
            subcommands=("list", "ls", "status", "history", "version"),
            value_flags=("-n", "--namespace", "--kube-context", "--kubeconfig"),
        ),
        "aws": Rule(
            prefixes=("describe-", "list-", "get-", "ls"),
            skip_words=1,
            deny_long=("--with-decryption",),
            value_flags=("--region", "--profile", "--output", "--endpoint-url", "--query"),
        ),
    }
)

_SECRET_WORDS = re.compile(r"secret|password|token|credential|login", re.I)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _positionals(args: list[str], value_flags: tuple[str, ...]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in value_flags:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def _check_flags(prog: str, args: list[str], rule: Rule) -> None:
    for i, a in enumerate(args):
        if prog == "curl" and a in ("-X", "--request") and i + 1 < len(args):
            if args[i + 1].upper() in ("GET", "HEAD"):
                continue
            raise Unsafe(MUTATING, f"curl sends a {args[i + 1].upper()} request")
        for long in rule.deny_long:
            # "--flag" also matches "--flag=value" and variants such as "--data-raw".
            if a == long or (long.startswith("--") and a.startswith(long)):
                raise Unsafe(MUTATING, f"{prog} {a.split('=')[0]} can write or change state")
        if rule.deny_short and a.startswith("-") and not a.startswith("--"):
            hit = next((ch for ch in a[1:] if ch in rule.deny_short), None)
            if hit:
                raise Unsafe(MUTATING, f"{prog} -{hit} can write or change state")


def classify_argv(argv: list[str]) -> None:
    """Raise ``Unsafe`` unless this single command is on the read-only allowlist."""
    prog, args = argv[0], argv[1:]
    if _ENV_ASSIGN.match(prog):
        raise Unsafe(UNKNOWN, "sets environment variables for the command")
    if prog == "sudo":
        raise Unsafe(MUTATING, "runs with elevated privileges (sudo)")
    if "/" in prog:
        raise Unsafe(UNKNOWN, f"{prog} is called by path, not a known command name")
    rule = RULES.get(prog)
    if rule is None:
        raise Unsafe(UNKNOWN, f"{prog} is not on the read-only allowlist")
    _check_flags(prog, args, rule)
    words = _positionals(args, rule.value_flags)
    if rule.max_positional is not None and len(words) > rule.max_positional:
        raise Unsafe(MUTATING, f"{prog} with these arguments can write or change state")
    if rule.prefixes:
        op = words[rule.skip_words] if len(words) > rule.skip_words else ""
        if not op.startswith(rule.prefixes):
            raise Unsafe(MUTATING, f"{prog} {' '.join(words[: rule.skip_words + 1])} is not a read-only operation")
        if _SECRET_WORDS.search(op):
            raise Unsafe(UNKNOWN, f"{prog} {op} may print secret values")
    if rule.subcommands:
        allowed = (sub.split() for sub in rule.subcommands)
        if not any(words[: len(parts)] == parts for parts in allowed):
            nested = any(sub.startswith(words[0] + " ") for sub in rule.subcommands) if words else False
            shown = " ".join([prog, *words[: 2 if nested else 1]])
            raise Unsafe(MUTATING, f"{shown} is not a read-only subcommand")
        if prog == "kubectl" and words[0] in ("get", "describe") and any(
            re.match(r"secrets?([./,]|$)", w) for w in words[1:]
        ):
            raise Unsafe(UNKNOWN, "kubectl may print secret values")


def parse_command(command: str) -> list[tuple[str | None, list[str]]]:
    """Lex and validate a command; return its segments or raise ``Unsafe``."""
    segments = split_segments(lex(command))
    if not segments:
        raise Unsafe(UNKNOWN, "empty command")
    for _, argv in segments:
        classify_argv(argv)
    return segments


def classify(command: str) -> Verdict:
    try:
        parse_command(command)
    except Unsafe as exc:
        return Verdict(exc.kind, exc.reason)
    return Verdict(READ_ONLY, "only read-only commands, no shell features")


def classify_step(commands: list[str]) -> Verdict:
    """A step is read-only only if every one of its commands is."""
    verdicts = [classify(c) for c in commands]
    for kind in (MUTATING, UNKNOWN):
        for v in verdicts:
            if v.kind == kind:
                return v
    return Verdict(READ_ONLY, "all commands are read-only")
