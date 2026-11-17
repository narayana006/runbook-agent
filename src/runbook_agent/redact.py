"""Mask secrets in captured command output before it is printed, logged or returned.

Redaction is pattern based: it catches well-known token formats and values
assigned to secret-looking names. It cannot recognise a secret that looks like
ordinary text, so it reduces exposure rather than guaranteeing none.
"""

from __future__ import annotations

import re

MASK = "[REDACTED:{}]"

_SECRET_NAME = (
    r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|credentials?)[A-Za-z0-9_.-]*"
)

# (label, pattern, group to mask). Group 0 masks the whole match. Order matters:
# whole-token formats run before the generic name=value rule so they keep their label.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL), 0),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b"), 0),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), 0),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), 0),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), 0),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    # Authorization: Bearer abc / Basic abc / bare value. The scheme is kept.
    ("auth-header", re.compile(
        r"(?im)\b(?:proxy-)?authorization\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?([^\s\"',]+)"), 1),
    # scheme://user:password@host keeps the user name.
    ("url-password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@", re.IGNORECASE), 1),
    # password=..., "api_key": "...", TOKEN: ..., --password xyz is not covered (no separator).
    ("secret-value", re.compile(
        r"(?i)([\"']?\b" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^\s\"',;}]+))"), -1),
]


def redact(text: str) -> tuple[str, int]:
    """Return ``text`` with secrets masked, and how many values were masked."""
    if not text:
        return text, 0
    count = 0
    for label, pattern, group in _PATTERNS:
        mask = MASK.format(label)

        def sub(m: re.Match[str], group: int = group, mask: str = mask) -> str:
            nonlocal count
            # name=value: mask whichever value group matched, keeping any quotes.
            idx = next(i for i in (2, 3, 4) if m.group(i) is not None) if group == -1 else group
            if m.group(idx).startswith("[REDACTED:"):
                return m.group(0)  # already masked by a more specific pattern
            count += 1
            whole, offset = m.group(0), m.start(0)
            return whole[:m.start(idx) - offset] + mask + whole[m.end(idx) - offset:]

        text = pattern.sub(sub, text)
    return text, count
