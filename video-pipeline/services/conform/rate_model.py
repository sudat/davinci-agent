"""The frozen Phase 0B drop/duplicate frame-rate model, published as the shared rule.

Every function here replicates a rule that the independent Golden derivation
``tests/goldens/reference/phase-0b/derive.py`` froze before this module existed:

- ``round_half_away``     <- derive.py:30-34 (nearest tick, ties away)
- ``ceil_fraction``       <- derive.py:37-38
- ``output_frame_count``  <- derive.py:58  (N = ceil(D*T - 1/2))
- ``assign_ticks``        <- derive.py:57  (nearest-tick assignment)
- ``tick_to_source_index``<- derive.py:59-64 (later-wins collision)
- ``cfr_conversion``      <- derive.py:56-70 (drop/dup accounting)

The formula ``(2*n + d) // (2*d)`` is copied verbatim. Over the non-negative
coordinate domain (PTS, frames, samples are all >= 0) it is exactly
round-half-away-from-zero; negative inputs are outside the canonical domain.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from pydantic import BaseModel, ConfigDict

from services.conform.errors import CoordinateRangeError
from services.conform.guards import guard_int64, validate_rate


class CfrConversionReport(BaseModel):
    """Drop/duplicate accounting for converting ``n`` source frames to a target rate.

    Indices are span-relative offsets (0-based into the converted span), matching
    the frozen Golden tables which convert whole sources starting at frame 0.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_frames: int
    dropped_source_frames: tuple[int, ...]
    duplicated_source_frames: tuple[int, ...]


def round_half_away(value: Fraction) -> int:
    """Nearest integer, ties away from zero (frozen formula, derive.py:30-34)."""

    if value.denominator == 1:
        return value.numerator
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def ceil_fraction(value: Fraction) -> int:
    """Exact ceiling of a Fraction (frozen formula, derive.py:37-38)."""

    return -((-value.numerator) // value.denominator)


def output_frame_count(duration: Fraction, target_rate: Fraction) -> int:
    """N = ceil(D*T - 1/2): ticks a duration D occupies at rate T (derive.py:58).

    For an integral number of source frames at a matching rate this returns the
    frame count unchanged, which is the equal-rate length invariant.
    """

    if duration < 0 or target_rate <= 0:
        raise CoordinateRangeError(f"duration/target must be positive: {duration}, {target_rate}")
    return guard_int64(ceil_fraction(duration * target_rate - Fraction(1, 2)), "output frames")


def assign_ticks(pts_seconds: Sequence[Fraction], target_rate: Fraction) -> list[int]:
    """Nearest-tick target assignment for each source PTS (derive.py:57)."""

    if target_rate <= 0:
        raise CoordinateRangeError(f"target rate must be positive: {target_rate}")
    return [round_half_away(point * target_rate) for point in pts_seconds]


def tick_to_source_index(assigned: Sequence[int], output_frames: int) -> list[int]:
    """Later-wins cursor mapping of each output tick to a source index (derive.py:59-64)."""

    mapping: list[int] = []
    cursor = 0
    for tick in range(output_frames):
        while cursor + 1 < len(assigned) and assigned[cursor + 1] <= tick:
            cursor += 1
        mapping.append(cursor)
    return mapping


def cfr_conversion(
    pts_seconds: Sequence[Fraction], duration: Fraction, target_rate: Fraction
) -> CfrConversionReport:
    """Full frozen drop/dup accounting for converting a PTS sequence (derive.py:56-70)."""

    validate_rate(target_rate.numerator, target_rate.denominator, "target_rate")
    for point in pts_seconds:
        if point < 0:
            raise CoordinateRangeError(f"pts must be non-negative: {point}")
    assigned = assign_ticks(pts_seconds, target_rate)
    output_frames = output_frame_count(duration, target_rate)
    mapping = tick_to_source_index(assigned, output_frames)
    shown = set(mapping)
    dropped = sorted(set(range(len(pts_seconds))) - shown)
    duplicated = sorted(
        {
            mapping[tick]
            for tick in range(1, output_frames)
            if mapping[tick] == mapping[tick - 1]
        }
    )
    return CfrConversionReport(
        output_frames=output_frames,
        dropped_source_frames=tuple(dropped),
        duplicated_source_frames=tuple(duplicated),
    )


def cfr_span_frame_count(source_frames: int, source_rate: Fraction, target_rate: Fraction) -> int:
    """Ticks that ``source_frames`` frames at ``source_rate`` occupy at ``target_rate``.

    Uses the frozen ``ceil(D*T - 1/2)`` rule; when the rates match this equals
    ``source_frames`` exactly, so the half-open length invariant holds.
    """

    validate_rate(source_rate.numerator, source_rate.denominator, "source_rate")
    if source_frames < 0:
        raise CoordinateRangeError(f"frame count must be non-negative: {source_frames}")
    return output_frame_count(Fraction(source_frames) / source_rate, target_rate)
