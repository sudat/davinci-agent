"""Typed errors for retention and garbage collection.

Every refusal carries a stable ``code`` (matched by tests and the CLI)
plus a human ``detail``; the CLI maps any ``RetentionError`` to a typed
nonzero exit instead of a traceback.
"""

from __future__ import annotations


class RetentionError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


__all__ = ["RetentionError"]
