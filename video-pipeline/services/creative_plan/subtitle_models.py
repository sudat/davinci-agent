"""Subtitle v2 artifact models (task 33; PRD §9, impl-plan 8.2).

Pure pydantic models for the Japanese subtitle pipeline — no LLM, no Resolve
fields (``ResolveFreeModel`` on everything that becomes plan payload).
Pipeline modules: ``subtitle_text`` (JA rules) -> ``subtitle_reconcile``
(edit reconciliation) -> ``subtitle_lines`` (chunk + reading speed) ->
``subtitle_plan`` (orchestration + capability ordering).

Coordinate contract (PRD §9.1): ``AsrSegmentV1`` carries seconds;
``SubtitleDraftCueV1``/``SubtitleReconciledCueV1`` carry Edit Source frames
(both source-side and, after reconciliation, record-side);
``SubtitlePlanCueV1`` carries the final record span plus its surviving source
span. All spans are non-empty half-open ``[start, end)`` ranges; the plan rate
is the shared Edit Mezzanine rate and every cue's source span must carry it.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    ResolveFreeModel,
    SourceFrameSpan,
    SourceId,
    StrictModel,
)

if TYPE_CHECKING:
    from services.creative_plan.subtitle_text import ProperNounDictionaryV1


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _to_float(value: object) -> object:
    return float(value) if isinstance(value, int) and not isinstance(value, bool) else value


class AsrSegmentV1(StrictModel):
    """One ASR transcript segment with timing in seconds (pre-normalization)."""

    segment_id: Identifier
    source_id: SourceId
    text: Annotated[str, Field(min_length=1, strict=True)]
    start_seconds: Annotated[float, Field(ge=0), BeforeValidator(_to_float)]
    end_seconds: Annotated[float, BeforeValidator(_to_float)]

    @model_validator(mode="after")
    def require_forward_seconds(self) -> AsrSegmentV1:
        if self.end_seconds <= self.start_seconds:
            raise PydanticCustomError(
                "span_inverted", "end_seconds must be greater than start_seconds"
            )
        return self


class SubtitleDraftCueV1(StrictModel):
    """One timed cue in Edit Source frames (timing normalized, text raw)."""

    cue_id: Identifier
    transcript_ref: Identifier
    source_id: SourceId
    text: Annotated[str, Field(min_length=1, strict=True)]
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward_span(self) -> SubtitleDraftCueV1:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "draft cue span is a non-empty half-open range")
        return self


class SubtitleReconciledCueV1(StrictModel):
    """One cue that survived the edit, mapped onto the record timeline.

    Record length equals the surviving source length: the native-rate 1:1
    mapping through the IR v2 primary placement. Reading-speed enforcement may
    later extend the RECORD span into following airtime (never the source).
    """

    cue_id: Identifier
    transcript_ref: Identifier
    source_id: SourceId
    text: Annotated[str, Field(min_length=1, strict=True)]
    start_frame: Frame
    end_frame: Frame
    record_start: Frame
    record_end: Frame

    @model_validator(mode="after")
    def require_coherent_spans(self) -> SubtitleReconciledCueV1:
        if self.end_frame <= self.start_frame or self.record_end <= self.record_start:
            raise PydanticCustomError("span_empty", "spans are non-empty half-open ranges")
        if self.end_frame - self.start_frame != self.record_end - self.record_start:
            raise PydanticCustomError(
                "record_length", "record length must equal surviving source length"
            )
        return self


class SubtitleStyleProfileV1(StrictModel):
    """Channel subtitle style knobs (PRD §9.2: configurable limits)."""

    profile_id: Identifier
    chars_per_line: Annotated[int, Field(gt=0, strict=True)] = 13
    lines_per_cue: Annotated[int, Field(gt=0, strict=True)] = 2
    reading_speed_min_cps: Annotated[float, Field(gt=0, strict=True)] = 5.0
    reading_speed_max_cps: Annotated[float, Field(gt=0, strict=True)] = 7.0

    @model_validator(mode="after")
    def require_speed_band(self) -> SubtitleStyleProfileV1:
        if self.reading_speed_min_cps >= self.reading_speed_max_cps:
            raise PydanticCustomError(
                "speed_band", "reading_speed_min_cps must be below reading_speed_max_cps"
            )
        return self


class SubtitlePlanCueV1(ResolveFreeModel):
    """One final subtitle cue: record frames + display lines (+ source span)."""

    cue_id: Identifier
    transcript_ref: Identifier
    source_id: SourceId
    lines: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)
    source_span: SourceFrameSpan
    record_span: RecordFrameSpan

    @model_validator(mode="after")
    def require_non_empty_record(self) -> SubtitlePlanCueV1:
        if self.record_span.end_frame <= self.record_span.start_frame:
            raise PydanticCustomError("span_empty", "record span is non-empty")
        return self


class ReconciliationRecordV1(StrictModel):
    """Explicit record of a cue dropped or trimmed by the edit (never silent)."""

    cue_id: Identifier
    transcript_ref: Identifier
    action: Literal["dropped", "trimmed"]
    detail: str
    source_frames_lost: Annotated[int, Field(ge=0, strict=True)]


class SubtitleReconciliationV1(StrictModel):
    """Result of ``reconcile_after_edit``: surviving cues + explicit records."""

    cues: Annotated[tuple[SubtitleReconciledCueV1, ...], BeforeValidator(_to_tuple)] = ()
    records: Annotated[tuple[ReconciliationRecordV1, ...], BeforeValidator(_to_tuple)] = ()


class TextProvenanceNoteV1(StrictModel):
    """Why cue text differs from the raw ASR transcript (never silent)."""

    transcript_ref: Identifier
    action: Literal[
        "punctuation_normalized",
        "filler_removed",
        "filler_retained",
        "proper_noun_substituted",
    ]
    detail: str


class ReadingSpeedViolationV1(StrictModel):
    """A cue outside the reading-speed band that could not be fixed (explicit)."""

    cue_id: Identifier
    transcript_ref: Identifier
    kind: Literal["reading_speed_violation", "reading_speed_under"]
    note: str
    measured_cps: Annotated[float, Field(ge=0, strict=True)]
    limit_cps: Annotated[float, Field(gt=0, strict=True)]


SubtitlePathKind = Literal["native_text_plus", "styled_template", "external_ass_srt", "manual"]

PATH_ORDER: tuple[SubtitlePathKind, ...] = (
    "native_text_plus",
    "styled_template",
    "external_ass_srt",
    "manual",
)


DEFAULT_STYLE_PROFILE: Final[SubtitleStyleProfileV1] = SubtitleStyleProfileV1(
    profile_id="subtitle-style-default"
)


class SubtitleBuildOptions(StrictModel):
    """Grouped knobs for the plan builder (defaults = channel defaults).

    ``proper_nouns=None`` loads the channel dictionary file; ``matrix_status``
    overrides the real mcp-fit row for tests/hermeticity.
    """

    style_profile: SubtitleStyleProfileV1 = DEFAULT_STYLE_PROFILE
    filler_policy: Literal["retain", "remove"] = "retain"
    proper_nouns: ProperNounDictionaryV1 | None = None
    matrix_status: str | None = None


class SubtitleCapabilityPathV1(StrictModel):
    """Ordered capability ladder (PRD §9.3) + the selected rung.

    Selection is a pure function of the mcp-fit matrix status for
    ``subtitle-capability``: accepted -> ``native_text_plus`` first, otherwise
    the external ASS/SRT render path (the existing fixed_presentation
    ``mov_text`` ladder stays as that rung's fallback, never deleted).
    """

    ordered_paths: Annotated[tuple[SubtitlePathKind, ...], BeforeValidator(_to_tuple)] = Field(
        min_length=4
    )
    selected: SubtitlePathKind
    matrix_capability: str
    matrix_status: str
    note: str

    @model_validator(mode="after")
    def require_selected_on_ladder(self) -> SubtitleCapabilityPathV1:
        if self.selected not in self.ordered_paths:
            raise PydanticCustomError("selected_not_ordered", "selected path must be on the ladder")
        return self


class SubtitlePlanV1(ResolveFreeModel):
    """Subtitle plan artifact: cues + style + provenance + explicit failures."""

    schema_version: Literal["subtitle-plan-v1"]
    episode_id: Identifier
    rate: RationalFrameRate
    style_profile: SubtitleStyleProfileV1
    filler_policy: Literal["retain", "remove"]
    cues: Annotated[tuple[SubtitlePlanCueV1, ...], BeforeValidator(_to_tuple)] = ()
    text_provenance: Annotated[tuple[TextProvenanceNoteV1, ...], BeforeValidator(_to_tuple)] = ()
    reconciliation: Annotated[tuple[ReconciliationRecordV1, ...], BeforeValidator(_to_tuple)] = ()
    violations: Annotated[tuple[ReadingSpeedViolationV1, ...], BeforeValidator(_to_tuple)] = ()
    capability_path: SubtitleCapabilityPathV1

    @model_validator(mode="after")
    def require_coherent_cues(self) -> SubtitlePlanV1:
        cue_ids = [cue.cue_id for cue in self.cues]
        if len(set(cue_ids)) != len(cue_ids):
            raise PydanticCustomError("duplicate_cue", "cue ids are unique")
        for cue in self.cues:
            if cue.source_span.rate != self.rate:
                raise PydanticCustomError(
                    "rate_mismatch",
                    "cue source spans carry the plan rate: {cue_id}",
                    {"cue_id": cue.cue_id},
                )
        ordered = sorted(self.cues, key=lambda cue: (cue.record_span.start_frame, cue.cue_id))
        if [cue.cue_id for cue in ordered] != [cue.cue_id for cue in self.cues]:
            raise PydanticCustomError("cue_order", "cues are in record order")
        for earlier, later in pairwise(ordered):
            if later.record_span.start_frame < earlier.record_span.end_frame:
                raise PydanticCustomError("cue_overlap", "subtitle cues overlap on the record")
        return self


__all__ = [
    "PATH_ORDER",
    "AsrSegmentV1",
    "ReadingSpeedViolationV1",
    "ReconciliationRecordV1",
    "SubtitleCapabilityPathV1",
    "SubtitleDraftCueV1",
    "SubtitlePathKind",
    "SubtitlePlanCueV1",
    "SubtitlePlanV1",
    "SubtitleReconciledCueV1",
    "SubtitleReconciliationV1",
    "SubtitleStyleProfileV1",
    "TextProvenanceNoteV1",
]
