"""One-time, expiring approvals recorded by a human for a specific step.

An approval is bound to a fingerprint of the runbook name, step number and the
exact command text, so editing the step after approval invalidates it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_TTL = 15 * 60


class ApprovalStore:
    def __init__(self, state_dir: str | Path, ttl: float = DEFAULT_TTL, clock=time.time):
        self.path = Path(state_dir) / "approvals.json"
        self.ttl = ttl
        self._clock = clock

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def grant(self, fingerprint: str, approver: str) -> None:
        data = self._load()
        data[fingerprint] = {"approver": approver, "expires": self._clock() + self.ttl}
        self._save(data)

    def consume(self, fingerprint: str) -> str | None:
        """Return the approver and delete the approval, or None if absent or expired."""
        data = self._load()
        entry = data.pop(fingerprint, None)
        if entry is None:
            return None
        self._save(data)
        if entry["expires"] < self._clock():
            return None
        return entry["approver"]
