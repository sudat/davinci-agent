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

from pydantic import BeforeValidator, Field

from services.contracts.primitives import (
    Identifier,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceId,
    SourceRef,
    StrictModel,
)
from services.creative_plan.ir_models_v2 import (  # noqa: TC001 (pydantic field types)
    AudioTrackRoleV2,
    VideoTrackRoleV2,
)
from services.creative_plan.subtitle_models import (  # noqa: TC001 (pydantic field type)
    SubtitlePathKind,
)

_NonEmpty = Annotated[str, Field(min_length=1, strict=True)]


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


StepAction = Literal[
    "prepare_project",
    "import_media",
    "place_clip",
    "place_overlay",
    "place_title",
    "place_audio",
    "apply_subtitles",
    "apply_voice_isolation",
    "apply_audio_op",
    "apply_audio_stage",
    "apply_ducking",
    "apply_color",
    "apply_transform",
    "apply_speed_change",
    "apply_transition",
    "manual_required",
    "apply_kit_recipe",
]


# ------------------------------------------------------- normalized params


class PrepareProjectParams(StrictModel):
    action: Literal["prepare_project"]
    timeline_name: _NonEmpty
    fps_num: int = Field(gt=0, strict=True)
    fps_den: int = Field(gt=0, strict=True)


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
    action: Literal["place_overlay"]
    item_id: Identifier
    source: SourceRef
    record_span: RecordFrameSpan
    track_role: VideoTrackRoleV2


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


class SubtitleParams(StrictModel):
    action: Literal["apply_subtitles"]
    selected_path: SubtitlePathKind
    cue_count: int = Field(ge=0, strict=True)
    style_profile_id: Identifier


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


class DuckingParams(StrictModel):
    action: Literal["apply_ducking"]
    effect_kind: Literal["ducking"]
    stage: str | None = None
    target_item_id: Identifier | None = None


class ColorParams(StrictModel):
    action: Literal["apply_color"]
    section: _NonEmpty
    target_note: _NonEmpty
    look_ref: Identifier | None = None


class TransformParams(StrictModel):
    action: Literal["apply_transform"]
    effect_kind: Literal["punch_in", "crop"]
    target_item_id: Identifier | None = None
    note: str = ""


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


StepParams = Annotated[
    PrepareProjectParams
    | ImportMediaParams
    | PlaceClipParams
    | PlaceOverlayParams
    | PlaceTitleParams
    | PlaceAudioParams
    | SubtitleParams
    | VoiceIsolationParams
    | AudioOpParams
    | AudioStageParams
    | DuckingParams
    | ColorParams
    | TransformParams
    | SpeedChangeParams
    | TransitionParams
    | ManualRequiredParams
    | KitRecipeParams,
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


class TitleReadback(StrictModel):
    kind: Literal["title"]
    item_id: Identifier
    record_span: RecordFrameSpan


class CueCountReadback(StrictModel):
    kind: Literal["cue_count"]
    cue_count: int = Field(ge=0, strict=True)


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
    properties: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]


class ManualNoteReadback(StrictModel):
    kind: Literal["manual_note"]
    expectation: _NonEmpty


class RecipeParamsReadback(StrictModel):
    kind: Literal["recipe_params"]
    recipe_id: _NonEmpty
    param_names: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]


ExpectedReadback = Annotated[
    ProjectReadback
    | ImportReadback
    | PlacementReadback
    | TitleReadback
    | CueCountReadback
    | AudioStateReadback
    | AudioMetricReadback
    | GradeReadback
    | TransformReadback
    | ManualNoteReadback
    | RecipeParamsReadback,
    Field(discriminator="kind"),
]


__all__ = [
    "AudioMetricReadback",
    "AudioOpParams",
    "AudioStageParams",
    "AudioStateReadback",
    "ColorParams",
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
    "SpeedChangeParams",
    "StepAction",
    "StepParams",
    "SubtitleParams",
    "TitleReadback",
    "TransformParams",
    "TransformReadback",
    "TransitionParams",
    "VoiceIsolationParams",
]
