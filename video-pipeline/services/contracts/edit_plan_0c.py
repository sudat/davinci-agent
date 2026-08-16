from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    ResolveFreeEnvelope,
    ResolveFreeModel,
    SourceFrameSpan,
)

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]
type ItemKind0C = Literal["video", "audio", "subtitle"]
type LockedField0C = Literal["span", "text", "order", "source", "selection"]
type Classification0C = Literal["clear", "ambiguous", "conflict"]


class EditPlanItem0C(ResolveFreeModel):
    item_id: Identifier
    kind: ItemKind0C
    source_id: Identifier
    span: SourceFrameSpan
    track_index: int = Field(gt=0, strict=True)
    av_link_id: Identifier | None = None
    subtitle_text: str | None = None
    locked_fields: Sequence[LockedField0C] = ()

    @model_validator(mode="after")
    def require_kind_consistency(self) -> EditPlanItem0C:
        if self.kind == "subtitle":
            if self.subtitle_text is None or not self.subtitle_text:
                raise PydanticCustomError("subtitle_text", "subtitle items must carry text")
        elif self.subtitle_text is not None:
            raise PydanticCustomError("subtitle_text", "only subtitle items carry text")
        if self.kind not in ("video", "audio") and self.av_link_id is not None:
            raise PydanticCustomError("av_link", "only A/V items may carry a link id")
        return self


class EditSourceRef0C(ResolveFreeModel):
    source_id: Identifier
    total_frames: int = Field(ge=1, strict=True)


class EditPlanBody0C(ResolveFreeModel):
    plan_version: Literal["v1", "v2"]
    edit_source: EditSourceRef0C
    items: tuple[EditPlanItem0C, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_ids(self) -> EditPlanBody0C:
        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_item", "plan item ids must be unique")
        return self


class EditPlan0C(ResolveFreeEnvelope[Literal["edit_plan_0c"]]):
    frame_rate: RationalFrameRate
    plan: EditPlanBody0C

    @model_validator(mode="after")
    def require_source_binding(self) -> EditPlan0C:
        if any(item.source_id != self.plan.edit_source.source_id for item in self.plan.items):
            raise PydanticCustomError("source_binding", "every item must reference the edit source")
        return self


class ItemIdSelector0C(ResolveFreeModel):
    kind: Literal["item_id"]
    item_id: Identifier


class SubtitleTextSelector0C(ResolveFreeModel):
    kind: Literal["subtitle_text_match"]
    text: str = Field(min_length=1)


type TargetSelector0C = Annotated[
    ItemIdSelector0C | SubtitleTextSelector0C,
    Field(discriminator="kind"),
]


class ReviewCommand0C(ResolveFreeModel):
    command_id: Identifier
    language: Literal["ja", "en"]
    instruction: str = Field(min_length=1)
    operation: Literal["remove_segment", "adjust_source_span", "correct_subtitle"]
    base_plan_version: Literal["v1", "v2"]
    target: TargetSelector0C
    new_span: SourceFrameSpan | None = None
    new_text: str | None = None

    @model_validator(mode="after")
    def require_operation_payload(self) -> ReviewCommand0C:
        if self.operation == "adjust_source_span" and self.new_span is None:
            raise PydanticCustomError("new_span", "adjust_source_span requires new_span")
        if self.operation == "correct_subtitle" and not self.new_text:
            raise PydanticCustomError("new_text", "correct_subtitle requires new_text")
        if self.operation == "remove_segment" and (self.new_span is not None or self.new_text):
            raise PydanticCustomError("remove_payload", "remove_segment carries no payload")
        return self


class ConflictRecord0C(ResolveFreeModel):
    command_id: Identifier
    target_item_id: Identifier
    locked_field: LockedField0C


class Decision0C(ResolveFreeModel):
    command_id: Identifier
    classification: Classification0C
    action: Literal["apply", "defer"]
    base_plan_version: Literal["v1", "v2"]
    resulting_plan_version: Literal["v1", "v2"]
    conflict: ConflictRecord0C | None = None
    target_candidate_item_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_action_consistency(self) -> Decision0C:
        if self.action == "apply":
            if self.classification != "clear":
                raise PydanticCustomError("auto_apply", "only clear commands may auto-apply")
            if self.resulting_plan_version == self.base_plan_version:
                raise PydanticCustomError("version", "apply must bump the plan version")
            if self.conflict is not None:
                raise PydanticCustomError("conflict_record", "apply carries no conflict record")
        else:
            if self.resulting_plan_version != self.base_plan_version:
                raise PydanticCustomError("version", "defer must keep the plan version")
            if self.classification == "conflict" and self.conflict is None:
                raise PydanticCustomError(
                    "conflict_record",
                    "deferred conflicts must record the colliding lock",
                )
            if self.classification != "conflict" and self.conflict is not None:
                raise PydanticCustomError("conflict_record", "only conflicts record a lock")
        return self
