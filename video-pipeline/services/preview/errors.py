"""Structured preview failure types (re-exported from :mod:`services.preview.models`)."""

from __future__ import annotations


class PreviewError(Exception):
    """Base class for structured preview failures; the adapter never fails silently."""


class PreviewToolchainError(PreviewError):
    """A pinned binary is missing or its sha256 drifted from the frozen lock."""


class PreviewLayoutError(PreviewError):
    """The Timeline IR does not form a contiguous, renderable record layout."""


class PreviewBindingError(PreviewError):
    """A media binding is missing, its file vanished, or its hash drifted."""


class PreviewRenderError(PreviewError):
    """The bounded ffmpeg render command failed or produced no output file."""


class PreviewVerificationError(PreviewError):
    """ffprobe assertions on the produced preview failed (drift detection)."""


class PreviewTraceError(PreviewError):
    """The trace manifest could not be assembled with full record coverage."""


__all__ = [
    "PreviewBindingError",
    "PreviewError",
    "PreviewLayoutError",
    "PreviewRenderError",
    "PreviewToolchainError",
    "PreviewTraceError",
    "PreviewVerificationError",
]
