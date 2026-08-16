"""Drop/duplicate prediction via the frozen conform frame-conversion model.

The source span is derived from decode-level observations of the INPUT
(``nb_read_frames`` from ``-count_frames`` and the tick-exact stream
duration), then handed to ``services.conform`` ``frame_conversion_accounting``
— the same frozen model that reproduces the Phase-0B golden tables. The
prediction is never taken from ffmpeg's exit code or progress output.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from services.conform.convert import frame_conversion_accounting
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan
from services.normalize.probe import VideoFacts, stream_duration_seconds

if TYPE_CHECKING:
    from services.conform.rate_model import CfrConversionReport


class SpanDerivationError(ValueError):
    """The probed input cannot yield an exact rational source span."""


@dataclass(frozen=True, slots=True)
class SourceSpan:
    frames: int
    rate: RationalFrameRate


def source_span_from_probe(video: VideoFacts) -> SourceSpan:
    """Exact rational source rate = decoded frames / tick-exact duration."""

    duration = stream_duration_seconds(video)
    if video.nb_read_frames <= 0 or duration <= 0:
        raise SpanDerivationError(
            f"undecodable or empty video span: frames={video.nb_read_frames}, "
            f"duration={duration}"
        )
    rate = Fraction(video.nb_read_frames) / duration
    return SourceSpan(
        frames=video.nb_read_frames,
        rate=RationalFrameRate(num=rate.numerator, den=rate.denominator),
    )


def expected_conversion(
    span: SourceSpan, target_rate: RationalFrameRate
) -> CfrConversionReport:
    """Frozen drop/dup accounting for the whole converted span."""

    return frame_conversion_accounting(
        SourceFrameSpan(start_frame=0, end_frame=span.frames, rate=span.rate),
        target_rate,
    )
