from __future__ import annotations

from pathlib import Path

import pytest

from services.normalize.errors import NormalizeVerificationError
from services.normalize.probe import MediaFacts, VideoFacts
from services.normalize.toolchain_guard import load_phase0b_lock
from services.normalize.verify import OutputExpectation, _verify_video_shape


def _facts(*, width: int, height: int, pix_fmt: str = "yuv420p") -> MediaFacts:
    video = VideoFacts(
        codec_name="h264",
        width=width,
        height=height,
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
    declared_scale_height: int | None = None,
    declared_conversions: tuple[str, ...] = (),
) -> OutputExpectation:
    lock = load_phase0b_lock(Path("config/toolchains/phase-0b-v2.json"))
    return OutputExpectation(
        target=lock.normalization.target,
        source=source,
        expected_output_frames=1,
        declared_scale_height=declared_scale_height,
        declared_conversions=declared_conversions,
    )


def test_no_declared_scale_keeps_strict_dimensions() -> None:
    source = _facts(width=3840, height=2160)
    _verify_video_shape(_facts(width=3840, height=2160), _expectation(source))
    with pytest.raises(NormalizeVerificationError, match="dimensions"):
        _verify_video_shape(_facts(width=1920, height=1080), _expectation(source))


def test_declared_scale_accepts_even_width_hd_output() -> None:
    source = _facts(width=3840, height=2160)
    _verify_video_shape(
        _facts(width=1920, height=1080),
        _expectation(
            source,
            declared_scale_height=1080,
            declared_conversions=("scale:3840x2160->1920x1080",),
        ),
    )


def test_declared_scale_rejects_wrong_dims_and_missing_record() -> None:
    source = _facts(width=3840, height=2160)
    with pytest.raises(NormalizeVerificationError, match="declared scale"):
        _verify_video_shape(
            _facts(width=3840, height=2160),
            _expectation(
                source,
                declared_scale_height=1080,
                declared_conversions=("scale:3840x2160->1920x1080",),
            ),
        )
    with pytest.raises(NormalizeVerificationError, match="record missing"):
        _verify_video_shape(
            _facts(width=1920, height=1080),
            _expectation(source, declared_scale_height=1080),
        )
