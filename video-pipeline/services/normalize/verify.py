"""Output verification: expected-fields comparison plus hard frame accounting.

Every expected field (CFR rate, sample rate, codecs, dimensions, pixel format,
rotation, color metadata, timescale) is compared against the decode-verified
ffprobe facts of the produced Edit Mezzanine; a missing or changed field is a
``silent_metadata_loss`` block. The decoded frame count must EXACTLY equal the
conform-accounting prediction — any drift is the hard
``unexplained_frame_count`` failure, regardless of ffmpeg's exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.contracts.primitives import RationalFrameRate
from services.normalize.errors import NormalizeVerificationError
from services.normalize.models import TargetProfile

if TYPE_CHECKING:
    from services.normalize.probe import MediaFacts
    from services.toolchain.normalization import NormalizeTarget

COLOR_FIELDS = ("color_space", "color_transfer", "color_primaries", "color_range")


@dataclass(frozen=True, slots=True)
class OutputExpectation:
    target: NormalizeTarget
    source: MediaFacts
    expected_output_frames: int
    declared_video_pix_fmt: str | None = None
    declared_scale_height: int | None = None
    declared_conversions: tuple[str, ...] = ()


def _loss(field: str, detail: str) -> NormalizeVerificationError:
    return NormalizeVerificationError("silent_metadata_loss", f"{field}: {detail}")


def expected_scaled_width(source_width: int, source_height: int, scale_height: int) -> int:
    """Even width ffmpeg's ``scale=-2:<height>`` produces for a source frame."""
    return 2 * round(source_width * scale_height / (2 * source_height))


def _require_rate(facts: MediaFacts, field: str, num: int, den: int) -> None:
    actual_num = getattr(facts.video, f"{field}_num")
    actual_den = getattr(facts.video, f"{field}_den")
    if (actual_num, actual_den) != (num, den):
        raise _loss(field, f"expected {num}/{den}, found {actual_num}/{actual_den}")


def _verify_audio(facts: MediaFacts, expectation: OutputExpectation) -> None:
    target = expectation.target
    source_audio = expectation.source.audio
    if facts.audio is None:
        raise _loss("audio", "recipe dropped the audio stream")
    if facts.audio.codec_name != target.audio_codec:
        raise _loss("audio codec", f"expected {target.audio_codec}")
    if facts.audio.sample_rate != target.sample_rate:
        raise _loss("sample_rate", f"expected {target.sample_rate} Hz")
    expected_channels = source_audio.channels if source_audio is not None else 1
    if facts.audio.channels != expected_channels:
        raise _loss("channels", "audio channel count changed")


def _verify_dimensions(facts: MediaFacts, expectation: OutputExpectation) -> None:
    video = facts.video
    source_video = expectation.source.video
    if expectation.declared_scale_height is None:
        if (video.width, video.height) != (source_video.width, source_video.height):
            raise _loss(
                "dimensions",
                f"expected {source_video.width}x{source_video.height}, "
                f"found {video.width}x{video.height}",
            )
        return
    expected_width = expected_scaled_width(
        source_video.width, source_video.height, expectation.declared_scale_height
    )
    expected_height = expectation.declared_scale_height
    if (video.width, video.height) != (expected_width, expected_height):
        raise _loss(
            "dimensions",
            f"expected declared scale {expected_width}x{expected_height}, "
            f"found {video.width}x{video.height}",
        )
    conversion = (
        f"scale:{source_video.width}x{source_video.height}"
        f"->{expected_width}x{expected_height}"
    )
    if conversion not in expectation.declared_conversions:
        raise _loss("dimensions", f"declared conversion record missing {conversion}")


def _verify_video_shape(facts: MediaFacts, expectation: OutputExpectation) -> None:
    video = facts.video
    source_video = expectation.source.video
    target = expectation.target
    if video.codec_name != "h264":
        raise _loss("video codec", f"expected h264, found {video.codec_name}")
    _require_rate(facts, "r_frame_rate", target.frame_rate.num, target.frame_rate.den)
    _require_rate(facts, "avg_frame_rate", target.frame_rate.num, target.frame_rate.den)
    _verify_dimensions(facts, expectation)
    declared = expectation.declared_video_pix_fmt
    if declared is None:
        if video.pix_fmt != source_video.pix_fmt:
            raise _loss("pix_fmt", f"expected {source_video.pix_fmt}")
    else:
        if video.pix_fmt != declared:
            raise _loss("pix_fmt", f"expected declared {declared}")
        conversion = f"pix_fmt:{source_video.pix_fmt}->{declared}"
        if conversion not in expectation.declared_conversions:
            raise _loss("pix_fmt", f"declared conversion record missing {conversion}")
    if (video.time_base_num, video.time_base_den) != (1, target.video_track_timescale):
        raise _loss(
            "video_track_timescale",
            f"expected 1/{target.video_track_timescale}, found "
            f"{video.time_base_num}/{video.time_base_den}",
        )


def _verify_preserved_metadata(
    facts: MediaFacts, expectation: OutputExpectation
) -> None:
    video, source_video = facts.video, expectation.source.video
    if video.rotation_degrees != source_video.rotation_degrees:
        raise _loss(
            "rotation",
            f"expected rotation metadata preserved "
            f"({source_video.rotation_degrees}), found {video.rotation_degrees}",
        )
    for field in COLOR_FIELDS:
        if getattr(video, field) != getattr(source_video, field):
            raise _loss(
                field,
                f"expected {getattr(source_video, field)!r} preserved, "
                f"found {getattr(video, field)!r}",
            )


def verify_output(facts: MediaFacts, expectation: OutputExpectation) -> None:
    """Compare every expected field; frame count must match exactly."""

    _verify_audio(facts, expectation)
    _verify_video_shape(facts, expectation)
    _verify_preserved_metadata(facts, expectation)
    expected_frames = expectation.expected_output_frames
    if facts.video.nb_read_frames != expected_frames:
        raise NormalizeVerificationError(
            "unexplained_frame_count",
            f"unexplained frame count: conform accounting predicted "
            f"{expected_frames} output frames, decode observed "
            f"{facts.video.nb_read_frames}",
        )


def target_profile_from_lock(target: NormalizeTarget) -> TargetProfile:
    return TargetProfile(
        frame_rate=RationalFrameRate(
            num=target.frame_rate.num, den=target.frame_rate.den
        ),
        sample_rate=target.sample_rate,
        video_codec=target.video_codec,
        audio_codec=target.audio_codec,
        container=target.container,
        video_track_timescale=target.video_track_timescale,
    )
