"""Typed Resolve-package compilation failures (fail-closed before any build)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PackageCompileError(Exception):
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


CAPABILITY_MATRIX_STALE = "capability-matrix-stale"
MATRIX_EVIDENCE_MISSING = "matrix-evidence-missing"
UNSUPPORTED_RETIME = "unsupported-retime"
UNSUPPORTED_TRANSITION = "unsupported-transition"
WRONG_TRACK_MAP = "wrong-track-map"
STALE_IR = "stale-ir"
RESOLVE_FIELD_IN_IR = "resolve-field-in-ir"
MEDIA_HASH_DRIFT = "media-hash-drift"
MEDIA_EXTENT_OVERFLOW = "media-extent-overflow"
MEDIA_BINDING_MISSING = "media-binding-missing"
CUE_TIMING_INEXACT = "cue-timing-inexact"


__all__ = [
    "CAPABILITY_MATRIX_STALE",
    "CUE_TIMING_INEXACT",
    "MATRIX_EVIDENCE_MISSING",
    "MEDIA_BINDING_MISSING",
    "MEDIA_EXTENT_OVERFLOW",
    "MEDIA_HASH_DRIFT",
    "RESOLVE_FIELD_IN_IR",
    "STALE_IR",
    "UNSUPPORTED_RETIME",
    "UNSUPPORTED_TRANSITION",
    "WRONG_TRACK_MAP",
    "PackageCompileError",
]
