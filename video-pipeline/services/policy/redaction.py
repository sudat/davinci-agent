"""Deterministic secret/PII redaction for anything heading into logs or audits.

Recursive over JSON-shaped objects: keys matching the secret patterns
(api_key/token/credential/password/secret) have their whole value replaced by
``[REDACTED:<key>]``; free-text values have emails and long digit runs masked.
Pure regex — deterministic, canonical-JSON-safe (no floats, no randomness).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

SECRET_KEY_RE: Final = re.compile(r"api_key|token|credential|password|secret", re.IGNORECASE)
SECRET_PARAM_RE: Final = re.compile(r"(?i)(api_key|token|password|secret|credential)=([^&\s\"']+)")
EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LONG_DIGIT_RE: Final = re.compile(r"\d{7,}")


def redact(obj: object) -> object:
    """Recursively redact secrets and PII from a JSON-shaped structure."""

    if isinstance(obj, Mapping):
        redacted: dict[object, object] = {}
        for key, value in obj.items():
            if isinstance(key, str) and SECRET_KEY_RE.search(key):
                redacted[key] = f"[REDACTED:{key}]"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(obj, list | tuple):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_text(text: str) -> str:
    """Mask secret query params, emails, and long digit runs in one string."""

    masked = SECRET_PARAM_RE.sub(r"\1=[REDACTED]", text)
    masked = EMAIL_RE.sub("[EMAIL_REDACTED]", masked)
    return LONG_DIGIT_RE.sub(_mask_digit_run, masked)


def _mask_digit_run(match: re.Match[str]) -> str:
    digits = match.group()
    return "x" * (len(digits) - 4) + digits[-4:]


__all__ = ["redact", "redact_text"]
