from __future__ import annotations

import json
from fractions import Fraction

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from services.conform import (
    CoordinateRangeError,
    OriginalTimestamp,
    PtsSpan,
    RationalTimeBase,
    SampleSpan,
    audio_sample_for_pts,
    frame_conversion_accounting,
    pts_floor_for_audio_sample,
    pts_floor_for_video_frame,
    pts_span_for_source_span,
    record_frame_for_source_frame,
    record_span_for_source_span,
    source_frame_for_record_frame,
    source_span_for_pts_span,
    video_frame_for_pts,
)
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.contracts.serialization import canonical_json_bytes

RATE_POOL = (
    RationalFrameRate(num=24, den=1),
    RationalFrameRate(num=25, den=1),
    RationalFrameRate(num=30, den=1),
    RationalFrameRate(num=50, den=1),
    RationalFrameRate(num=90, den=1),
    RationalFrameRate(num=24000, den=1001),
    RationalFrameRate(num=30000, den=1001),
    RationalFrameRate(num=60000, den=1001),
)
TIME_BASE_POOL = (
    RationalTimeBase(num=1, den=90000),
    RationalTimeBase(num=1, den=1000),
    RationalTimeBase(num=1, den=48000),
    RationalTimeBase(num=1001, den=90000),
)
rates = st.sampled_from(RATE_POOL)
time_bases = st.sampled_from(TIME_BASE_POOL)


@given(
    pts_a=st.integers(min_value=0, max_value=10**9),
    pts_b=st.integers(min_value=0, max_value=10**9),
    rate=rates,
    time_base=time_bases,
)
def test_pts_to_frame_is_monotonic(
    pts_a: int, pts_b: int, rate: RationalFrameRate, time_base: RationalTimeBase
) -> None:
    low, high = sorted((pts_a, pts_b))
    first = OriginalTimestamp(pts=low, time_base=time_base)
    second = OriginalTimestamp(pts=high, time_base=time_base)

    assert video_frame_for_pts(first, rate) <= video_frame_for_pts(second, rate)


@given(
    frame=st.integers(min_value=0, max_value=60000),
    frame_rate=st.integers(min_value=1, max_value=120),
    ticks_per_frame=st.integers(min_value=1, max_value=5000),
)
def test_on_lattice_round_trip_is_identity(
    frame: int, frame_rate: int, ticks_per_frame: int
) -> None:
    rate = RationalFrameRate(num=frame_rate, den=1)
    time_base = RationalTimeBase(num=1, den=frame_rate * ticks_per_frame)

    timestamp = pts_floor_for_video_frame(frame, rate, time_base)

    assert timestamp.pts == frame * ticks_per_frame
    assert video_frame_for_pts(timestamp, rate) == frame


@given(
    frame=st.integers(min_value=0, max_value=10**6),
    rate=rates,
    time_base=time_bases,
)
def test_floor_inverse_round_trips_when_tick_is_at_most_half_frame(
    frame: int, rate: RationalFrameRate, time_base: RationalTimeBase
) -> None:
    assume(rate.as_fraction * time_base.as_fraction <= Fraction(1, 2))

    timestamp = pts_floor_for_video_frame(frame, rate, time_base)

    assert video_frame_for_pts(timestamp, rate) == frame


@given(
    sample=st.integers(min_value=0, max_value=10**8),
    sample_rate=st.sampled_from([8000, 16000, 22050, 24000, 44100, 48000, 96000]),
    time_base=time_bases,
)
def test_audio_floor_inverse_round_trips_when_tick_is_at_most_half_sample(
    sample: int, sample_rate: int, time_base: RationalTimeBase
) -> None:
    assume(Fraction(sample_rate) * time_base.as_fraction <= Fraction(1, 2))

    timestamp = pts_floor_for_audio_sample(sample, sample_rate, time_base)

    assert audio_sample_for_pts(timestamp, sample_rate) == sample


@given(
    start=st.integers(min_value=0, max_value=5000),
    length=st.integers(min_value=0, max_value=2000),
    rate=rates,
    base=st.integers(min_value=0, max_value=10**6),
)
def test_equal_rate_placement_preserves_half_open_length(
    start: int, length: int, rate: RationalFrameRate, base: int
) -> None:
    span = SourceFrameSpan(start_frame=start, end_frame=start + length, rate=rate)

    placed = record_span_for_source_span(span, base, rate)

    assert placed.start_frame == base
    assert placed.length == span.length
    if length > 0:
        assert record_frame_for_source_frame(start, span, base, rate) == base
        assert (
            record_frame_for_source_frame(start + length - 1, span, base, rate)
            == base + length - 1
        )
        assert source_frame_for_record_frame(base, span, base, rate) == start
        assert (
            source_frame_for_record_frame(base + length - 1, span, base, rate) == start + length - 1
        )


@given(
    frame_rate=st.integers(min_value=1, max_value=120),
    ticks_per_frame=st.integers(min_value=1, max_value=3000),
    start=st.integers(min_value=0, max_value=4000),
    length=st.integers(min_value=0, max_value=1500),
)
def test_pts_span_length_is_invariant_on_the_lattice(
    frame_rate: int, ticks_per_frame: int, start: int, length: int
) -> None:
    rate = RationalFrameRate(num=frame_rate, den=1)
    time_base = RationalTimeBase(num=1, den=frame_rate * ticks_per_frame)
    span = SourceFrameSpan(start_frame=start, end_frame=start + length, rate=rate)

    pts_span = pts_span_for_source_span(span, time_base)

    assert pts_span.length == length * ticks_per_frame
    assert source_span_for_pts_span(pts_span, rate) == span


@given(
    src_rate=rates,
    tgt_rate=rates,
    start=st.integers(min_value=0, max_value=500),
    length=st.integers(min_value=0, max_value=120),
    base=st.integers(min_value=0, max_value=10**6),
)
def test_record_mapping_is_monotonic_and_covers_every_placed_tick(
    src_rate: RationalFrameRate,
    tgt_rate: RationalFrameRate,
    start: int,
    length: int,
    base: int,
) -> None:
    span = SourceFrameSpan(start_frame=start, end_frame=start + length, rate=src_rate)
    report = frame_conversion_accounting(span, tgt_rate)
    placed = record_span_for_source_span(span, base, tgt_rate)

    assert placed.length == report.output_frames
    dropped = set(report.dropped_source_frames)
    shown_ticks = []
    for offset in range(length):
        if offset in dropped:
            with pytest.raises(CoordinateRangeError):
                record_frame_for_source_frame(start + offset, span, base, tgt_rate)
        else:
            shown_ticks.append(
                record_frame_for_source_frame(start + offset, span, base, tgt_rate)
            )

    assert shown_ticks == sorted(shown_ticks)
    for tick in range(base, base + report.output_frames):
        resolved = source_frame_for_record_frame(tick, span, base, tgt_rate)
        assert start <= resolved < start + length
        assert resolved - start not in dropped


@given(
    pts=st.integers(min_value=0, max_value=10**9),
    rate=rates,
    time_base=time_bases,
)
def test_rounding_is_deterministic_across_calls_and_serialization(
    pts: int, rate: RationalFrameRate, time_base: RationalTimeBase
) -> None:
    timestamp = OriginalTimestamp(pts=pts, time_base=time_base)
    first = video_frame_for_pts(timestamp, rate)
    revived = OriginalTimestamp.model_validate_json(timestamp.model_dump_json())

    assert video_frame_for_pts(timestamp, rate) == first
    assert video_frame_for_pts(revived, rate) == first


def _reject_float_literal(value: str) -> int:
    raise AssertionError(f"float literal in canonical JSON: {value}")


@given(
    n=st.integers(min_value=0, max_value=10**5),
    frame_rate=st.integers(min_value=1, max_value=1000),
)
def test_half_tick_boundaries_round_away_in_pts_to_frame(n: int, frame_rate: int) -> None:
    time_base = RationalTimeBase(num=1, den=2 * frame_rate)
    timestamp = OriginalTimestamp(pts=2 * n + 1, time_base=time_base)

    assert (
        video_frame_for_pts(timestamp, RationalFrameRate(num=frame_rate, den=1)) == n + 1
    )


@given(
    frame_rate=st.integers(min_value=1, max_value=120),
    tick_den=st.integers(min_value=1, max_value=5000),
    start=st.integers(min_value=0, max_value=4000),
    length=st.integers(min_value=0, max_value=1500),
)
def test_floor_inverse_and_pts_span_stay_within_true_time_extent(
    frame_rate: int, tick_den: int, start: int, length: int
) -> None:
    rate = RationalFrameRate(num=frame_rate, den=1)
    time_base = RationalTimeBase(num=1, den=tick_den)

    for frame in (start, start + length):
        timestamp = pts_floor_for_video_frame(frame, rate, time_base)
        assert timestamp.seconds <= Fraction(frame, frame_rate)

    span = SourceFrameSpan(start_frame=start, end_frame=start + length, rate=rate)
    pts_span = pts_span_for_source_span(span, time_base)
    tick = time_base.as_fraction
    assert pts_span.start.seconds <= span.rate.duration_for(span.start_frame)
    assert pts_span.start.seconds > span.rate.duration_for(span.start_frame) - tick
    assert pts_span.end.seconds <= span.rate.duration_for(span.end_frame)
    assert pts_span.end.seconds > span.rate.duration_for(span.end_frame) - tick


@given(
    start=st.integers(min_value=0, max_value=10**9),
    end=st.integers(min_value=0, max_value=10**9),
    time_base=time_bases,
    sample_rate=st.sampled_from([48000, 96000]),
)
def test_canonical_serialization_never_contains_floats(
    start: int, end: int, time_base: RationalTimeBase, sample_rate: int
) -> None:
    low, high = sorted((start, end))
    models = (
        OriginalTimestamp(pts=low, time_base=time_base),
        PtsSpan(start_pts=low, end_pts=high, time_base=time_base),
        SampleSpan(start_sample=low, end_sample=high, sample_rate=sample_rate),
    )

    for model in models:
        payload = canonical_json_bytes(model)
        assert json.loads(payload, parse_float=_reject_float_literal)
