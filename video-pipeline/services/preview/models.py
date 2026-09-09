"""Strict input-binding and trace-manifest models for the pinned-ffmpeg preview adapter.

The preview adapter is Resolve-free by contract (``services.preview`` must never
import ``services.resolve_bridge``); every value here is integer-exact — frame
counts, millisecond bounds, sample-rate integers — with no floats anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    Sha256,
    SourceFrameSpan,
    StrictModel,
)
from services.contracts.styled_presentation import (  # noqa: TC001 (pydantic runtime)
    SubtitleStyleParams,
)
from services.contracts.timeline_ir import (  # noqa: TC001 (pydantic runtime)
    TimelineItem0C,
)
from services.preview.errors import (
    PreviewBindingError,
    PreviewError,
    PreviewLayoutError,
    PreviewRenderError,
    PreviewToolchainError,
    PreviewTraceError,
    PreviewVerificationError,
)


def _tuple[Value](value: list[Value] | tuple[Value, ...]) -> tuple[Value, ...]:
    return tuple(value)


type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(_tuple)]


class MediaBinding(StrictModel):
    media_path: str
    sha256: Sha256

    @field_validator("media_path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise PydanticCustomError("relative_path", "media bindings must be absolute paths")
        return value


class ItemBinding(StrictModel):
    item_id: Identifier
    binding: MediaBinding


class PreviewMediaBindings(StrictModel):
    items: Sequence[ItemBinding] = Field(min_length=1)
    bgm: MediaBinding | None = None

    @model_validator(mode="after")
    def require_unique_item_bindings(self) -> PreviewMediaBindings:
        ids = [entry.item_id for entry in self.items]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_binding", "item bindings must be unique")
        return self

    def binding_for(self, item_id: str) -> MediaBinding | None:
        for entry in self.items:
            if entry.item_id == item_id:
                return entry.binding
        return None


class PreviewLayout(StrictModel):
    """Validated, render-ready projection of a Timeline IR subset."""

    rate: RationalFrameRate
    video_items: Sequence[TimelineItem0C] = Field(min_length=1)
    audio_items: Sequence[TimelineItem0C] = Field(min_length=1)
    subtitle_items: Sequence[TimelineItem0C] = ()
    total_record_frames: int = Field(gt=0, strict=True)
    samples_per_frame: int = Field(gt=0, strict=True)


class TraceInput(StrictModel):
    item_id: Identifier
    kind: Literal["video", "audio", "subtitle"]
    media_path: str
    sha256: Sha256
    source_span: SourceFrameSpan
    record_span: RecordFrameSpan


class TraceDecision(StrictModel):
    decision_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    classification: Literal["clear", "ambiguous", "conflict", "initial"]
    applied: bool
    plan_version_after: str = Field(min_length=1)


class RecordDecisionSpan(StrictModel):
    span: RecordFrameSpan
    decision_id: str = Field(min_length=1)


class StrategyNotes(StrictModel):
    subtitle_rung: str
    overlay_strategy: str
    audio_strategy: str
    determinism_policy: Literal["semantic-equivalence-h264-videotoolbox"]


class FfprobeSummary(StrictModel):
    stream_count: int = Field(gt=0, strict=True)
    video_codec: str
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    r_frame_rate: str
    avg_frame_rate: str
    nb_read_frames: int = Field(gt=0, strict=True)
    video_duration_ms: int = Field(gt=0, strict=True)
    container_duration_ms: int = Field(gt=0, strict=True)
    audio_codec: str
    audio_sample_rate: int = Field(gt=0, strict=True)
    audio_channels: int = Field(gt=0, strict=True)
    subtitle_codec: str | None


class PreviewFile(StrictModel):
    path: str
    sha256: Sha256
    size: int = Field(ge=0, strict=True)
    decoded_video_sha256: Sha256


class TimelineBinding(StrictModel):
    plan_version: str = Field(min_length=1)
    ir_sha256: Sha256
    total_record_frames: int = Field(gt=0, strict=True)
    timeline_rate: RationalFrameRate


class TraceStyleTable(StrictModel):
    """The recorded soft-sub style table (Todo 57): metadata only, never burn-in.

    The preview MP4 keeps its ``mov_text`` soft-subtitle track; the profile's
    resolved style parameters and per-cue style ids are recorded here so the
    editorial review can see exactly which presentation would be applied at
    finalization. Pixel content is untouched by styling.
    """

    style_id: Identifier
    params: SubtitleStyleParams
    cue_style_ids: tuple[Identifier, ...]
    titled_item_ids: tuple[Identifier, ...]
    styled_presentation_sha256: Sha256


class PreviewTraceManifest(StrictModel):
    schema_version: Literal["preview-trace-v1"]
    output_id: Literal["landscape", "vertical"] = "landscape"
    preview: PreviewFile
    timeline_binding: TimelineBinding
    inputs: Sequence[TraceInput] = Field(min_length=1)
    decisions: Sequence[TraceDecision] = Field(min_length=1)
    record_to_decision: Sequence[RecordDecisionSpan] = Field(min_length=1)
    strategy_notes: StrategyNotes
    ffprobe_summary: FfprobeSummary
    presentation_style: TraceStyleTable | None = None

    @model_validator(mode="after")
    def require_full_record_coverage(self) -> PreviewTraceManifest:
        known_ids = {entry.decision_id for entry in self.decisions}
        if len(known_ids) != len(self.decisions):
            raise PydanticCustomError("duplicate_decision", "decision ids must be unique")
        total = self.timeline_binding.total_record_frames
        cursor = 0
        for entry in self.record_to_decision:
            span = entry.span
            if span.end_frame <= span.start_frame:
                raise PydanticCustomError("empty_coverage", "coverage spans must be non-empty")
            if span.start_frame != cursor:
                raise PydanticCustomError(
                    "coverage_broken",
                    "coverage gap or overlap at {start}: expected {cursor}",
                    {"start": span.start_frame, "cursor": cursor},
                )
            if entry.decision_id not in known_ids:
                raise PydanticCustomError(
                    "unknown_decision",
                    "coverage references unknown decision {decision_id}",
                    {"decision_id": entry.decision_id},
                )
            cursor = span.end_frame
        if cursor != total:
            raise PydanticCustomError(
                "coverage_short",
                "coverage ends at {cursor} but the timeline holds {total} frames",
                {"cursor": cursor, "total": total},
            )
        return self


class AppliedDecision(StrictModel):
    """A review decision that was applied before this preview regeneration.

    The preview adapter only renders post-apply states, so the classification
    is pinned to ``clear``; deferred decisions keep the previous preview.

    ``plan_sha256`` is the review-store ``versions.json`` entry hash
    (``VersionEntry.plan_sha256`` in ``services/review_command/store.py``)
    recorded for ``plan_version_after`` — the content sha the decision
    provably refers to. The preview guard compares it against the rendered
    edit plan's own content sha, so a decision whose version counter ran
    ahead of the plan's own version label (content-identical re-commit)
    still renders, while a genuinely stale plan is refused.
    """

    decision_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    classification: Literal["clear"]
    plan_version_after: str = Field(min_length=1)
    plan_sha256: Sha256 | None = None
    previous_trace: PreviewTraceManifest


__all__ = [
    "AppliedDecision",
    "FfprobeSummary",
    "ItemBinding",
    "MediaBinding",
    "PreviewBindingError",
    "PreviewError",
    "PreviewFile",
    "PreviewLayout",
    "PreviewLayoutError",
    "PreviewMediaBindings",
    "PreviewRenderError",
    "PreviewToolchainError",
    "PreviewTraceError",
    "PreviewTraceManifest",
    "PreviewVerificationError",
    "RecordDecisionSpan",
    "StrategyNotes",
    "TimelineBinding",
    "TraceDecision",
    "TraceInput",
    "TraceStyleTable",
]
