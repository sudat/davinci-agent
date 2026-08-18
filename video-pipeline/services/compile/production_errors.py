"""Typed error surface for the production Timeline IR compiler (Todo 44).

Every refusal carries an explicit machine-readable code so callers can route
failures without string matching (mirrors the 0C ``CompileError`` precedent
but with structured codes).
"""

from __future__ import annotations

from typing import Literal

CompileErrorCode = Literal[
    "unresolved_anchor",
    "record_overlap",
    "sample_rate_mismatch",
    "sample_conversion_lossy",
    "source_extent_lossy",
]


class CompileProductionError(Exception):
    """A production compile was refused; nothing was emitted."""

    def __init__(self, code: CompileErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code: CompileErrorCode = code
        self.detail = detail


__all__ = ["CompileErrorCode", "CompileProductionError"]
