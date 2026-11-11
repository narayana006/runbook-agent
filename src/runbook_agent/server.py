"""Minimal MCP server (JSON-RPC 2.0 over stdio, one message per line).

It exposes three tools: ``list_runbooks``, ``get_runbook`` and ``run_step``.
There is deliberately no tool that grants approval: a mutating step only runs
after a human records a one-time approval with ``runbook-agent approve``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import IO

from . import __version__
from .approvals import ApprovalStore
from .classifier import classify, classify_step
from .executor import AuditLog, Executor
from .parser import Runbook, parse_file

PROTOCOL_VERSION = "2024-11-05"

_RUNBOOK_ARG = {"runbook": {"type": "string", "description": "Runbook file name, e.g. disk-full.md"}}
TOOLS = [
    {
        "name": "list_runbooks",
        "description": "List the runbooks in the served directory with their step counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_runbook",
        "description": "Return a runbook's steps, commands and read-only/mutating classification.",
        "inputSchema": {"type": "object", "properties": _RUNBOOK_ARG, "required": ["runbook"]},
    },
    {
        "name": "run_step",
        "description": (
            "Run one step. Read-only steps run immediately. Mutating steps run only if a human "
            "has approved that exact step out of band; otherwise the result says approval is required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {**_RUNBOOK_ARG, "step": {"type": "integer", "description": "Step number"}},
            "required": ["runbook", "step"],
        },
    },
]


class ToolError(Exception):
    pass


class RunbookServer:
    def __init__(self, directory: str | Path, *, execute: bool = False, store: ApprovalStore | None = None,
                 audit: AuditLog | None = None, timeout: float = 30.0, executor_factory=Executor,
                 rules: dict | None = None):
        self.directory = Path(directory).resolve()
        self.execute = execute
        self.store = store
        self.audit = audit or AuditLog(None)
        self.timeout = timeout
        self._executor_factory = executor_factory
        self.rules = rules

    # -- tools ---------------------------------------------------------
    def _load(self, name: str) -> Runbook:
        path = (self.directory / str(name)).resolve()
        if path.parent != self.directory or path.suffix != ".md" or not path.is_file():
            raise ToolError(f"unknown runbook: {name}")
        return parse_file(path)

    def list_runbooks(self, _args: dict) -> list[dict]:
        out = []
        for path in sorted(self.directory.glob("*.md")):
            rb = parse_file(path)
            gated = sum(classify_step(s.commands, self.rules).needs_approval for s in rb.steps)
            out.append({"runbook": rb.name, "title": rb.title, "steps": len(rb.steps),
                        "steps_needing_approval": gated})
        return out

    def get_runbook(self, args: dict) -> dict:
        rb = self._load(args.get("runbook", ""))
        steps = []
        for s in rb.steps:
            v = classify_step(s.commands, self.rules)
            steps.append({
                "step": s.number, "title": s.title, "section": s.section,
                "classification": v.kind, "needs_approval": v.needs_approval,
                "commands": [{"command": c, **asdict(classify(c, self.rules))} for c in s.commands],
            })
        return {"runbook": rb.name, "title": rb.title, "steps": steps}

    def run_step(self, args: dict) -> dict:
        rb = self._load(args.get("runbook", ""))
        step = next((s for s in rb.steps if s.number == args.get("step")), None)
        if step is None:
            raise ToolError(f"{rb.name} has no step {args.get('step')!r}")

        def approver(s):
            return self.store.consume(s.fingerprint(rb.name)) if self.store else None

        executor = self._executor_factory(rb, execute=self.execute, approver=approver,
                                          audit=self.audit, timeout=self.timeout, rules=self.rules)
        outcomes, _ = executor.run_step(step)
        actions = {o.action for o in outcomes}
        result = {"runbook": rb.name, "step": step.number, "title": step.title,
                  "status": "blocked" if "blocked" in actions else "dry-run" if "dry-run" in actions else "ran",
                  "commands": [asdict(o) for o in outcomes]}
        if result["status"] == "blocked":
            result["message"] = (
                "This step changes state and has not been approved. Ask a human to run: "
                f"runbook-agent approve {self.directory / rb.name} {step.number}"
            )
        elif result["status"] == "dry-run":
            result["message"] = "Server is in dry-run mode (started without --execute); nothing was run."
        return result

    # -- protocol ------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            return None  # notifications get no reply
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "runbook-agent", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(msg.get("params") or {})
            else:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # keep the server alive on unexpected errors
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call(self, params: dict) -> dict:
        tools = {"list_runbooks": self.list_runbooks, "get_runbook": self.get_runbook, "run_step": self.run_step}
        try:
            fn = tools.get(params.get("name"))
            if fn is None:
                raise ToolError(f"unknown tool: {params.get('name')}")
            payload, is_error = fn(params.get("arguments") or {}), False
        except ToolError as exc:
            payload, is_error = {"error": str(exc)}, True
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": is_error}

    def serve(self, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
