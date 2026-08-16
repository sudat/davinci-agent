from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]
type Frame = Annotated[int, Field(ge=0, strict=True)]
type TrackIndex0C = Annotated[int, Field(gt=0, strict=True)]
type LockedField = Literal["span", "text", "order", "source", "selection"]

PHASE_0C_FIXTURE_IDS: tuple[str, ...] = (
    "p0c-remove-clear",
    "p0c-span-clear",
    "p0c-subtitle-clear",
    "p0c-ambiguous-two-targets",
    "p0c-locked-conflict",
)


class FrameSpan0C(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameSpan0C:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "0C spans must be non-empty half-open ranges")
        return self


class EditSource0C(StrictModel):
    source_id: Identifier
    frame_rate_num: Literal[30]
    frame_rate_den: Literal[1]
    total_frames: Frame


class PlanItem0C(StrictModel):
    item_id: Identifier
    kind: Literal["video", "audio", "subtitle"]
    source_id: Identifier
    span: FrameSpan0C
    track_index: TrackIndex0C
    av_link_id: Identifier | None = None
    subtitle_text: str | None = None
    locked_fields: Sequence[LockedField] = ()

    @model_validator(mode="after")
    def require_kind_consistency(self) -> PlanItem0C:
        if self.kind == "subtitle":
            if not self.subtitle_text:
                raise PydanticCustomError("subtitle_text", "subtitle items must carry text")
        elif self.subtitle_text is not None:
            raise PydanticCustomError("subtitle_text", "only subtitle items carry text")
        if self.kind not in {"video", "audio"} and self.av_link_id is not None:
            raise PydanticCustomError("av_link", "only A/V items may carry a link id")
        return self


class InputEditPlan0C(StrictModel):
    plan_version: Literal["v1"]
    edit_source: EditSource0C
    items: tuple[PlanItem0C, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_ids(self) -> InputEditPlan0C:
        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_item", "plan item ids must be unique")
        return self


class ItemIdSelector(StrictModel):
    kind: Literal["item_id"]
    item_id: Identifier


class SubtitleTextSelector(StrictModel):
    kind: Literal["subtitle_text_match"]
    text: str = Field(min_length=1)


type TargetSelector0C = Annotated[
    ItemIdSelector | SubtitleTextSelector,
    Field(discriminator="kind"),
]


class ReviewCommandSpec0C(StrictModel):
    language: Literal["ja"]
    instruction: str = Field(min_length=1)
    operation: Literal["remove_segment", "adjust_source_span", "correct_subtitle"]
    target: TargetSelector0C
    new_span: FrameSpan0C | None = None
    new_text: str | None = None

    @model_validator(mode="after")
    def require_operation_payload(self) -> ReviewCommandSpec0C:
        if self.operation == "adjust_source_span" and self.new_span is None:
            raise PydanticCustomError("new_span", "adjust_source_span requires new_span")
        if self.operation == "correct_subtitle" and not self.new_text:
            raise PydanticCustomError("new_text", "correct_subtitle requires new_text")
        if self.operation == "remove_segment" and (self.new_span is not None or self.new_text):
            raise PydanticCustomError("remove_payload", "remove_segment carries no payload")
        return self


class SpanChange0C(StrictModel):
    item_id: Identifier
    new_span: FrameSpan0C


class TextChange0C(StrictModel):
    item_id: Identifier
    new_text: str


class ExpectedOutcome0C(StrictModel):
    target_candidate_item_ids: tuple[Identifier, ...] = Field(min_length=1)
    classification: Literal["clear", "ambiguous", "conflict"]
    decision: Literal["apply", "defer"]
    resulting_plan_version: Literal["v1", "v2"]
    removed_item_ids: tuple[Identifier, ...] = ()
    span_changes: tuple[SpanChange0C, ...] = ()
    text_changes: tuple[TextChange0C, ...] = ()

    @model_validator(mode="after")
    def require_decision_consistency(self) -> ExpectedOutcome0C:
        applies = self.decision == "apply"
        if applies != (self.resulting_plan_version == "v2"):
            raise PydanticCustomError("version", "apply bumps to v2; defer keeps v1")
        if not applies and (self.removed_item_ids or self.span_changes or self.text_changes):
            raise PydanticCustomError("defer_mutation", "defer must not pre-register mutations")
        if applies and self.classification != "clear":
            raise PydanticCustomError("auto_apply", "only clear commands may auto-apply")
        return self


class Phase0CFixtureManifest(StrictModel):
    schema_version: Literal["phase-0c-fixture-manifest-v1"]
    fixture_id: Literal[
        "p0c-remove-clear",
        "p0c-span-clear",
        "p0c-subtitle-clear",
        "p0c-ambiguous-two-targets",
        "p0c-locked-conflict",
    ]
    phase: Literal["phase-0c"]
    edit_plan: InputEditPlan0C
    command: ReviewCommandSpec0C
    expected: ExpectedOutcome0C
    expectation_basis: Literal["pre-registered-integer-frame-reasoning"]

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            + b"\n"
        )

    @model_validator(mode="after")
    def reject_observed_result_fields(self) -> Phase0CFixtureManifest:
        stack: list[object] = [self.model_dump(mode="json", exclude_none=True)]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if "observed" in key:
                        raise PydanticCustomError(
                            "observed_result_field",
                            "manifests pre-register deterministic expectations; observed "
                            "outputs are not manifest fields",
                        )
                    stack.append(value)
            elif isinstance(node, list | tuple):
                stack.extend(node)
        return self
