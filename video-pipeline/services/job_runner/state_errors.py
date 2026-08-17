"""Typed errors for the job runner runtime-state layer."""

from __future__ import annotations


class StateStoreError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


__all__ = ["StateStoreError"]
