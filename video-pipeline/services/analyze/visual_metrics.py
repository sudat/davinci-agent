"""Deterministic per-frame luma metrics for the minimum visual checks.

Pure Python (stdlib only) integer math over the decoded raw gray bytes:
means/diffs/fractions are integer permille of the 8-bit full scale 255 and
blur energy is the integer mean-square Laplacian over interior pixels
(exactly 0 on any flat frame). No numpy, no cv2 — by project constraint the
analyzer stack keeps zero new dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from services.analyze.visual_constants import (
    BLACK_MAX_MEAN_M,
    BLACK_MIN_PIXEL_FRACTION_M,
    BLACK_PIXEL_MAX_SAMPLE,
    CLIPPED_HIGH_SAMPLE,
    DECODE_H,
    DECODE_W,
    EXPOSURE_HIGH_MEAN_M,
    EXPOSURE_LOW_MEAN_M,
    OVER_MIN_CLIPPED_FRACTION_M,
)
from services.analyze.visual_models import FrameFact

if TYPE_CHECKING:
    from services.analyze.visual_decode import LumaFrame

_FULL_SCALE: int = 255
_PIXELS: int = DECODE_W * DECODE_H
_INTERIOR: int = (DECODE_W - 2) * (DECODE_H - 2)
Direction = Literal["over", "under"]
DIRECTIONS: tuple[Direction, ...] = ("over", "under")


def _laplacian_mean_square(luma: bytes) -> int:
    total = 0
    width = DECODE_W
    for y in range(1, DECODE_H - 1):
        row = y * width
        for x in range(1, width - 1):
            center = luma[row + x]
            value = (
                4 * center
                - luma[row + x - 1]
                - luma[row + x + 1]
                - luma[row + width + x]
                - luma[row - width + x]
            )
            total += value * value
    return total // _INTERIOR


def compute_frame_facts(frames: Sequence[LumaFrame]) -> tuple[FrameFact, ...]:
    computed: list[FrameFact] = []
    previous: bytes | None = None
    for frame in frames:
        luma = frame.luma
        mean_m = sum(luma) * 1000 // (_FULL_SCALE * _PIXELS)
        black_m = sum(1 for pixel in luma if pixel <= BLACK_PIXEL_MAX_SAMPLE) * 1000 // _PIXELS
        clipped_m = sum(1 for pixel in luma if pixel >= CLIPPED_HIGH_SAMPLE) * 1000 // _PIXELS
        if previous is None:
            diff_m = 0
        else:
            diff_m = (
                sum(abs(a - b) for a, b in zip(luma, previous, strict=True))
                * 1000
                // (_FULL_SCALE * _PIXELS)
            )
        previous = luma
        computed.append(
            FrameFact(
                frame_index=frame.frame_index,
                pts=frame.pts,
                mean_luma_m=mean_m,
                black_pixel_fraction_m=black_m,
                clipped_high_fraction_m=clipped_m,
                diff_prev_m=diff_m,
                laplacian_mean_square=_laplacian_mean_square(luma),
            )
        )
    return tuple(computed)


def is_black(fact: FrameFact) -> bool:
    return fact.mean_luma_m <= BLACK_MAX_MEAN_M and (
        fact.black_pixel_fraction_m >= BLACK_MIN_PIXEL_FRACTION_M
    )


def exposure_direction(fact: FrameFact) -> Direction | None:
    if is_black(fact):
        return None
    if fact.mean_luma_m > EXPOSURE_HIGH_MEAN_M and (
        fact.clipped_high_fraction_m >= OVER_MIN_CLIPPED_FRACTION_M
    ):
        return "over"
    if fact.mean_luma_m < EXPOSURE_LOW_MEAN_M:
        return "under"
    return None


__all__ = [
    "DIRECTIONS",
    "compute_frame_facts",
    "exposure_direction",
    "is_black",
]
