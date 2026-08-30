"""Measured-frame boundary for the Task 6 live color stage (mcp-complete-parity).

``FrameComparison`` is the STRICT typed readback the DRX handler accepts
from a frame-comparison port: the mean luma difference between one frame
of two renders plus each extracted frame's sha256 — measured from media
the pinned ffmpeg produced (raw gray extraction so the pinned bootstrap
ffmpeg needs no image encoder).
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import Field, field_validator

from services.contracts.primitives import StrictModel
from services.foundation_io import sha256_file
from services.qc.tools import load_qc_tools

#: What the handlers inject as ``LiveSessionContext.frame_diff``.
FrameDiffFn = Callable[[str, int, str, int], object]

_EXTRACT_TIMEOUT_SECONDS = 60.0
_DIFF_W = 320
_DIFF_H = 180


class FrameComparison(StrictModel):
    """One before/after frame pair measured from two rendered files."""

    mean_abs_diff: float
    a_sha256: str = Field(min_length=64, max_length=64, strict=True)
    b_sha256: str = Field(min_length=64, max_length=64, strict=True)

    @field_validator("mean_abs_diff")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"non-finite or negative frame diff: {value!r}")
        return value


def _gray_frame(ffmpeg: str, video: Path, frame: int, out: Path) -> None:
    subprocess.run(
        (
            ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            f"select='eq(n,{frame})'",
            "-frames:v",
            "1",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            str(out),
        ),
        capture_output=True,
        text=True,
        timeout=_EXTRACT_TIMEOUT_SECONDS,
        check=True,
    )


def _mean_abs_diff(a: Path, b: Path) -> float:
    data_a = a.read_bytes()
    data_b = b.read_bytes()
    return sum(abs(x - y) for x, y in zip(data_a, data_b, strict=True)) / max(len(data_a), 1)


def default_frame_comparison() -> FrameDiffFn:
    """Comparison port over the pinned QC stack (hash-verified binaries)."""

    ffmpeg = str(load_qc_tools().ffmpeg)

    def compare(path_a: str, frame_a: int, path_b: str, frame_b: int) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="t6-frame-") as tmp:
            gray_a = Path(tmp) / "a.gray"
            gray_b = Path(tmp) / "b.gray"
            _gray_frame(ffmpeg, Path(path_a), frame_a, gray_a)
            _gray_frame(ffmpeg, Path(path_b), frame_b, gray_b)
            return {
                "mean_abs_diff": _mean_abs_diff(gray_a, gray_b),
                "a_sha256": sha256_file(gray_a),
                "b_sha256": sha256_file(gray_b),
            }

    return compare


def parse_frame_comparison(payload: object) -> FrameComparison:
    """Boundary parse (``ValidationError`` propagates to the handler's
    typed refusal)."""

    row = dict(payload) if isinstance(payload, dict) else {"value": payload}
    return FrameComparison.model_validate(row)


__all__ = [
    "FrameComparison",
    "FrameDiffFn",
    "default_frame_comparison",
    "parse_frame_comparison",
]
