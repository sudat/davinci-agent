from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.conform import (
    CfrConversionReport,
    CoordinateOverflowError,
    CoordinateRangeError,
    NonMonotonicPtsError,
    OriginalTimestamp,
    PtsSpan,
    RationalTimeBase,
    SampleSpan,
    assign_ticks,
    audio_sample_for_pts,
    audio_span_for_pts_span,
    cfr_conversion,
    frame_conversion_accounting,
    pts_floor_for_audio_sample,
    pts_floor_for_video_frame,
    pts_span_for_audio_span,
    pts_span_for_source_span,
    record_frame_for_source_frame,
    record_span_for_source_span,
    require_monotonic_pts,
    source_frame_for_record_frame,
    source_span_for_pts_span,
    video_frame_for_pts,
)
from services.conform import cli as conform_cli
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.contracts.serialization import canonical_json_bytes

GOLDEN_EXPECTED = Path(__file__).resolve().parents[1] / "goldens/reference/phase-0b/expected.json"
RATE_24 = RationalFrameRate(num=24, den=1)
RATE_30 = RationalFrameRate(num=30, den=1)
RATE_2997 = RationalFrameRate(num=30000, den=1001)
RATE_5994 = RationalFrameRate(num=60000, den=1001)
TB_90K = RationalTimeBase(num=1, den=90000)
TB_MS = RationalTimeBase(num=1, den=1000)
TB_48K = RationalTimeBase(num=1, den=48000)
TB_NTSC = RationalTimeBase(num=1, den=30000)


def golden_variant(variant_id: str) -> dict:
    payload: dict = json.loads(GOLDEN_EXPECTED.read_text(encoding="utf-8"))
    return dict(payload["variants"][variant_id])


# --- CFR exact vectors (happy) ------------------------------------------------


@pytest.mark.parametrize("frame", [0, 1, 2, 3, 149, 150, 299, 300, 449, 450, 499, 500, 599, 600])
def test_frame_to_pts_is_exact_at_ffmpeg_time_base(frame: int) -> None:
    for rate, ticks_per_frame in ((RATE_24, 3750), (RATE_2997, 3003)):
        timestamp = pts_floor_for_video_frame(frame, rate, TB_90K)

        assert timestamp.pts == frame * ticks_per_frame
        assert video_frame_for_pts(timestamp, rate) == frame


def test_frame_to_pts_is_exact_at_ntsc_native_time_base() -> None:
    for frame in (0, 1, 2, 499, 500, 600):
        timestamp = pts_floor_for_video_frame(frame, RATE_2997, TB_NTSC)

        assert timestamp.pts == frame * 1001
        assert video_frame_for_pts(timestamp, RATE_2997) == frame


@pytest.mark.parametrize("frame", [0, 2, 4, 150, 300, 450, 598, 600])
def test_even_frames_are_on_lattice_at_59_94(frame: int) -> None:
    timestamp = pts_floor_for_video_frame(frame, RATE_5994, TB_90K)

    assert timestamp.pts == frame * 3003 // 2
    assert video_frame_for_pts(timestamp, RATE_5994) == frame


@pytest.mark.parametrize("frame", [1, 3, 149, 499])
def test_odd_frames_at_59_94_use_documented_floor_and_round_trip(frame: int) -> None:
    timestamp = pts_floor_for_video_frame(frame, RATE_5994, TB_90K)
    exact = Fraction(frame * 3003, 2)

    assert timestamp.pts == exact.numerator // exact.denominator
    assert video_frame_for_pts(timestamp, RATE_5994) == frame


@pytest.mark.parametrize("frame", [0, 30, 60, 90, 120, 150, 300, 600])
def test_2997_on_lattice_at_millisecond_time_base(frame: int) -> None:
    timestamp = pts_floor_for_video_frame(frame, RATE_2997, TB_MS)

    assert timestamp.pts == frame * 1001 // 30
    assert video_frame_for_pts(timestamp, RATE_2997) == frame


def test_2997_off_lattice_at_millisecond_time_base_documents_floor() -> None:
    first = pts_floor_for_video_frame(1, RATE_2997, TB_MS)
    frame_500 = pts_floor_for_video_frame(500, RATE_2997, TB_MS)

    assert first.pts == 33
    assert video_frame_for_pts(first, RATE_2997) == 1
    assert frame_500.pts == 16683
    assert video_frame_for_pts(frame_500, RATE_2997) == 500


@pytest.mark.parametrize("frame", [0, 3, 6, 150, 300])
def test_24fps_on_lattice_at_millisecond_time_base(frame: int) -> None:
    timestamp = pts_floor_for_video_frame(frame, RATE_24, TB_MS)

    assert timestamp.pts == frame * 125 // 3
    assert video_frame_for_pts(timestamp, RATE_24) == frame


def test_audio_identity_at_native_time_base_48k() -> None:
    timestamp = pts_floor_for_audio_sample(1976384, 48000, TB_48K)

    assert timestamp.pts == 1976384
    assert audio_sample_for_pts(timestamp, 48000) == 1976384


@pytest.mark.parametrize("sample", [0, 8, 16, 24000, 48000, 96000])
def test_audio_48k_on_lattice_at_ffmpeg_time_base(sample: int) -> None:
    timestamp = pts_floor_for_audio_sample(sample, 48000, TB_90K)

    assert timestamp.pts == sample * 15 // 8
    assert audio_sample_for_pts(timestamp, 48000) == sample


def test_audio_48k_off_lattice_floor_round_trips() -> None:
    timestamp = pts_floor_for_audio_sample(1, 48000, TB_90K)

    assert timestamp.pts == 1
    assert audio_sample_for_pts(timestamp, 48000) == 1


def test_audio_96k_identity_at_native_time_base() -> None:
    timestamp = pts_floor_for_audio_sample(96001, 96000, RationalTimeBase(num=1, den=96000))

    assert timestamp.pts == 96001
    assert audio_sample_for_pts(timestamp, 96000) == 96001


def test_audio_96k_off_lattice_documents_coarse_tick() -> None:
    timestamp = pts_floor_for_audio_sample(1, 96000, TB_90K)

    assert timestamp.pts == 0
    assert audio_sample_for_pts(timestamp, 96000) == 0


# --- Frozen Phase 0B golden parity --------------------------------------------


@pytest.mark.parametrize(
    "variant_id",
    ["p0b-cfr24", "p0b-ntsc2997", "p0b-ntsc5994", "p0b-vfr-2-3-cadence"],
)
@pytest.mark.parametrize(
    ("target_name", "target"),
    [("cfr24", RATE_24), ("cfr30", RATE_30)],
)
def test_frozen_model_parity_with_phase_0b_goldens(
    variant_id: str, target_name: str, target: RationalFrameRate
) -> None:
    variant = golden_variant(variant_id)
    expected = variant[target_name]
    source = variant["source"]
    frame_count = source["frame_count"]
    if variant_id == "p0b-vfr-2-3-cadence":
        pts_seconds = [Fraction((5 * index) // 2, 60) for index in range(frame_count)]
    else:
        rate = Fraction(source["frame_rate"]["num"], source["frame_rate"]["den"])
        pts_seconds = [Fraction(index, 1) / rate for index in range(frame_count)]
    duration = Fraction(source["duration_seconds"]["num"], source["duration_seconds"]["den"])

    report = frame_conversion_accounting_for_pts(pts_seconds, duration, target)

    assert report.output_frames == expected["output_frames"]
    assert list(report.dropped_source_frames) == expected["dropped_source_frames"]
    assert list(report.duplicated_source_frames) == expected["duplicated_source_frames"]


def frame_conversion_accounting_for_pts(
    pts_seconds: list[Fraction], duration: Fraction, target: RationalFrameRate
) -> CfrConversionReport:
    return cfr_conversion(pts_seconds, duration, target.as_fraction)


@pytest.mark.parametrize(
    ("variant_id", "target"),
    [
        ("p0b-cfr24", RATE_30),
        ("p0b-ntsc2997", RATE_24),
        ("p0b-ntsc2997", RATE_30),
        ("p0b-ntsc5994", RATE_24),
        ("p0b-ntsc5994", RATE_30),
    ],
)
def test_span_accounting_matches_goldens_for_uniform_cfr(
    variant_id: str, target: RationalFrameRate
) -> None:
    variant = golden_variant(variant_id)
    target_name = "cfr24" if target == RATE_24 else "cfr30"
    expected = variant[target_name]
    source_rate = variant["source"]["frame_rate"]
    span = SourceFrameSpan(
        start_frame=0,
        end_frame=variant["source"]["frame_count"],
        rate=RationalFrameRate(num=source_rate["num"], den=source_rate["den"]),
    )

    report = frame_conversion_accounting(span, target)

    assert report.output_frames == expected["output_frames"]
    assert list(report.dropped_source_frames) == expected["dropped_source_frames"]
    assert list(report.duplicated_source_frames) == expected["duplicated_source_frames"]


def test_drop_frame_boundary_at_2997_to_30_matches_frozen_table() -> None:
    variant = golden_variant("p0b-ntsc2997")
    frozen = variant["cfr30"]

    pts_seconds = [Fraction(index * 1001, 30000) for index in range(501)]
    assigned = assign_ticks(pts_seconds, Fraction(30, 1))
    span = SourceFrameSpan(start_frame=0, end_frame=600, rate=RATE_2997)
    report = frame_conversion_accounting(span, RATE_30)

    assert assigned[499] == 499
    assert assigned[500] == 501
    assert frozen["output_frames"] == 601
    assert frozen["duplicated_source_frames"] == [499]
    assert report.output_frames == 601
    assert report.dropped_source_frames == ()
    assert report.duplicated_source_frames == (499,)
    assert record_frame_for_source_frame(499, span, 0, RATE_30) == 499
    assert record_frame_for_source_frame(500, span, 0, RATE_30) == 501
    assert source_frame_for_record_frame(500, span, 0, RATE_30) == 499


# --- Span placement and half-open semantics -----------------------------------


def test_equal_rate_placement_is_pure_offset_with_length_invariant() -> None:
    span = SourceFrameSpan(start_frame=10, end_frame=40, rate=RATE_30)

    placed = record_span_for_source_span(span, 100, RATE_30)

    assert (placed.start_frame, placed.end_frame) == (100, 130)
    assert placed.length == span.length == 30
    assert record_frame_for_source_frame(10, span, 100, RATE_30) == 100
    assert record_frame_for_source_frame(39, span, 100, RATE_30) == 129
    assert source_frame_for_record_frame(100, span, 100, RATE_30) == 10
    assert source_frame_for_record_frame(129, span, 100, RATE_30) == 39


def test_2997_to_30_placement_reports_six_hundred_one_frames() -> None:
    span = SourceFrameSpan(start_frame=0, end_frame=600, rate=RATE_2997)

    placed = record_span_for_source_span(span, 7, RATE_30)
    report = frame_conversion_accounting(span, RATE_30)

    assert (placed.start_frame, placed.end_frame) == (7, 608)
    assert placed.length == 601
    assert report.output_frames == 601
    assert report.duplicated_source_frames == (499,)
    assert record_frame_for_source_frame(499, span, 7, RATE_30) == 7 + 499
    assert record_frame_for_source_frame(500, span, 7, RATE_30) == 7 + 501
    assert source_frame_for_record_frame(7 + 500, span, 7, RATE_30) == 499


def test_pts_span_round_trip_on_lattice_preserves_length() -> None:
    span = SourceFrameSpan(start_frame=100, end_frame=200, rate=RATE_2997)

    pts_span = pts_span_for_source_span(span, TB_90K)

    assert (pts_span.start_pts, pts_span.end_pts) == (100 * 3003, 200 * 3003)
    assert pts_span.length == span.length * 3003
    assert source_span_for_pts_span(pts_span, RATE_2997) == span


def test_audio_span_conversion_is_exact_at_48k() -> None:
    pts_span = PtsSpan(start_pts=0, end_pts=90000, time_base=TB_90K)

    samples = audio_span_for_pts_span(pts_span, 48000)
    back = pts_span_for_audio_span(samples, TB_90K)

    assert (samples.start_sample, samples.end_sample) == (0, 48000)
    assert samples.length == 48000
    assert back == pts_span


def test_half_open_span_shorter_than_one_frame_keeps_empty_extent() -> None:
    pts_span = PtsSpan(start_pts=0, end_pts=1, time_base=TB_90K)

    frames = source_span_for_pts_span(pts_span, RATE_30)

    assert frames.start_frame == frames.end_frame == 0
    assert frames.length == 0


def test_off_by_one_naive_length_assumption_is_detected() -> None:
    span = SourceFrameSpan(start_frame=0, end_frame=600, rate=RATE_2997)

    placed = record_span_for_source_span(span, 0, RATE_30)
    report = frame_conversion_accounting(span, RATE_30)

    assert placed.length == 601
    assert placed.length != span.length
    assert placed.length - span.length == (
        len(report.duplicated_source_frames) - len(report.dropped_source_frames)
    )


def test_dropped_source_frame_record_query_is_rejected_explicitly() -> None:
    span = SourceFrameSpan(start_frame=0, end_frame=600, rate=RATE_5994)

    with pytest.raises(CoordinateRangeError, match="dropped"):
        record_frame_for_source_frame(1, span, 0, RATE_30)


def test_record_query_outside_placement_is_rejected() -> None:
    span = SourceFrameSpan(start_frame=0, end_frame=10, rate=RATE_30)

    with pytest.raises(CoordinateRangeError):
        source_frame_for_record_frame(10, span, 0, RATE_30)
    with pytest.raises(CoordinateRangeError):
        record_frame_for_source_frame(10, span, 0, RATE_30)


# --- Failure cases: rejection paths -------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [
        lambda: SourceFrameSpan(start_frame=-5, end_frame=10, rate=RATE_30),
        lambda: OriginalTimestamp(pts=-1, time_base=TB_90K),
        lambda: PtsSpan(start_pts=-2, end_pts=5, time_base=TB_90K),
        lambda: SampleSpan(start_sample=-1, end_sample=5, sample_rate=48000),
        lambda: record_span_for_source_span(
            SourceFrameSpan(start_frame=0, end_frame=5, rate=RATE_30), -1, RATE_30
        ),
    ],
    ids=["source-span", "timestamp", "pts-span", "sample-span", "negative-base"],
)
def test_negative_positions_are_rejected(builder: type) -> None:
    with pytest.raises((ValidationError, CoordinateRangeError)):
        builder()


@pytest.mark.parametrize(
    "builder",
    [
        lambda: SourceFrameSpan(start_frame=10, end_frame=5, rate=RATE_30),
        lambda: PtsSpan(start_pts=10, end_pts=5, time_base=TB_90K),
        lambda: SampleSpan(start_sample=10, end_sample=5, sample_rate=48000),
    ],
    ids=["source-span", "pts-span", "sample-span"],
)
def test_inverted_spans_are_rejected(builder: type) -> None:
    with pytest.raises(ValidationError, match="span_inverted"):
        builder()


def test_pts_beyond_int64_guard_is_rejected() -> None:
    with pytest.raises(ValidationError, match="coordinate_overflow"):
        OriginalTimestamp(pts=1 << 63, time_base=TB_90K)


def test_time_base_component_beyond_guard_is_rejected() -> None:
    with pytest.raises(ValidationError, match="coordinate_overflow"):
        RationalTimeBase(num=1, den=1 << 40)


def test_rate_component_beyond_guard_is_rejected_on_conversion() -> None:
    timestamp = OriginalTimestamp(pts=0, time_base=TB_90K)
    huge_rate = RationalFrameRate(num=1 << 32, den=1)

    with pytest.raises(CoordinateOverflowError, match="frame_rate"):
        video_frame_for_pts(timestamp, huge_rate)
    with pytest.raises(CoordinateOverflowError, match="sample_rate"):
        audio_sample_for_pts(timestamp, 1 << 32)


def test_conversion_result_overflow_is_rejected() -> None:
    timestamp = OriginalTimestamp(pts=(1 << 63) - 1, time_base=RationalTimeBase(num=1, den=1))

    with pytest.raises(CoordinateOverflowError):
        video_frame_for_pts(timestamp, RATE_2997)


def test_non_monotonic_pts_sequence_is_rejected() -> None:
    sequence = [
        OriginalTimestamp(pts=0, time_base=TB_90K),
        OriginalTimestamp(pts=3003, time_base=TB_90K),
        OriginalTimestamp(pts=2002, time_base=TB_90K),
    ]

    with pytest.raises(NonMonotonicPtsError, match="index 2"):
        require_monotonic_pts(sequence)


def test_equal_pts_are_allowed_as_non_decreasing() -> None:
    sequence = [
        OriginalTimestamp(pts=100, time_base=TB_90K),
        OriginalTimestamp(pts=100, time_base=TB_90K),
    ]

    require_monotonic_pts(sequence)


def test_malformed_types_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OriginalTimestamp.model_validate({"pts": "5", "time_base": TB_90K})
    with pytest.raises(ValidationError):
        OriginalTimestamp.model_validate({"pts": 5.0, "time_base": TB_90K})
    with pytest.raises(ValidationError):
        OriginalTimestamp.model_validate(
            {"pts": 5, "time_base": {"num": 1, "den": 0}}
        )
    with pytest.raises(ValidationError):
        OriginalTimestamp.model_validate_json('{"pts":5.5,"time_base":{"num":1,"den":90000}}')
    with pytest.raises(ValidationError):
        SampleSpan.model_validate(
            {"start_sample": 0, "end_sample": 5, "sample_rate": "48000"}
        )


def test_time_base_is_stored_reduced() -> None:
    time_base = RationalTimeBase(num=2, den=180000)

    assert (time_base.num, time_base.den) == (1, 90000)


# --- Canonical serialization never contains floats -----------------------------


def _reject_float_literal(value: str) -> int:
    raise AssertionError(f"float literal in canonical JSON: {value}")


def test_canonical_serialization_contains_integral_values_only() -> None:
    models = (
        OriginalTimestamp(pts=3003, time_base=TB_90K),
        PtsSpan(start_pts=0, end_pts=600600, time_base=TB_90K),
        SampleSpan(start_sample=0, end_sample=48000, sample_rate=48000),
        CfrConversionReport(
            output_frames=601, dropped_source_frames=(), duplicated_source_frames=(499,)
        ),
    )

    for model in models:
        payload = canonical_json_bytes(model)
        revived = json.loads(payload, parse_float=_reject_float_literal)
        assert revived


# --- CLI probes ----------------------------------------------------------------


def test_cli_cfr_vector_battery_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert conform_cli.main(["cfr-vector"]) == 0

    output = capsys.readouterr().out

    assert "cfr exact vectors ok" in output


def test_cli_span_boundary_matches_frozen_values(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "span-boundary",
        "--fps-num", "30000", "--fps-den", "1001",
        "--frames", "600",
        "--target-num", "30", "--target-den", "1",
        "--expect-output", "601",
        "--expect-duplicated", "499",
    ]

    assert conform_cli.main(argv) == 0

    assert "boundary ok" in capsys.readouterr().out


CONVERT_SPAN_BASE = [
    "convert-span",
    "--start-frame", "0", "--end-frame", "600",
    "--fps-num", "30000", "--fps-den", "1001",
    "--timeline-num", "30", "--timeline-den", "1",
    "--base", "0",
]


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["convert-span", "--start-frame", "-5", "--end-frame", "10",
          "--fps-num", "30000", "--fps-den", "1001",
          "--timeline-num", "30", "--timeline-den", "1", "--base", "0"], "negative_span"),
        (["convert-span", "--start-frame", "10", "--end-frame", "5",
          "--fps-num", "30000", "--fps-den", "1001",
          "--timeline-num", "30", "--timeline-den", "1", "--base", "0"], "inverted_span"),
        (["convert-pts", "--pts", str(1 << 63), "--time-base-num", "1", "--time-base-den", "90000",
          "--fps-num", "30000", "--fps-den", "1001"], "coordinate_overflow"),
        (["convert-span", "--start-frame", "0", "--end-frame", "1",
          "--fps-num", str(1 << 32), "--fps-den", "1",
          "--timeline-num", "30", "--timeline-den", "1", "--base", "0"], "coordinate_overflow"),
        (["pts-sequence", "--pts", "0,3003,6006,3003",
          "--time-base-num", "1", "--time-base-den", "90000"], "non_monotonic_pts"),
        ([*CONVERT_SPAN_BASE, "--expect-record-length", "600"], "off_by_one"),
        (["span-boundary", "--fps-num", "30000", "--fps-den", "1001", "--frames", "600",
          "--target-num", "30", "--target-den", "1",
          "--expect-output", "600", "--expect-duplicated", "500"], "drop_frame_boundary_mismatch"),
        (["convert-span", "--start-frame", "1.5", "--end-frame", "10",
          "--fps-num", "30000", "--fps-den", "1001",
          "--timeline-num", "30", "--timeline-den", "1", "--base", "0"], "malformed_input"),
    ],
)
def test_cli_failure_probes_report_explicit_labels(
    argv: list[str], label: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert conform_cli.main(argv) == 2

    assert f"error={label}:" in capsys.readouterr().out


def test_cli_convert_span_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*CONVERT_SPAN_BASE, "--expect-record-length", "601"]

    assert conform_cli.main(argv) == 0

    assert "record span ok" in capsys.readouterr().out
