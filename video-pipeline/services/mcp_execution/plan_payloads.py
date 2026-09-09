"""Typed normalized params + expected readbacks per step action (task 38).

Every :class:`~services.mcp_execution.plan_models.McpExecutionStepV1` carries
its ``normalized_params`` from the ``StepParams`` union and its
``expected_readback`` from the ``ExpectedReadback`` union. Both unions are
DISCRIMINATED (``action`` / ``kind``) and closed: a step whose params do not
match its action cannot validate, and no payload accepts free-form fields
(``StrictModel`` forbids extras). There is deliberately no generic
"raw call" payload — the vocabulary is the table below.
"""

# allow: SIZE_OK — pure flat model table (one params/readback class per
# action); splitting would separate the discriminated union members from
# the union they form. ir_models_v2/edit_models_v2 precedent.

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceId,
    SourceRef,
    StrictModel,
    to_tuple,
)
from services.creative_plan.ir_models_v2 import (  # noqa: TC001 (pydantic field types)
    AudioTrackRoleV2,
    VideoTrackRoleV2,
)
from services.creative_plan.subtitle_models import (  # noqa: TC001 (pydantic field type)
    SubtitlePathKind,
)
from services.outputs.geometry import DEFAULT_OUTPUT_ID, OutputId

_NonEmpty = Annotated[str, Field(min_length=1, strict=True)]


StepAction = Literal[
    "prepare_project",
    "import_media",
    "place_clip",
    "place_overlay",
    "place_title",
    "place_audio",
    "apply_subtitles",
    "apply_telop",
    "apply_voice_isolation",
    "apply_audio_op",
    "apply_audio_stage",
    "apply_ducking",
    "apply_color",
    "apply_transform",
    "set_transform",
    "apply_speed_change",
    "apply_transition",
    "manual_required",
    "apply_kit_recipe",
    "render_native",
]


# ------------------------------------------------------- normalized params


class PrepareProjectParams(StrictModel):
    action: Literal["prepare_project"]
    timeline_name: _NonEmpty
    fps_num: int = Field(gt=0, strict=True)
    fps_den: int = Field(gt=0, strict=True)
    #: Which enumerated output canvas the fresh project is bootstrapped
    #: for (default keeps stored pre-output plans parsing byte-identically).
    output_id: OutputId = DEFAULT_OUTPUT_ID


class ImportMediaParams(StrictModel):
    action: Literal["import_media"]
    source_id: SourceId


class PlaceClipParams(StrictModel):
    action: Literal["place_clip"]
    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    track_role: VideoTrackRoleV2
    av_link_id: Identifier | None = None


class PlaceOverlayParams(StrictModel):
    """Overlay placement: external transparent media stacks OVER base clips,
    so it carries the explicit video track index (default 1 keeps stored
    pre-track plans parsing and placing exactly where they did)."""

    action: Literal["place_overlay"]
    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    track_role: VideoTrackRoleV2
    track_index: int = Field(default=1, ge=1, strict=True)


class PlaceTitleParams(StrictModel):
    action: Literal["place_title"]
    item_id: Identifier
    record_span: RecordFrameSpan
    track_role: VideoTrackRoleV2


class PlaceAudioParams(StrictModel):
    action: Literal["place_audio"]
    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    audio_role: AudioTrackRoleV2
    av_link_id: Identifier | None = None


class SubtitleCuePayload(StrictModel):
    """One committed cue: exact display text + half-open record span.

    The text is the plan cue's display lines joined with newlines (each
    rendered line preserved); the record span is the committed half-open
    frame range the native mutation must reproduce exactly
    (probe-evidenced construction: nested-timeline Fusion Title).
    """

    cue_id: Identifier
    text: _NonEmpty
    record_span: RecordFrameSpan


class SubtitleParams(StrictModel):
    """Exact committed cues for the native subtitle mutation (Task 4).

    Replaces the former cue_count-only payload: a count cannot drive an
    exact text/timing mutation, so every cue's text and record span ride
    the typed payload itself. ``style_profile_id`` is the style reference
    (the plan artifact's ``SubtitleStyleProfileV1.profile_id``); the live
    handler resolves it to bound Text+ presentation inputs and refuses
    unknown references typed.
    """

    action: Literal["apply_subtitles"]
    selected_path: SubtitlePathKind
    cues: Annotated[tuple[SubtitleCuePayload, ...], BeforeValidator(to_tuple)] = ()
    style_profile_id: Identifier

    @model_validator(mode="after")
    def require_non_empty_spans(self) -> SubtitleParams:
        for cue in self.cues:
            if cue.record_span.end_frame <= cue.record_span.start_frame:
                raise PydanticCustomError(
                    "span_empty", "cue {cue_id} record span is non-empty", {"cue_id": cue.cue_id}
                )
        return self


#: The committed telop card kinds (DESIGN telop-nested §1.1 + D §5): opening
#: is the large intro card, persistent the top-left always-on card (tiled),
#: chapter the full-black chapter card whose span is also the tile gap,
#: persistent_second the D chapter-name layer on V4 (tiled, anchored below
#: the persistent card's band).
TelopKind = Literal["opening", "persistent", "persistent_second", "chapter"]


class TelopCardPayload(StrictModel):
    """One committed telop card: kind, exact text, half-open record span.

    The persistent kind's record span is the LOGICAL on-screen span; the
    live handler tiles it with the measured insert-title card length,
    skipping chapter-card gaps (DESIGN telop-nested §2.4). Appearance
    never rides the payload — the style reference resolves through the
    channel presentation profile's ``telop_style`` (WBS-0).
    """

    card_id: Identifier
    kind: TelopKind
    text: _NonEmpty
    record_span: RecordFrameSpan


class TelopParams(StrictModel):
    """Exact committed telop cards for the native V3 nested-card mutation
    (DESIGN telop-nested §8 WBS-2 — the ``SubtitleParams`` mirror).

    ``style_profile_id`` is the channel profile's ``telop_style.recipe_id``
    (``telop/default``); the live handler resolves it to the band template's
    published inputs and refuses unknown references typed.
    """

    action: Literal["apply_telop"]
    cards: Annotated[tuple[TelopCardPayload, ...], BeforeValidator(to_tuple)] = ()
    #: The profile's ``telop_style.recipe_id`` (RecipeRef vocabulary —
    #: slash-bearing, unlike an Identifier: ``telop/default``).
    style_profile_id: _NonEmpty

    @model_validator(mode="after")
    def require_non_empty_spans(self) -> TelopParams:
        for card in self.cards:
            if card.record_span.end_frame <= card.record_span.start_frame:
                raise PydanticCustomError(
                    "span_empty",
                    "card {card_id} record span is non-empty",
                    {"card_id": card.card_id},
                )
        return self


class VoiceIsolationParams(StrictModel):
    action: Literal["apply_voice_isolation"]
    effect_kind: Literal["voice_isolation"]
    stage: str | None = None
    target_item_id: Identifier | None = None
    note: str = ""


class AudioOpParams(StrictModel):
    action: Literal["apply_audio_op"]
    effect_kind: Literal["eq", "compression", "noise_cleanup"]
    stage: str | None = None
    capability: _NonEmpty
    justification: str | None = None


class AudioStageParams(StrictModel):
    action: Literal["apply_audio_stage"]
    stage: _NonEmpty
    goal: _NonEmpty
    metric: _NonEmpty
    minimum: float = Field(strict=True)
    maximum: float = Field(strict=True)
    unit: _NonEmpty
    #: The dialogue-chain Fairlight binding this stage resolves through
    #: (mcp-complete-parity Task 5); None keeps legacy executor payloads
    #: valid while the stage rides a non-MCP rung.
    preset_ref: _NonEmpty | None = None


class DuckingParams(StrictModel):
    action: Literal["apply_ducking"]
    effect_kind: Literal["ducking"]
    stage: str | None = None
    target_item_id: Identifier | None = None


class ColorTargetPayload(StrictModel):
    """One explicit DRX target: the product item id plus its committed
    record span (the join key against the live timeline-structure
    readback — never a selection or an implicit current clip).

    ``source_span`` is the committed SOURCE identity of the same item (its
    cut of the edit mezzanine): when several placed video items share one
    record span — measured on v44-real-01, every subtitle cue card sits on
    the overlay track at exactly the span of the clip beneath it — the
    source frames are the deterministic independent join that selects the
    placed clip and never a positional first hit. None keeps legacy
    executor payloads valid on non-MCP rungs (span-only resolution).
    """

    item_id: Identifier
    record_span: RecordFrameSpan
    source_span: SourceFrameSpan | None = None


class ColorParams(StrictModel):
    action: Literal["apply_color"]
    section: _NonEmpty
    target_note: _NonEmpty
    look_ref: Identifier | None = None
    #: Task 6 binding id resolving the kit selection to ONE hash-pinned
    #: DRX (handler-side table). None keeps legacy executor payloads valid
    #: on non-MCP rungs; the live handler refuses a missing ref typed.
    drx_ref: Identifier | None = None
    #: Explicit target items (product item ids + committed record spans).
    #: The live handler refuses an empty target list typed.
    targets: Annotated[tuple[ColorTargetPayload, ...], BeforeValidator(to_tuple)] = ()

    @model_validator(mode="after")
    def require_non_empty_target_spans(self) -> ColorParams:
        for target in self.targets:
            if target.record_span.end_frame <= target.record_span.start_frame:
                raise PydanticCustomError(
                    "span_empty",
                    "target {item_id} record span is non-empty",
                    {"item_id": target.item_id},
                )
        return self


class TransformParams(StrictModel):
    action: Literal["apply_transform"]
    effect_kind: Literal["punch_in", "crop"]
    target_item_id: Identifier | None = None
    note: str = ""


class SetTransformParams(StrictModel):
    action: Literal["set_transform"]
    track_index: int = Field(ge=1, strict=True)
    item_index: int = Field(ge=0, strict=True)
    rotation_angle: float = Field(strict=True)
    target_item_id: Identifier | None = None


class SpeedChangeParams(StrictModel):
    action: Literal["apply_speed_change"]
    effect_kind: Literal["speed_change"]
    target_item_id: Identifier | None = None
    speed_factor_pct: int = Field(gt=0, strict=True)


class TransitionParams(StrictModel):
    action: Literal["apply_transition"]
    effect_kind: Literal["transition"]
    transition_id: Identifier
    style: Literal["dissolve", "motion", "other"]
    at_record_frame: int = Field(ge=0, strict=True)
    duration_frames: int = Field(gt=0, strict=True)
    a_item_ref: Identifier
    b_item_ref: Identifier


class ManualRequiredParams(StrictModel):
    action: Literal["manual_required"]
    effect_kind: Literal["manual_required", "color_look"]
    effect_id: Identifier | None = None
    note: _NonEmpty


class KitRecipeParams(StrictModel):
    action: Literal["apply_kit_recipe"]
    intent_id: Identifier
    kind: _NonEmpty
    recipe_id: _NonEmpty
    resolved_params: dict[str, float]
    rationale: _NonEmpty
    target_span: RecordFrameSpan
    note: str = ""


class RenderNativeParams(StrictModel):
    """Typed native render lifecycle params (Task 7).

    Every setting is an explicit field so the handler can list, validate,
    apply, and read back each one independently. The custom name is
    deterministic per episode so rerun/resume can reconcile the same output.
    """

    action: Literal["render_native"]
    custom_name: _NonEmpty
    format_id: _NonEmpty
    codec_id: _NonEmpty
    width: int = Field(ge=1, strict=True)
    height: int = Field(ge=1, strict=True)
    frame_rate: float = Field(gt=0, strict=True)
    select_all_frames: bool = Field(strict=True)
    export_video: bool = Field(strict=True)
    export_audio: bool = Field(strict=True)
    data_burn_in: _NonEmpty


StepParams = Annotated[
    PrepareProjectParams
    | ImportMediaParams
    | PlaceClipParams
    | PlaceOverlayParams
    | PlaceTitleParams
    | PlaceAudioParams
    | SubtitleParams
    | TelopParams
    | VoiceIsolationParams
    | AudioOpParams
    | AudioStageParams
    | DuckingParams
    | ColorParams
    | TransformParams
    | SetTransformParams
    | SpeedChangeParams
    | TransitionParams
    | ManualRequiredParams
    | KitRecipeParams
    | RenderNativeParams,
    Field(discriminator="action"),
]


# ------------------------------------------------------ expected readbacks


class ProjectReadback(StrictModel):
    kind: Literal["project"]
    project_name: _NonEmpty
    timeline_frame_rate: _NonEmpty


class ImportReadback(StrictModel):
    kind: Literal["import"]
    source_id: SourceId


class PlacementReadback(StrictModel):
    kind: Literal["placement"]
    item_id: Identifier
    source_span: SourceFrameSpan
    record_span: RecordFrameSpan
    track_index: int = Field(default=1, ge=1, strict=True)


class TitleReadback(StrictModel):
    kind: Literal["title"]
    item_id: Identifier
    record_span: RecordFrameSpan


class CueCountReadback(StrictModel):
    kind: Literal["cue_count"]
    cue_count: int = Field(ge=0, strict=True)


class SubtitleCuesReadback(StrictModel):
    """Expected readback for the native subtitle mutation (Task 8).

    The live handler returns exact per-cue evidence (id, text, record
    span, readback-sourced style); the runner gate verifies the COMPLETE
    committed cue set — count plus every cue's identity, text, and span.
    A count-only gate could not consume that evidence (measured on
    v44-real-01: every attempt failed ``cue_count: missing`` while the
    cues existed natively). ``CueCountReadback`` stays in the union so
    previously committed plans keep parsing.
    """

    kind: Literal["subtitle_cues"]
    cues: Annotated[tuple[SubtitleCuePayload, ...], BeforeValidator(to_tuple)] = ()


class TelopCardsReadback(StrictModel):
    """Expected readback for the native telop mutation (DESIGN WBS-3).

    The live handler returns exact per-card evidence (id, kind, text,
    logical record span, tile spans, readback-sourced style); the runner
    gate verifies the COMPLETE committed card set — identity, kind, text,
    and record span per card (the subtitle Task-8 precedent: the gate must
    consume the evidence the handler actually returns). Tile spans and
    style are handler-sourced and verified inside the mutation itself.
    """

    kind: Literal["telop_cards"]
    cards: Annotated[tuple[TelopCardPayload, ...], BeforeValidator(to_tuple)] = ()


class AudioStateReadback(StrictModel):
    kind: Literal["audio_state"]
    item_ref: _NonEmpty
    state_property: _NonEmpty


class AudioMetricReadback(StrictModel):
    kind: Literal["audio_metric"]
    stage: _NonEmpty
    metric: _NonEmpty
    minimum: float = Field(strict=True)
    maximum: float = Field(strict=True)
    unit: _NonEmpty


class GradeReadback(StrictModel):
    kind: Literal["grade"]
    target: _NonEmpty
    preset_ref: _NonEmpty


class TransformReadback(StrictModel):
    kind: Literal["transform"]
    item_id: _NonEmpty
    properties: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]


class SetTransformReadback(StrictModel):
    kind: Literal["set_transform"]
    track_index: int = Field(ge=1, strict=True)
    item_index: int = Field(ge=0, strict=True)
    rotation_angle: float = Field(strict=True)


class ManualNoteReadback(StrictModel):
    kind: Literal["manual_note"]
    expectation: _NonEmpty


class RecipeParamsReadback(StrictModel):
    kind: Literal["recipe_params"]
    recipe_id: _NonEmpty
    param_names: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]


class RenderNativeReadback(StrictModel):
    """Expected readback for the native render step.

    The custom name is deterministic per episode so the runner can
    verify the handler used the same name it was given.
    """

    kind: Literal["render_native"]
    custom_name: _NonEmpty


ExpectedReadback = Annotated[
    ProjectReadback
    | ImportReadback
    | PlacementReadback
    | TitleReadback
    | CueCountReadback
    | SubtitleCuesReadback
    | TelopCardsReadback
    | AudioStateReadback
    | AudioMetricReadback
    | GradeReadback
    | TransformReadback
    | SetTransformReadback
    | ManualNoteReadback
    | RecipeParamsReadback
    | RenderNativeReadback,
    Field(discriminator="kind"),
]


__all__ = [
    "AudioMetricReadback",
    "AudioOpParams",
    "AudioStageParams",
    "AudioStateReadback",
    "ColorParams",
    "ColorTargetPayload",
    "CueCountReadback",
    "DuckingParams",
    "ExpectedReadback",
    "GradeReadback",
    "ImportMediaParams",
    "ImportReadback",
    "KitRecipeParams",
    "ManualNoteReadback",
    "ManualRequiredParams",
    "PlaceAudioParams",
    "PlaceClipParams",
    "PlaceOverlayParams",
    "PlaceTitleParams",
    "PlacementReadback",
    "PrepareProjectParams",
    "ProjectReadback",
    "RecipeParamsReadback",
    "RenderNativeParams",
    "RenderNativeReadback",
    "SpeedChangeParams",
    "StepAction",
    "StepParams",
    "SubtitleCuePayload",
    "SubtitleCuesReadback",
    "SubtitleParams",
    "TelopCardPayload",
    "TelopCardsReadback",
    "TelopKind",
    "TelopParams",
    "TitleReadback",
    "TransformParams",
    "TransformReadback",
    "TransitionParams",
    "VoiceIsolationParams",
]
