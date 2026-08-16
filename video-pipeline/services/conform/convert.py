"""Exact conversions among the four canonical coordinate systems (PRD 9.2/9.3).

Rounding policy — every direction states its rule; no silent rounding.

| Direction                            | Rule (frozen-golden reference)              |
| ------------------------------------ | -------------------------------------------- |
| PTS -> Edit Video frame              | round-half-away (nearest tick, derive.py:57) |
| Edit Video frame -> PTS              | floor (latest tick at or before boundary)    |
| PTS -> Edit Audio sample             | round-half-away (nearest sample)             |
| Edit Audio sample -> PTS             | floor (symmetric with frame -> PTS)          |
| Source frames -> record frame count  | N = ceil(D*T - 1/2) (derive.py:58)           |
| Source frame -> record tick          | round-half-away + later-wins (derive.py:59)  |
| Record tick -> source frame          | later-wins inverse mapping                   |

Why each rule holds:

- The nearest-tick assignment reproduces the frozen Phase 0B drop/dup model
  exactly, so analyzers and the model share one assignment rule.
- ``floor`` inverses return the latest lattice tick at or before the exact
  boundary: exact on the lattice, and the round trip
  ``video_frame_for_pts(pts_floor_for_video_frame(f)) == f`` provably holds
  whenever one tick spans at most half a frame (``fps*time_base <= 1/2``).
  Off-lattice behavior when a tick is coarser (e.g. 96 kHz at time_base
  1/90000) is documented, not hidden: neighboring samples collapse to a tick.
- ``N = ceil(D*T - 1/2)`` degenerates to the identity length when rates match,
  which preserves the half-open ``[start, end)`` length invariant; any frame
  count drift at mismatched rates is reported by drop/dup accounting instead of
  being silently rounded away.

All arithmetic uses ``Fraction``/int only. No float ever enters a stored or
serialized canonical field, and analyzers must call these functions instead of
performing ad hoc second-based conversions (PRD 9.3).
"""

from __future__ import annotations

from bisect import bisect_right
from fractions import Fraction

from services.conform.coordinates import (
    OriginalTimestamp,
    PtsSpan,
    RationalTimeBase,
    SampleSpan,
)
from services.conform.errors import CoordinateRangeError
from services.conform.guards import guard_int64, validate_rate, validate_rate_component
from services.conform.rate_model import (
    CfrConversionReport,
    assign_ticks,
    cfr_conversion,
    cfr_span_frame_count,
    round_half_away,
)
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)


def video_frame_for_pts(timestamp: OriginalTimestamp, rate: RationalFrameRate) -> int:
    """PTS -> edit video frame: nearest tick, ties away (frozen assignment rule)."""

    validate_rate(rate.num, rate.den, "frame_rate")
    return guard_int64(
        round_half_away(timestamp.seconds * rate.as_fraction), "edit video frame"
    )


def pts_floor_for_video_frame(
    frame: int, rate: RationalFrameRate, time_base: RationalTimeBase
) -> OriginalTimestamp:
    """Edit video frame -> PTS: floor of the exact boundary onto the tick lattice."""

    validate_rate(rate.num, rate.den, "frame_rate")
    if frame < 0:
        raise CoordinateRangeError(f"frame must be non-negative: {frame}")
    exact = Fraction(frame) / (rate.as_fraction * time_base.as_fraction)
    return OriginalTimestamp(pts=exact.numerator // exact.denominator, time_base=time_base)


def audio_sample_for_pts(timestamp: OriginalTimestamp, sample_rate: int) -> int:
    """PTS -> edit audio sample: nearest sample, ties away."""

    validate_rate_component(sample_rate, "sample_rate")
    return guard_int64(round_half_away(timestamp.seconds * sample_rate), "edit audio sample")


def pts_floor_for_audio_sample(
    sample: int, sample_rate: int, time_base: RationalTimeBase
) -> OriginalTimestamp:
    """Edit audio sample -> PTS: floor of the exact sample boundary."""

    validate_rate_component(sample_rate, "sample_rate")
    if sample < 0:
        raise CoordinateRangeError(f"sample must be non-negative: {sample}")
    exact = Fraction(sample, sample_rate) / time_base.as_fraction
    return OriginalTimestamp(pts=exact.numerator // exact.denominator, time_base=time_base)


def pts_span_for_source_span(span: SourceFrameSpan, time_base: RationalTimeBase) -> PtsSpan:
    """Frame span -> PTS span: floor at both boundaries, half-open preserved."""

    start = pts_floor_for_video_frame(span.start_frame, span.rate, time_base)
    end = pts_floor_for_video_frame(span.end_frame, span.rate, time_base)
    return PtsSpan(start_pts=start.pts, end_pts=end.pts, time_base=time_base)


def source_span_for_pts_span(span: PtsSpan, rate: RationalFrameRate) -> SourceFrameSpan:
    """PTS span -> frame span: nearest tick at both boundaries (monotone, half-open)."""

    start = video_frame_for_pts(span.start, rate)
    end = video_frame_for_pts(span.end, rate)
    return SourceFrameSpan(start_frame=start, end_frame=end, rate=rate)


def audio_span_for_pts_span(span: PtsSpan, sample_rate: int) -> SampleSpan:
    """PTS span -> sample span: nearest sample at both boundaries."""

    start = audio_sample_for_pts(span.start, sample_rate)
    end = audio_sample_for_pts(span.end, sample_rate)
    return SampleSpan(start_sample=start, end_sample=end, sample_rate=sample_rate)


def pts_span_for_audio_span(span: SampleSpan, time_base: RationalTimeBase) -> PtsSpan:
    """Sample span -> PTS span: floor at both boundaries."""

    start = pts_floor_for_audio_sample(span.start_sample, span.sample_rate, time_base)
    end = pts_floor_for_audio_sample(span.end_sample, span.sample_rate, time_base)
    return PtsSpan(start_pts=start.pts, end_pts=end.pts, time_base=time_base)


def record_span_for_source_span(
    span: SourceFrameSpan, base_record_frame: int, timeline_rate: RationalFrameRate
) -> RecordFrameSpan:
    """Place a source span at a record base: ``[base, base + N)`` with frozen N."""

    validate_rate(span.rate.num, span.rate.den, "source_rate")
    validate_rate(timeline_rate.num, timeline_rate.den, "timeline_rate")
    if base_record_frame < 0:
        raise CoordinateRangeError(f"base record frame must be non-negative: {base_record_frame}")
    length = cfr_span_frame_count(span.length, span.rate.as_fraction, timeline_rate.as_fraction)
    return RecordFrameSpan(
        start_frame=base_record_frame,
        end_frame=guard_int64(base_record_frame + length, "record frame"),
    )


def frame_conversion_accounting(
    span: SourceFrameSpan, timeline_rate: RationalFrameRate
) -> CfrConversionReport:
    """Frozen drop/dup accounting for a uniform CFR span at a target rate."""

    validate_rate(span.rate.num, span.rate.den, "source_rate")
    validate_rate(timeline_rate.num, timeline_rate.den, "timeline_rate")
    source_rate = span.rate.as_fraction
    pts_seconds = [Fraction(offset) / source_rate for offset in range(span.length)]
    duration = Fraction(span.length) / source_rate
    return cfr_conversion(pts_seconds, duration, timeline_rate.as_fraction)


def _relative_offset(source_frame: int, span: SourceFrameSpan) -> int:
    if source_frame < span.start_frame or source_frame >= span.end_frame:
        raise CoordinateRangeError(
            f"source frame {source_frame} outside half-open span "
            f"[{span.start_frame}, {span.end_frame})"
        )
    return source_frame - span.start_frame


def record_frame_for_source_frame(
    source_frame: int,
    span: SourceFrameSpan,
    base_record_frame: int,
    timeline_rate: RationalFrameRate,
) -> int:
    """Source frame -> record tick via the frozen assignment; drops are rejected."""

    offset = _relative_offset(source_frame, span)
    report = frame_conversion_accounting(span, timeline_rate)
    if offset in report.dropped_source_frames:
        raise CoordinateRangeError(
            f"source frame offset {offset} is dropped at the target rate; "
            "the drop/dup accounting above must drive any placement decision"
        )
    tick = round_half_away(
        Fraction(offset) / span.rate.as_fraction * timeline_rate.as_fraction
    )
    return guard_int64(base_record_frame + tick, "record frame")


def source_frame_for_record_frame(
    record_frame: int,
    span: SourceFrameSpan,
    base_record_frame: int,
    timeline_rate: RationalFrameRate,
) -> int:
    """Record tick -> source frame via the frozen later-wins inverse mapping."""

    placed = record_span_for_source_span(span, base_record_frame, timeline_rate)
    if not placed.start_frame <= record_frame < placed.end_frame:
        raise CoordinateRangeError(
            f"record frame {record_frame} outside placement [{placed.start_frame}, "
            f"{placed.end_frame})"
        )
    assigned = assign_ticks(
        [Fraction(offset) / span.rate.as_fraction for offset in range(span.length)],
        timeline_rate.as_fraction,
    )
    index = bisect_right(assigned, record_frame - base_record_frame) - 1
    return span.start_frame + max(index, 0)
