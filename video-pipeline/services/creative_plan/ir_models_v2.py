"""Timeline IR v2 models (task 31; PRD §12).

The NLE-neutral representation the compiler emits and tasks 32-35, 38, 60
consume: multiple video tracks with roles (primary/b_roll/insert/still/
graphic), subtitle cues, audio roles (dialogue/music/ambience/sfx), semantic
transitions, effect intents, presentation intent refs (task-32 seam),
source-to-record relationships (every placed span carries its SourceRef +
record frame span), and Decision ID linkage (every placed span carries the
MomentCandidateV2 ``candidate_ref`` it came from — the compiler refuses refs
outside the selection, so no orphan placements survive).

NO Resolve-specific fields anywhere (ResolveFreeModel rejects ``resolve*``
keys); Resolve execution detail lives in task 38's McpExecutionPlan. The
0C/v1 contracts module (services/contracts/timeline_ir.py) stays frozen.

Layout invariants enforced AT THE MODEL (hand-built IRs are checked too):
unique track roles/ids per kind, globally unique item ids, primary present /
in record order / starting at frame 0 / contiguous (gaps and overlaps are
typed errors), non-overlapping items on every other track, unique
non-overlapping cues, unique effect/transition ids.

Tuple coercion: every ``tuple`` field carries ``BeforeValidator(to_tuple)``
so JSON lists round-trip (cf. tasks 12/22/24).
"""

# allow: SIZE_OK — pure model definitions + their layout validators, mirroring
# services/contracts/timeline_ir.py; splitting further would separate the
# invariants from the model they guard. Task-23/29 single-module precedent.

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    ResolveFreeModel,
    SourceRef,
    to_tuple,
)

_Frames = Annotated[int, Field(gt=0, strict=True)]


VideoTrackRoleV2 = Literal["primary", "b_roll", "insert", "still", "graphic"]
AudioTrackRoleV2 = Literal["dialogue", "music", "ambience", "sfx"]
EffectKindV2 = Literal[
    "punch_in",
    "crop",
    "color_look",
    "voice_isolation",
    "noise_cleanup",
    "ducking",
    "speed_change",
    "manual_required",
]


class PlacedClipV2(ResolveFreeModel):
    """One placed video span: handled source span + record span + decision."""

    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    candidate_ref: Identifier
    av_link_id: Identifier | None = None


class SubtitleCueV2(ResolveFreeModel):
    """One subtitle cue mapped onto the record timeline."""

    cue_id: Identifier
    text: Annotated[str, Field(min_length=1, strict=True)]
    record_span: RecordFrameSpan
    candidate_ref: Identifier
    transcript_ref: Identifier


class AudioItemV2(ResolveFreeModel):
    """One placed audio span (dialogue mirror, music bed, SFX)."""

    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    candidate_ref: Identifier | None = None
    av_link_id: Identifier | None = None


class SemanticTransitionV2(ResolveFreeModel):
    """A semantic transition at one primary boundary (Resolve-free)."""

    transition_id: Identifier
    style: Literal["dissolve", "motion", "other"]
    at_record_frame: Frame
    a_item_ref: Identifier
    b_item_ref: Identifier
    duration_frames: _Frames


class EffectIntentV2(ResolveFreeModel):
    """A semantic effect/treatment intent targeting an item or the timeline."""

    effect_id: Identifier
    kind: EffectKindV2
    target_item_id: Identifier | None = None
    note: str = ""
    speed_factor_pct: _Frames | None = None


class VideoTrackV2(ResolveFreeModel):
    role: VideoTrackRoleV2
    track_id: Identifier
    items: Annotated[tuple[PlacedClipV2, ...], BeforeValidator(to_tuple)] = Field(min_length=1)


class AudioTrackV2(ResolveFreeModel):
    role: AudioTrackRoleV2
    track_id: Identifier
    items: Annotated[tuple[AudioItemV2, ...], BeforeValidator(to_tuple)] = Field(min_length=1)


class TimelineIrV2(ResolveFreeModel):
    """The NLE-neutral Timeline IR v2 (PRD §12) — no Resolve specifics."""

    schema_version: Literal["timeline-ir-v2"]
    episode_id: Identifier
    rate: RationalFrameRate
    video_tracks: Annotated[tuple[VideoTrackV2, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )
    subtitle_cues: Annotated[tuple[SubtitleCueV2, ...], BeforeValidator(to_tuple)] = ()
    audio_tracks: Annotated[tuple[AudioTrackV2, ...], BeforeValidator(to_tuple)] = ()
    transitions: Annotated[tuple[SemanticTransitionV2, ...], BeforeValidator(to_tuple)] = ()
    effect_intents: Annotated[tuple[EffectIntentV2, ...], BeforeValidator(to_tuple)] = ()
    presentation_intent_refs: Annotated[tuple[Identifier, ...], BeforeValidator(to_tuple)] = ()

    @model_validator(mode="after")
    def require_coherent_layout(self) -> TimelineIrV2:
        _check_tracks(self.video_tracks, self.audio_tracks)
        _check_primary(self.video_tracks)
        _check_cues(self.subtitle_cues)
        _check_ids(self)
        return self


def _check_tracks(video: tuple[VideoTrackV2, ...], audio: tuple[AudioTrackV2, ...]) -> None:
    for tracks, label in ((video, "video"), (audio, "audio")):
        roles = [track.role for track in tracks]
        ids = [track.track_id for track in tracks]
        if len(set(roles)) != len(roles):
            raise PydanticCustomError(
                "duplicate_track_role", "{label} roles are unique", {"label": label}
            )
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_track_id", "track ids are unique")
        for track in tracks:
            spans = sorted(
                (item.record_span.start_frame, item.record_span.end_frame) for item in track.items
            )
            for earlier, later in pairwise(spans):
                if later[0] < earlier[1]:
                    raise PydanticCustomError(
                        "record_overlap",
                        "{role} items overlap on the record",
                        {"role": track.role},
                    )


def _check_primary(video: tuple[VideoTrackV2, ...]) -> None:
    primary = next((t for t in video if t.role == "primary"), None)
    if primary is None:
        raise PydanticCustomError("missing_primary", "a primary video track is required")
    ordered = sorted(primary.items, key=lambda item: (item.record_span.start_frame, item.item_id))
    if [item.item_id for item in ordered] != [item.item_id for item in primary.items]:
        raise PydanticCustomError("primary_order", "primary items are in record order")
    if ordered[0].record_span.start_frame != 0:
        raise PydanticCustomError("primary_start", "primary starts at record frame 0")
    for earlier, later in pairwise(ordered):
        if later.record_span.start_frame > earlier.record_span.end_frame:
            raise PydanticCustomError(
                "primary_gap",
                "primary gap at record frame {frame}",
                {"frame": earlier.record_span.end_frame},
            )
        if later.record_span.start_frame < earlier.record_span.end_frame:
            raise PydanticCustomError("primary_overlap", "primary items overlap")


def _check_cues(cues: tuple[SubtitleCueV2, ...]) -> None:
    cue_ids = [cue.cue_id for cue in cues]
    if len(set(cue_ids)) != len(cue_ids):
        raise PydanticCustomError("duplicate_cue", "cue ids are unique")
    spans = sorted((cue.record_span.start_frame, cue.record_span.end_frame) for cue in cues)
    for earlier, later in pairwise(spans):
        if later[0] < earlier[1]:
            raise PydanticCustomError("cue_overlap", "subtitle cues overlap on the record")


def _check_ids(ir: TimelineIrV2) -> None:
    item_ids = [item.item_id for track in ir.video_tracks + ir.audio_tracks for item in track.items]
    if len(set(item_ids)) != len(item_ids):
        raise PydanticCustomError("duplicate_item", "item ids are globally unique")
    effect_ids = [effect.effect_id for effect in ir.effect_intents]
    if len(set(effect_ids)) != len(effect_ids):
        raise PydanticCustomError("duplicate_effect", "effect ids are unique")
    transition_ids = [t.transition_id for t in ir.transitions]
    if len(set(transition_ids)) != len(transition_ids):
        raise PydanticCustomError("duplicate_transition", "transition ids are unique")


__all__ = [
    "AudioItemV2",
    "AudioTrackRoleV2",
    "AudioTrackV2",
    "EffectIntentV2",
    "EffectKindV2",
    "PlacedClipV2",
    "SemanticTransitionV2",
    "SubtitleCueV2",
    "TimelineIrV2",
    "VideoTrackRoleV2",
    "VideoTrackV2",
]
