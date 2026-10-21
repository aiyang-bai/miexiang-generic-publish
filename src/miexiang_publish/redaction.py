from __future__ import annotations

import re


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(secret[_ -]?(?:id|key)|session[_ -]?token|access[_ -]?token|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|authorization\s*:\s*bearer)\s*([=: ]\s*)?[^\s,;\"']+"
)


def redact_text(value: str) -> str:
    """Remove common provider secret values before diagnostics leave the process."""
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
