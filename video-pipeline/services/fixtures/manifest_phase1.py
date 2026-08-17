"""Phase-1-technical fixture manifest models (five canonical References).

Every expected value in a manifest is pre-registered by rational derivation
from the DECLARED inputs here (transcript segments with declared analyzer
observations, declared A/V link structure, and the declared editorial rule
spec). Nothing in this manifest family is derived from production code.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, JsonValue, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import canonical_model_bytes

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]
type Frame = Annotated[int, Field(ge=0, strict=True)]

PHASE_1_FIXTURE_IDS: tuple[str, ...] = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)


class FrameSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "spans must be non-empty half-open ranges")
        return self


class EditSourceSpec(StrictModel):
    source_id: Identifier
    frame_rate_num: Literal[30]
    frame_rate_den: Literal[1]
    total_frames: Frame
    audio_sample_rate: Literal[48000]


class VideoRecipe(StrictModel):
    generator: Literal["lavfi"]
    filter: str = Field(min_length=1)
    duration_frames: Frame


class AudioRecipe(StrictModel):
    tts_voice: Literal["Kyoko"]
    synthesis: Literal["say-aiff-per-segment"]
    assembly: Literal["ffmpeg-concat-declared-alignment"]
    asr_preprocess_argv: Sequence[str] = Field(min_length=1)


class MediaRecipe(StrictModel):
    video: VideoRecipe
    audio: AudioRecipe


class EligibilityDeclared(StrictModel):
    language: Literal["ja"]
    principal_video_count: int = Field(ge=0, strict=True)
    audio_present: bool
    vfr: bool
    cfr_normalizable: bool
    total_duration_sec: int = Field(gt=0, strict=True)
    speaker_count: int = Field(ge=1, strict=True)
    privacy_flags: Sequence[str] = ()
    rights_flags: Sequence[str] = ()


class EligibilityExpectation(StrictModel):
    contract: Literal["talking-head-mvp-v1"]
    expected_status: Literal["supported", "assisted", "unsupported"]
    declared: EligibilityDeclared


class ObservedScores(StrictModel):
    content_score: int = Field(ge=0, le=10, strict=True)
    clarity_score: int = Field(ge=0, le=10, strict=True)
    pause_ms: int = Field(ge=0, strict=True)
    retake_group: Identifier | None = None


class TranscriptSegment(StrictModel):
    segment_id: Identifier
    kind: Literal["speech", "pause", "filler", "false_start"]
    text: str
    span: FrameSpan
    observed: ObservedScores


class TranscriptSpec(StrictModel):
    language: Literal["ja"]
    segments: Sequence[TranscriptSegment] = Field(min_length=1)


class AvLinkSpec(StrictModel):
    av_link_id: Identifier
    segment_id: Identifier
    video_span: FrameSpan
    audio_span: FrameSpan

    @model_validator(mode="after")
    def require_equal_length(self) -> AvLinkSpec:
        video_len = self.video_span.end_frame - self.video_span.start_frame
        audio_len = self.audio_span.end_frame - self.audio_span.start_frame
        if video_len != audio_len:
            raise PydanticCustomError("av_link_length", "linked A/V spans must have equal length")
        return self

    def offset_frames(self) -> int:
        return self.audio_span.start_frame - self.video_span.start_frame


class SubtitleSpec(StrictModel):
    subtitle_id: Identifier
    segment_id: Identifier
    text: str = Field(min_length=1)
    span: FrameSpan


class ScoringRules(StrictModel):
    rule_id: Literal["p1-scoring-v1"]
    content_weight: Literal[2]
    clarity_weight: Literal[1]
    min_speech_score: Literal[20]


class PauseRules(StrictModel):
    rule_id: Literal["p1-pauses-v1"]
    delete_threshold_frames: Literal[15]


class RetakeRules(StrictModel):
    rule_id: Literal["p1-retakes-v1"]
    selection: Literal["max-score-per-group"]


class DurationBudgetRules(StrictModel):
    rule_id: Literal["p1-budget-v1"]
    max_output_frames: int = Field(gt=0, strict=True)
    min_output_frames: int = Field(gt=0, strict=True)
    enforcement: Literal["drop-lowest-score-non-must"]


class OrderingRules(StrictModel):
    rule_id: Literal["p1-ordering-v1"]
    rule: Literal["source-order-stable"]


class MustIncludeRules(StrictModel):
    rule_id: Literal["p1-must-include-v1"]
    segment_ids: Sequence[Identifier] = ()


class EditorialRules(StrictModel):
    scoring: ScoringRules
    pauses: PauseRules
    retakes: RetakeRules
    duration_budget: DurationBudgetRules
    ordering: OrderingRules
    must_include: MustIncludeRules


class ReviewTarget(StrictModel):
    kind: Literal["item_id", "subtitle_text_match"]
    item_id: Identifier | None = None
    text: str | None = None

    @model_validator(mode="after")
    def require_consistent_selector(self) -> ReviewTarget:
        if self.kind == "item_id" and self.item_id is None:
            raise PydanticCustomError("selector", "item_id selector requires item_id")
        if self.kind == "subtitle_text_match" and self.text is None:
            raise PydanticCustomError("selector", "text selector requires text")
        return self


class ReviewCommandSpec(StrictModel):
    command_index: int = Field(ge=1, strict=True)
    operation: Literal["remove_segment", "adjust_source_span", "correct_subtitle"]
    target: ReviewTarget
    new_span: FrameSpan | None = None
    new_text: str | None = None


class Phase1TechnicalFixtureManifest(StrictModel):
    schema_version: Literal["phase-1-technical-fixture-manifest-v1"]
    phase: Literal["phase-1-technical"]
    fixture_id: str
    fixture_only: Literal[True]
    expectation_basis: Literal["pre-registered-declared-input-derivation"]
    eligibility: EligibilityExpectation
    edit_source: EditSourceSpec
    media_recipe: MediaRecipe
    transcript: TranscriptSpec
    av_links: Sequence[AvLinkSpec] = Field(min_length=1)
    subtitles: Sequence[SubtitleSpec] = ()
    editorial_rules: EditorialRules
    review_commands: Sequence[ReviewCommandSpec] = ()
    analyzer_expectations: JsonValue
    expected: JsonValue

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)
