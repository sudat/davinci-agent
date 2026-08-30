"""Deterministic IR adapter for the T13 finishing harness.

The committed review plane stores ``TimelineIr0C`` (plan store ``ir-vN``);
the finishing compiler consumes ``TimelineIrV2``. This adapter is a pure
shape mapping — spans and text are copied verbatim, never re-derived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.cli._v44_finishing_build import FinishingMalformedError
from services.creative_plan.ir_models_v2 import (
    AudioItemV2,
    AudioTrackV2,
    PlacedClipV2,
    SubtitleCueV2,
    TimelineIrV2,
    VideoTrackV2,
)
from services.creative_plan.subtitle_models import AsrSegmentV1

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate
    from services.contracts.timeline_ir import TimelineIr0C


def ir0c_to_v2(ir: TimelineIr0C, episode_id: str) -> TimelineIrV2:
    """Map the review-plane IR onto IR v2 (identity provenance ids).

    The 0C plane carries no candidate/transcript provenance ids, so each
    item's own id IS its provenance. The review-plane audio mirrors the A/V
    edit, so every audio item maps onto the ``dialogue`` role.
    """

    video: list[PlacedClipV2] = []
    cues: list[SubtitleCueV2] = []
    audio: list[AudioItemV2] = []
    for track in ir.tracks:
        for item in track.items:
            if item.kind == "video":
                video.append(
                    PlacedClipV2(
                        item_id=item.item_id,
                        source=item.source,
                        record_span=item.record_span,
                        candidate_ref=item.item_id,
                        av_link_id=item.av_link_id,
                    )
                )
            elif item.kind == "subtitle":
                if not item.subtitle_text:
                    raise FinishingMalformedError(
                        "ir-invalid", f"subtitle item {item.item_id} carries no text"
                    )
                cues.append(
                    SubtitleCueV2(
                        cue_id=item.item_id,
                        text=item.subtitle_text,
                        record_span=item.record_span,
                        candidate_ref=item.item_id,
                        transcript_ref=item.item_id,
                    )
                )
            else:
                audio.append(
                    AudioItemV2(
                        item_id=item.item_id,
                        source=item.source,
                        record_span=item.record_span,
                        av_link_id=item.av_link_id,
                    )
                )
    if not video:
        raise FinishingMalformedError(
            "ir-invalid", "the committed IR carries no video items (empty primary)"
        )
    audio_tracks: tuple[AudioTrackV2, ...] = (
        (AudioTrackV2(role="dialogue", track_id="a-dialogue", items=tuple(audio)),)
        if audio
        else ()
    )
    return TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id=episode_id,
        rate=ir.rate,
        video_tracks=(VideoTrackV2(role="primary", track_id="v-primary", items=tuple(video)),),
        subtitle_cues=tuple(cues),
        audio_tracks=audio_tracks,
    )


def asr_segments_from_cues(
    ir_v2: TimelineIrV2, source_id: str
) -> tuple[tuple[AsrSegmentV1, ...], int]:
    """Restore committed record-timed cues to source timing for reconciliation."""

    seconds = _seconds_per_frame(ir_v2.rate)
    primary = next(track for track in ir_v2.video_tracks if track.role == "primary")
    segments: list[AsrSegmentV1] = []
    for cue in ir_v2.subtitle_cues:
        clip = next(
            clip
            for clip in primary.items
            if clip.record_span.start_frame <= cue.record_span.start_frame
            and cue.record_span.end_frame <= clip.record_span.end_frame
        )
        source_start = (
            clip.source.span.start_frame
            + cue.record_span.start_frame
            - clip.record_span.start_frame
        )
        source_end = source_start + cue.record_span.length
        segments.append(
            AsrSegmentV1(
                segment_id=f"asr-{cue.cue_id}",
                source_id=source_id,
                text=cue.text,
                start_seconds=source_start * seconds,
                end_seconds=source_end * seconds,
            )
        )
    total = max((clip.source.span.end_frame for clip in primary.items), default=0)
    return tuple(segments), total


def _seconds_per_frame(rate: RationalFrameRate) -> float:
    return rate.den / rate.num


__all__ = ["asr_segments_from_cues", "ir0c_to_v2"]
