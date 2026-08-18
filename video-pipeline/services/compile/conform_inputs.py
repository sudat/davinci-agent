"""Exact anchor resolution over the edit-source geometry (Todo 44).

Every plan item and transcript cue span must resolve against one edit source:
matching frame rate, span within extent. Audio-sample spans convert to edit
frames ONLY through :mod:`services.conform` exact rational arithmetic — a
sample boundary that does not land exactly on the frame lattice is a typed
lossy-conversion error, never a silent rounding (PRD 9.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from pydantic import Field

from services.compile.production_errors import CompileProductionError
from services.compile.subtitle_policy import FrameCueSpan
from services.conform.convert import video_frame_for_pts
from services.conform.coordinates import OriginalTimestamp, RationalTimeBase
from services.contracts.primitives import (
    PositiveInteger,
    RationalFrameRate,
    SourceId,
    StrictModel,
)

if TYPE_CHECKING:
    from services.compile.subtitle_policy import TranscriptCueSource
    from services.plan.edit_plan_models import EditPlan


class EditSourceGeometry(StrictModel):
    """The conform anchor domain: one edit source, its rate, extent, rate pair."""

    source_id: SourceId
    frame_rate: RationalFrameRate
    total_frames: int = Field(gt=0, strict=True)
    audio_sample_rate: PositiveInteger


@dataclass(frozen=True, slots=True)
class ResolvedCue:
    """A transcript cue span resolved onto the edit-source frame axis."""

    segment_id: str
    text: str
    start_frame: int
    end_frame: int


def exact_frame_for_sample(sample: int, geometry: EditSourceGeometry) -> int:
    """Sample -> edit frame via the conform layer; lossy boundaries are errors."""

    timestamp = OriginalTimestamp(
        pts=sample, time_base=RationalTimeBase(num=1, den=geometry.audio_sample_rate)
    )
    exact = timestamp.seconds * geometry.frame_rate.as_fraction
    if exact.denominator != 1:
        raise CompileProductionError(
            "sample_conversion_lossy",
            f"sample {sample} at {geometry.audio_sample_rate} Hz does not land on an "
            f"exact {geometry.frame_rate.num}/{geometry.frame_rate.den} frame boundary "
            f"({exact})",
        )
    frame = video_frame_for_pts(timestamp, geometry.frame_rate)
    if frame != exact.numerator:  # pragma: no cover - frozen assignment rule
        raise CompileProductionError(
            "sample_conversion_lossy",
            f"conform assignment {frame} disagrees with the exact frame {exact}",
        )
    return frame


def total_audio_samples(geometry: EditSourceGeometry) -> int:
    exact = Fraction(geometry.total_frames) / geometry.frame_rate.as_fraction * (
        geometry.audio_sample_rate
    )
    if exact.denominator != 1:
        raise CompileProductionError(
            "source_extent_lossy",
            "the edit source extent has no integral audio sample length "
            f"({exact.numerator}/{exact.denominator} samples)",
        )
    return exact.numerator


def resolve_plan_anchors(plan: EditPlan, geometry: EditSourceGeometry) -> None:
    """Every item span must resolve against the geometry (rate + extent)."""

    rate = geometry.frame_rate
    for item in plan.items:
        if (
            item.span.rate_num != rate.num
            or item.span.rate_den != rate.den
            or item.span.end_frame > geometry.total_frames
        ):
            raise CompileProductionError(
                "unresolved_anchor",
                f"item {item.item_id} span [{item.span.start_frame},"
                f"{item.span.end_frame}) at {item.span.rate_num}/{item.span.rate_den} "
                f"does not resolve against source {geometry.source_id} "
                f"({rate.num}/{rate.den}, {geometry.total_frames} frames)",
            )


def resolve_transcript_cues(
    transcript: TranscriptCueSource, geometry: EditSourceGeometry
) -> tuple[ResolvedCue, ...]:
    """Resolve every declared cue span onto the frame axis (exact or error)."""

    resolved: list[ResolvedCue] = []
    for segment in transcript.segments:
        span = segment.span
        if isinstance(span, FrameCueSpan):
            start, end = span.start_frame, span.end_frame
            if end > geometry.total_frames:
                raise CompileProductionError(
                    "unresolved_anchor",
                    f"cue {segment.segment_id} span [{start},{end}) exceeds the "
                    f"edit source extent {geometry.total_frames}",
                )
        else:
            if span.sample_rate != geometry.audio_sample_rate:
                raise CompileProductionError(
                    "sample_rate_mismatch",
                    f"cue {segment.segment_id} declares {span.sample_rate} Hz but the "
                    f"edit source is {geometry.audio_sample_rate} Hz",
                )
            if span.end_sample > total_audio_samples(geometry):
                raise CompileProductionError(
                    "unresolved_anchor",
                    f"cue {segment.segment_id} sample span exceeds the edit source",
                )
            start = exact_frame_for_sample(span.start_sample, geometry)
            end = exact_frame_for_sample(span.end_sample, geometry)
            if end <= start:
                raise CompileProductionError(
                    "sample_conversion_lossy",
                    f"cue {segment.segment_id} collapses to an empty frame span",
                )
        resolved.append(
            ResolvedCue(
                segment_id=segment.segment_id,
                text=segment.text,
                start_frame=start,
                end_frame=end,
            )
        )
    return tuple(resolved)


__all__ = [
    "EditSourceGeometry",
    "ResolvedCue",
    "exact_frame_for_sample",
    "resolve_plan_anchors",
    "resolve_transcript_cues",
    "total_audio_samples",
]
