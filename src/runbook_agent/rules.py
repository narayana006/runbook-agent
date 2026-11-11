"""Load user-defined allowlist rules from a JSON config file.

The file can add read-only programs, extend the built-in rule for a program
(more read-only subcommands, more flags that write), or remove programs from
the allowlist. It is only ever read when passed explicitly with ``--rules``,
so a file sitting in a cloned repository cannot loosen the classifier.

Example::

    {
      "allow": {
        "gcloud": {"subcommands": ["compute instances list", "config list"],
                   "value_flags": ["--project"]},
        "kubectl": {"subcommands": ["get-contexts"]}
      },
      "deny": ["ping"]
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .classifier import RULES, Rule

# Programs whose job is to run other programs. Allowing any of them would let a
# "read-only" command execute arbitrary code, so the config may never add them.
NEVER_ALLOW = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "env", "xargs", "sudo", "su", "doas", "nohup",
    "timeout", "nice", "ionice", "exec", "eval", "command", "builtin", "time", "watch", "strace", "ltrace",
    "chroot", "setsid", "runuser", "python", "python3", "perl", "ruby", "node", "php", "lua", "awk", "gawk",
    "sed", "ssh", "scp", "rsync"
})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_LIST_FIELDS = ("subcommands", "prefixes", "deny_long", "value_flags")
_FIELDS = {*_LIST_FIELDS, "deny_short", "max_positional", "skip_words"}


class ConfigError(ValueError):
    pass


def _str_list(prog: str, key: str, value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"allow.{prog}.{key} must be a list of non-empty strings")
    return tuple(" ".join(v.split()) for v in value)


def _parse_rule(prog: str, spec) -> dict:
    if not isinstance(spec, dict):
        raise ConfigError(f"allow.{prog} must be an object")
    extra = set(spec) - _FIELDS
    if extra:
        raise ConfigError(f"allow.{prog}: unknown field(s) {', '.join(sorted(extra))}")
    out: dict = {}
    for key in _LIST_FIELDS:
        if key in spec:
            out[key] = _str_list(prog, key, spec[key])
    if "deny_short" in spec:
        v = spec["deny_short"]
        if not isinstance(v, str) or not v.isalnum():
            raise ConfigError(f"allow.{prog}.deny_short must be a string of flag letters, e.g. \"fo\"")
        out["deny_short"] = v
    for key in ("max_positional", "skip_words"):
        if key in spec:
            v = spec[key]
            if v is None and key == "max_positional":
                out[key] = None
                continue
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ConfigError(f"allow.{prog}.{key} must be a non-negative integer")
            out[key] = v
    return out


def _merge(prog: str, base: Rule, fields: dict) -> Rule:
    """Extend a built-in rule. Restrictions are added; the built-in ones are kept."""
    fixed = {"prefixes", "max_positional", "skip_words"} & set(fields)
    if fixed:
        raise ConfigError(f"allow.{prog}: {', '.join(sorted(fixed))} cannot be changed for a built-in program")
    if "subcommands" in fields and not base.subcommands:
        raise ConfigError(
            f"allow.{prog}: the built-in rule already allows any arguments; "
            "use deny_short/deny_long to restrict it, or deny to remove it"
        )
    return replace(
        base,
        subcommands=base.subcommands + tuple(s for s in fields.get("subcommands", ()) if s not in base.subcommands),
        deny_short=base.deny_short + "".join(c for c in fields.get("deny_short", "") if c not in base.deny_short),
        deny_long=base.deny_long + tuple(f for f in fields.get("deny_long", ()) if f not in base.deny_long),
        value_flags=base.value_flags + tuple(f for f in fields.get("value_flags", ()) if f not in base.value_flags),
    )


def build_rules(config: dict, base: dict[str, Rule] | None = None) -> dict[str, Rule]:
    """Return a new rule table: ``base`` (the built-ins) plus the config's changes."""
    if not isinstance(config, dict):
        raise ConfigError("rules config must be a JSON object")
    extra = set(config) - {"allow", "deny"}
    if extra:
        raise ConfigError(f"unknown top-level field(s): {', '.join(sorted(extra))}")
    rules = dict(RULES if base is None else base)
    allow = config.get("allow", {})
    if not isinstance(allow, dict):
        raise ConfigError("allow must be an object mapping program names to rules")
    deny = config.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ConfigError("deny must be a list of program names")
    both = set(allow) & set(deny)
    if both:
        raise ConfigError(f"listed in both allow and deny: {', '.join(sorted(both))}")
    for prog, spec in allow.items():
        if not _NAME.match(prog):
            raise ConfigError(f"allow: {prog!r} is not a plain command name")
        if prog in NEVER_ALLOW:
            raise ConfigError(f"allow: {prog} can run arbitrary commands and can never be auto-run")
        fields = _parse_rule(prog, spec)
        rules[prog] = _merge(prog, rules[prog], fields) if prog in rules else Rule(**fields)
    for prog in deny:
        rules.pop(prog, None)
    return rules


def load_rules(path: str | Path | None) -> dict[str, Rule]:
    """Load a rules file, or return the built-in table when ``path`` is None."""
    if path is None:
        return dict(RULES)
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    try:
        return build_rules(config)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
