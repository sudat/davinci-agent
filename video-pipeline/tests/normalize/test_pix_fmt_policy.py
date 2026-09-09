from __future__ import annotations

from pathlib import Path

import pytest

from services.normalize.errors import NormalizeVerificationError
from services.normalize.probe import MediaFacts, VideoFacts
from services.normalize.toolchain_guard import load_phase0b_lock
from services.normalize.verify import OutputExpectation, _verify_video_shape


def _facts(*, pix_fmt: str) -> MediaFacts:
    video = VideoFacts(
        codec_name="h264",
        width=16,
        height=16,
        pix_fmt=pix_fmt,
        r_frame_rate_num=30,
        r_frame_rate_den=1,
        avg_frame_rate_num=30,
        avg_frame_rate_den=1,
        nb_read_frames=1,
        frame_count_source="decoded",
        duration_num=1,
        duration_den=30,
        rotation_degrees=0,
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        color_range="tv",
        time_base_num=1,
        time_base_den=30000,
    )
    return MediaFacts(video=video, audio=None)


def _expectation(
    source: MediaFacts,
    *,
    declared_video_pix_fmt: str | None = None,
    declared_conversions: tuple[str, ...] = (),
) -> OutputExpectation:
    lock = load_phase0b_lock(Path("config/toolchains/phase-0b-v2.json"))
    return OutputExpectation(
        target=lock.normalization.target,
        source=source,
        expected_output_frames=1,
        declared_video_pix_fmt=declared_video_pix_fmt,
        declared_conversions=declared_conversions,
    )


def test_identity_pix_fmt_passes() -> None:
    source = _facts(pix_fmt="yuv420p")
    _verify_video_shape(source, _expectation(source))


def test_identity_pix_fmt_rejects_conversion() -> None:
    source = _facts(pix_fmt="yuv420p10le")
    with pytest.raises(NormalizeVerificationError, match="expected yuv420p10le"):
        _verify_video_shape(
            _facts(pix_fmt="yuv420p"), _expectation(source)
        )


def test_declared_pix_fmt_requires_and_accepts_recorded_conversion() -> None:
    source = _facts(pix_fmt="yuv420p10le")
    _verify_video_shape(
        _facts(pix_fmt="yuv420p"),
        _expectation(
            source,
            declared_video_pix_fmt="yuv420p",
            declared_conversions=("pix_fmt:yuv420p10le->yuv420p",),
        ),
    )


def test_declared_pix_fmt_rejects_output_mismatch_and_missing_record() -> None:
    source = _facts(pix_fmt="yuv420p10le")
    with pytest.raises(NormalizeVerificationError, match="declared yuv420p"):
        _verify_video_shape(
            _facts(pix_fmt="yuv444p"),
            _expectation(
                source,
                declared_video_pix_fmt="yuv420p",
                declared_conversions=("pix_fmt:yuv420p10le->yuv420p",),
            ),
        )
    with pytest.raises(NormalizeVerificationError, match="record missing"):
        _verify_video_shape(
            _facts(pix_fmt="yuv420p"),
            _expectation(source, declared_video_pix_fmt="yuv420p"),
        )
