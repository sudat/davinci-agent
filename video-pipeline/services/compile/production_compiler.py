"""The production Timeline IR compiler (Todo 44).

Compiles a committed Production Edit Plan (Todo 43) plus the declared
transcript cue sources into the production Timeline IR:

1. anchor resolution — every plan item and cue span resolves against the edit
   source geometry (rate + extent); sample spans convert through
   :mod:`services.conform` exact rational arithmetic only;
2. deterministic record placement — video track 1 / audio track 2 (frozen
   indexes), cursor-verified per track: record holes become explicit gap
   items (legal), overlaps are typed errors;
3. dialogue-span filtering + edit-boundary splitting — cue spans map through
   the kept video items' source→record correspondence; removed spans produce
   no cues, cues crossing an item boundary split at exact integer frames;
4. cue generation under the frozen Japanese formatting table
   (:mod:`services.compile.subtitle_cues`);
5. fail-closed QC (:mod:`services.compile.ir_qc`) — an IR that violates any
   rule never leaves the compiler.

No Resolve-specific field can enter the IR: the contracts reject them at the
model level, and QC re-scans the serialized document.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.compile.conform_inputs import (
    EditSourceGeometry,
    resolve_plan_anchors,
    resolve_transcript_cues,
)
from services.compile.ir_qc import require_ir_qc
from services.compile.record_placement import (
    AvPlacement,
    TrackAllocation,
    allocate_track,
    cue_pieces,
)
from services.compile.subtitle_cues import build_cue_item
from services.contracts.primitives import (
    ArtifactRef,
    Producer,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineGapItem,
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.compile.subtitle_policy import SubtitleQcPolicy, TranscriptCueSource
    from services.plan.edit_plan_models import EditPlan

PRODUCER: Final = Producer(name="production-compiler", version="1")
VIDEO_TRACK_INDEX: Final = 1
AUDIO_TRACK_INDEX: Final = 2
SUBTITLE_TRACK_INDEX: Final = 3


@dataclass(frozen=True, slots=True)
class CompileProductionResult:
    """The compiled IR plus deterministic drop metadata for short cue pieces."""

    ir: TimelineIrProduction
    dropped_cues: tuple[str, ...]

    def ir_sha256(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self.ir)).hexdigest()


def _placements(plan: EditPlan) -> tuple[AvPlacement, ...]:
    return tuple(
        AvPlacement(
            item_id=item.item_id,
            track_kind=item.track_kind,
            source_id=item.source_ref.source_id,
            source_start=item.span.start_frame,
            source_end=item.span.end_frame,
            record_start=item.record_span.start_frame,
            record_end=item.record_span.end_frame,
            av_link_id=item.link_group_id,
        )
        for item in plan.items
    )


def _gap_item(kind: str, index: int, start: int, end: int) -> TimelineGapItem:
    digest = hashlib.sha256(f"{kind}|{index}|{start}|{end}".encode()).hexdigest()
    return TimelineGapItem(
        item_id=f"gap.{digest[:24]}",
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
    )


def _track_items(
    kind: str,
    index: int,
    allocation: TrackAllocation,
    geometry: EditSourceGeometry,
) -> tuple[TimelineItem0C | TimelineGapItem, ...]:
    items: list[TimelineItem0C | TimelineGapItem] = []
    gap_queue = list(allocation.gaps)
    for placement in allocation.items:
        while gap_queue and gap_queue[0][0] < placement.record_start:
            start, end = gap_queue.pop(0)
            items.append(_gap_item(kind, index, start, end))
        items.append(
            TimelineItem0C(
                item_id=placement.item_id,
                kind=placement.track_kind,
                source=SourceRef(
                    source_id=placement.source_id,
                    span=SourceFrameSpan(
                        start_frame=placement.source_start,
                        end_frame=placement.source_end,
                        rate=geometry.frame_rate,
                    ),
                ),
                record_span=RecordFrameSpan(
                    start_frame=placement.record_start, end_frame=placement.record_end
                ),
                av_link_id=placement.av_link_id,
            )
        )
    for start, end in gap_queue:
        items.append(_gap_item(kind, index, start, end))
    return tuple(items)


def _content_hash(rate: EditSourceGeometry, tracks: tuple[TimelineTrackProduction, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(rate.frame_rate))
    for track in tracks:
        digest.update(canonical_model_bytes(track))
    return digest.hexdigest()


def compile_production(
    plan: EditPlan,
    geometry: EditSourceGeometry,
    transcript: TranscriptCueSource,
    policy: SubtitleQcPolicy,
    *,
    artifact_id: str,
) -> CompileProductionResult:
    """Deterministically compile the plan + transcript into the production IR."""

    resolve_plan_anchors(plan, geometry)
    resolved = resolve_transcript_cues(transcript, geometry)
    placements = _placements(plan)
    video = allocate_track("video", (p for p in placements if p.track_kind == "video"))
    audio = allocate_track("audio", (p for p in placements if p.track_kind == "audio"))

    dropped: list[str] = []
    cue_items: list[SubtitleCueItem] = []
    for piece in cue_pieces(video.items, resolved):
        item, reason = build_cue_item(piece, policy, geometry.frame_rate, geometry.source_id)
        if item is None:
            dropped.append(reason or f"{piece.segment_id}:dropped")
        else:
            cue_items.append(item)

    tracks: list[TimelineTrackProduction] = [
        TimelineTrackProduction(
            track=TrackRef0C(kind="video", index=VIDEO_TRACK_INDEX),
            items=_track_items("video", VIDEO_TRACK_INDEX, video, geometry),
        ),
        TimelineTrackProduction(
            track=TrackRef0C(kind="audio", index=AUDIO_TRACK_INDEX),
            items=_track_items("audio", AUDIO_TRACK_INDEX, audio, geometry),
        ),
    ]
    if cue_items:
        tracks.append(
            TimelineTrackProduction(
                track=TrackRef0C(kind="subtitle", index=SUBTITLE_TRACK_INDEX),
                items=tuple(cue_items),
            )
        )

    ir = TimelineIrProduction(
        artifact_id=artifact_id,
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash=_content_hash(geometry, tuple(tracks)),
        producer=PRODUCER,
        inputs=(
            ArtifactRef(
                artifact_id=plan.proposal_id,
                sha256=hashlib.sha256(canonical_model_bytes(plan)).hexdigest(),
            ),
        ),
        rate=geometry.frame_rate,
        tracks=tuple(tracks),
    )
    require_ir_qc(ir, policy)
    return CompileProductionResult(ir=ir, dropped_cues=tuple(dropped))


__all__ = ["CompileProductionResult", "compile_production"]
