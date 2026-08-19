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
PHASE3_SCOPE = "phase3-scope-rejected"
PRESENTATION_INVALID = "presentation-invalid"
UNAPPROVED_ASSET = "unapproved-asset"
AUDIO_ROLE_MISSING = "audio-role-missing"
AUDIO_ROLE_CONFLATION = "audio-role-conflation"
SUBTITLE_UNSAFE_AREA = "subtitle-unsafe-area"
RENDER_PRESET_MISMATCH = "render-preset-mismatch"
STYLED_PRESENTATION_DRIFT = "styled-presentation-drift"


__all__ = [
    "AUDIO_ROLE_CONFLATION",
    "AUDIO_ROLE_MISSING",
    "CAPABILITY_MATRIX_STALE",
    "CUE_TIMING_INEXACT",
    "MATRIX_EVIDENCE_MISSING",
    "MEDIA_BINDING_MISSING",
    "MEDIA_EXTENT_OVERFLOW",
    "MEDIA_HASH_DRIFT",
    "PHASE3_SCOPE",
    "PRESENTATION_INVALID",
    "RENDER_PRESET_MISMATCH",
    "RESOLVE_FIELD_IN_IR",
    "STALE_IR",
    "STYLED_PRESENTATION_DRIFT",
    "SUBTITLE_UNSAFE_AREA",
    "UNAPPROVED_ASSET",
    "UNSUPPORTED_RETIME",
    "UNSUPPORTED_TRANSITION",
    "WRONG_TRACK_MAP",
    "PackageCompileError",
]
