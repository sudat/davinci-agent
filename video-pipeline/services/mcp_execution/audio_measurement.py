"""Measured-audio boundary for the Task 5 live audio stages (mcp-complete-parity).

``MeasuredAudio`` is the STRICT typed readback every audio-stage handler
accepts from a measurement port: values measured from Resolve-rendered
media only, each row bound to the measured file's name and sha256. The
default port wires the pinned QC stack (``qc.checks.audio_checks`` +
``qc.tools``); a malformed or non-finite port payload fails validation at
this boundary and never reaches a product readback.
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import Field, field_validator

from services.contracts.primitives import StrictModel
from services.foundation_io import sha256_file
from services.qc.checks.audio_checks import measure_rendered_audio
from services.qc.tools import load_qc_tools

#: What the handlers inject as ``LiveSessionContext.audio_measure``.
AudioMeasureFn = Callable[[str], object]


class MeasuredAudio(StrictModel):
    """One rendered-media measurement row; every value is finite and
    media-sourced — a plan target echoed here cannot validate."""

    integrated_loudness_lufs: float
    true_peak_dbtp: float
    channels: int = Field(ge=1, strict=True)
    peak_mb: int = Field(strict=True)
    floor_mb: int = Field(strict=True)
    max_silence_ms: int = Field(ge=0, strict=True)
    media_name: str = Field(min_length=1, strict=True)
    media_sha256: str = Field(min_length=64, max_length=64, strict=True)

    @field_validator("integrated_loudness_lufs", "true_peak_dbtp")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError(f"non-finite measured value: {value!r}")
        return value


def default_audio_measurement() -> AudioMeasureFn:
    """Measurement port over the pinned QC stack (hash-verified binaries)."""

    tools = load_qc_tools()

    def measure(media_path: str) -> dict[str, object]:
        render = Path(media_path)
        with tempfile.TemporaryDirectory(prefix="t5-audio-measure-") as tmp:
            values = measure_rendered_audio(tools, render, Path(tmp))
        return {
            "integrated_loudness_lufs": values.integrated_loudness_lufs,
            "true_peak_dbtp": values.true_peak_dbtp,
            "channels": values.channels,
            "peak_mb": values.peak_mb,
            "floor_mb": values.floor_mb,
            "max_silence_ms": values.max_silence_ms,
        }

    return measure


def parse_measured(payload: object, media: Path) -> MeasuredAudio:
    """Boundary parse: port payload + the measured file's own identity
    (``ValidationError`` propagates to the handler's typed refusal)."""

    row = dict(payload) if isinstance(payload, dict) else {"value": payload}
    return MeasuredAudio.model_validate(
        {**row, "media_name": media.name, "media_sha256": sha256_file(media)}
    )


__all__ = [
    "AudioMeasureFn",
    "MeasuredAudio",
    "default_audio_measurement",
    "parse_measured",
]

