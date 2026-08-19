"""Resolve Package value models: the NLE-instruction manifest (not API calls).

A package names Resolve operations (that is its purpose) but carries only
capability-matrix-validated ones: base_cut placements via AppendToTimeline
clipInfo records, the fixed subtitle as an EXTERNAL post-render mov_text step
anchored to record frames, and a render job spec bound to the
CompletionPercentage==100 contract with SelectAllFrames and the fixed preset.
All fields are strictly typed integers/strings — no floats anywhere.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    Identifier,
    RationalFrameRate,
    Sha256,
    StrictModel,
)
from services.resolve_adapter.presentation_models import (  # noqa: TC001 (pydantic resolves annotations at runtime)
    PresentationSection,
)

type TrackType = Literal["video", "audio"]

SUBTITLE_MUX_ARGV: Final[tuple[str, ...]] = (
    "{ffmpeg}",
    "-nostdin",
    "-y",
    "-v",
    "error",
    "-i",
    "{render}",
    "-i",
    "{srt}",
    "-map",
    "0:v",
    "-map",
    "0:a",
    "-map",
    "1:0",
    "-c:v",
    "copy",
    "-c:a",
    "copy",
    "-c:s",
    "mov_text",
    "-metadata:s:s:0",
    "language=eng",
    "{output}",
)


class ClipInfo(StrictModel):
    media_source_id: Identifier
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)
    track_type: TrackType
    track_index: int = Field(gt=0, strict=True)
    record_frame: int = Field(ge=0, strict=True)


class AppendPlacement(StrictModel):
    item_id: Identifier
    media_source_id: Identifier
    capability: Literal["base_cut", "media_intro_outro"] = "base_cut"
    api_operation: Literal["AppendToTimeline"] = "AppendToTimeline"
    clip_info: ClipInfo


class LinkGroup(StrictModel):
    av_link_id: Identifier
    item_ids: tuple[Identifier, ...] = Field(min_length=2)


class SubtitleCueInstruction(StrictModel):
    cue_id: Identifier
    text: str = Field(min_length=1, strict=True)
    lines: tuple[str, ...] = Field(min_length=1)
    style_ref: Identifier
    min_duration_frames: int = Field(gt=0, strict=True)
    anchor_record_start_frame: int = Field(ge=0, strict=True)
    anchor_record_end_frame: int = Field(gt=0, strict=True)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_forward_cue(self) -> SubtitleCueInstruction:
        if self.end_ms <= self.start_ms or self.anchor_record_end_frame <= (
            self.anchor_record_start_frame
        ):
            raise PydanticCustomError("cue_span", "cue timings must be forward")
        return self


class SubtitlePostRenderStep(StrictModel):
    rung: Literal["external"] = "external"
    capability: Literal["fixed_subtitle"] = "fixed_subtitle"
    mux_operation: Literal["ffmpeg-mov-text"] = "ffmpeg-mov-text"
    argv: tuple[str, ...] = Field(min_length=1, default=SUBTITLE_MUX_ARGV)
    cues: tuple[SubtitleCueInstruction, ...] = Field(min_length=1)


class RenderCompletionRule(StrictModel):
    field: Literal["CompletionPercentage"] = "CompletionPercentage"
    value: Literal[100] = 100
    status_strings_parsed: Literal[False] = False
    marks_bound_render_extent: Literal[False] = False


class RenderJobSpec(StrictModel):
    capability: Literal["render"] = "render"
    video_format: str = Field(min_length=1)
    video_codec: str = Field(min_length=1)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    frame_rate: RationalFrameRate
    audio_codec: str = Field(min_length=1)
    audio_sample_rate: int = Field(gt=0, strict=True)
    audio_channels: int = Field(gt=0, strict=True)
    select_all_frames: Literal[True] = True
    timeline_start_timecode: str = Field(min_length=1)
    frame_origin: int = Field(ge=0, strict=True)
    extent_frames: int = Field(gt=0, strict=True)
    completion: RenderCompletionRule = RenderCompletionRule()


class AppendTrackMapEntry(StrictModel):
    logical_kind: Literal["video", "audio"]
    logical_index: int = Field(gt=0, strict=True)
    resolve_track_type: TrackType
    resolve_track_index: int = Field(gt=0, strict=True)


class ExternalTrackMapEntry(StrictModel):
    logical_kind: Literal["subtitle"]
    logical_index: int = Field(gt=0, strict=True)
    placement: Literal["post-render-external"] = "post-render-external"


class RoleTrackMapEntry(StrictModel):
    """A role-separated audio track: dialogue and ambient never share one."""

    logical_kind: Literal["audio"] = "audio"
    role: Literal["dialogue", "ambient"]
    resolve_track_type: Literal["audio"] = "audio"
    resolve_track_index: int = Field(gt=0, strict=True)


type TrackMapEntry = (
    AppendTrackMapEntry | ExternalTrackMapEntry | RoleTrackMapEntry
)


class TimelineView(StrictModel):
    frame_rate: RationalFrameRate
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    audio_sample_rate: int = Field(gt=0, strict=True)
    start_timecode: str = Field(min_length=1)
    frame_origin: int = Field(ge=0, strict=True)


class MediaBinding(StrictModel):
    source_id: Identifier
    path: str = Field(min_length=1)
    sha256: Sha256
    duration_frames: int = Field(gt=0, strict=True)


class InputsView(StrictModel):
    capability_matrix_path: str
    capability_matrix_sha256: Sha256
    toolchain_lock_sha256: Sha256
    declared_media: tuple[MediaBinding, ...] = Field(min_length=1)


class ResolvePackage(ArtifactEnvelope[Literal["resolve_package_v1"]]):
    timeline: TimelineView
    track_map: tuple[TrackMapEntry, ...]
    placements: tuple[AppendPlacement, ...]
    link_groups: tuple[LinkGroup, ...]
    subtitle_step: SubtitlePostRenderStep | None = None
    render_job: RenderJobSpec
    inputs_view: InputsView
    presentation: PresentationSection | None = None


__all__ = [
    "SUBTITLE_MUX_ARGV",
    "AppendPlacement",
    "AppendTrackMapEntry",
    "ClipInfo",
    "ExternalTrackMapEntry",
    "InputsView",
    "LinkGroup",
    "MediaBinding",
    "RenderCompletionRule",
    "RenderJobSpec",
    "ResolvePackage",
    "RoleTrackMapEntry",
    "SubtitleCueInstruction",
    "SubtitlePostRenderStep",
    "TimelineView",
    "TrackMapEntry",
    "TrackType",
]
