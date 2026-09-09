"""Prediction module: conform accounting reproduces every frozen golden table."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path

import pytest

from services.conform.convert import frame_conversion_accounting
from services.conform.rate_model import CfrConversionReport
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.normalize.accounting import (
    SourceSpan,
    SpanDerivationError,
    expected_conversion,
    source_span_from_probe,
)
from services.normalize.probe import VideoFacts, parse_media_facts
from tests.normalize.conftest import PHASE_0B_MANIFEST_DIR

TARGET_RATE = RationalFrameRate(num=30, den=1)


def _probe_facts(ffprobe: Path, media: Path) -> tuple[VideoFacts, dict[str, object]]:
    stdout = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-print_format", "json", "-show_streams",
            "-show_format", "-count_frames", str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    payload: dict[str, object] = json.loads(stdout)
    return parse_media_facts(payload).video, payload


EMPTY_VIDEO = VideoFacts(
    codec_name="h264",
    width=1920,
    height=1080,
    pix_fmt="yuv420p",
    r_frame_rate_num=30,
    r_frame_rate_den=1,
    avg_frame_rate_num=30,
    avg_frame_rate_den=1,
    nb_read_frames=0,
    frame_count_source="decoded",
    duration_num=0,
    duration_den=1,
    rotation_degrees=None,
    color_space=None,
    color_transfer=None,
    color_primaries=None,
    color_range=None,
    time_base_num=1,
    time_base_den=30000,
)


def test_probe_derived_span_reproduces_frozen_conversion_tables(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path
) -> None:
    for fixture_id, media in fixture_media.items():
        manifest = json.loads(
            (PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        )
        video, _payload = _probe_facts(pinned_ffprobe, media)
        span = source_span_from_probe(video)
        golden_table = manifest["rational_frame_rate_table"]["frame_rate"]
        assert isinstance(golden_table, dict)
        assert span.rate.as_fraction == Fraction(
            golden_table["num"], golden_table["den"]
        ), fixture_id
        golden = manifest["conversions"]["cfr30"]
        assert isinstance(golden, dict)
        assert span.frames == golden["input_frames"], fixture_id
        report = expected_conversion(span, TARGET_RATE)
        assert report.output_frames == golden["output_frames"], fixture_id
        assert list(report.dropped_source_frames) == golden["dropped_source_frames"]
        assert list(report.duplicated_source_frames) == golden["duplicated_source_frames"]


def test_expected_conversion_delegates_to_conform_accounting() -> None:
    span = SourceSpan(frames=600, rate=RationalFrameRate(num=30000, den=1001))
    report = expected_conversion(span, TARGET_RATE)
    direct = frame_conversion_accounting(
        SourceFrameSpan(
            start_frame=0, end_frame=600, rate=RationalFrameRate(num=30000, den=1001)
        ),
        TARGET_RATE,
    )
    assert report == direct == CfrConversionReport(
        output_frames=601, dropped_source_frames=(), duplicated_source_frames=(499,)
    )


def test_span_rejects_empty_or_undecodable_video() -> None:
    with pytest.raises(SpanDerivationError):
        source_span_from_probe(EMPTY_VIDEO)
