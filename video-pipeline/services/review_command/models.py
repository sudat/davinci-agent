from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.edit_plan_0c import TargetSelector0C  # noqa: TC001 (pydantic runtime)
from services.contracts.primitives import Identifier, ResolveFreeModel

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]
type CommandKind0C = Literal[
    "remove_segment",
    "adjust_source_span",
    "correct_subtitle",
    "approve_remaining",
    "approve_editorial_plan",
]
type ActorIntent0C = Literal["operator", "model"]

DECISION_FIELD_KEYS = frozenset(
    {
        "decision",
        "decision_id",
        "classification",
        "action",
        "resulting_plan_version",
        "conflict",
        "target_candidate_item_ids",
    }
)


def reject_decision_keys(value: object) -> object:
    """Reject operator-decision field names anywhere in an inbound proposal.

    Decisions are a separate contract produced by deterministic validation;
    a proposal payload carrying decision fields is a smuggling attempt and
    fails validation before unknown-key handling.
    """

    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and key in DECISION_FIELD_KEYS:
                    raise PydanticCustomError(
                        "decision_field_forbidden",
                        "Decision fields are forbidden on proposals: {key}",
                        {"key": key},
                    )
                stack.append(child)
        elif isinstance(node, list | tuple):
            stack.extend(node)
    return value


class Confidence0C(ResolveFreeModel):
    num: int = Field(ge=0, strict=True)
    den: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_bounded_ratio(self) -> Confidence0C:
        if self.num > self.den:
            raise PydanticCustomError("confidence_range", "confidence num must not exceed den")
        return self


class ProposalAmbiguity0C(ResolveFreeModel):
    status: Literal["clear", "ambiguous"]
    reasons: Sequence[str] = ()

    @model_validator(mode="after")
    def require_reason_consistency(self) -> ProposalAmbiguity0C:
        if self.status == "ambiguous" and not self.reasons:
            raise PydanticCustomError(
                "ambiguity_reasons",
                "ambiguous proposals must state at least one reason",
            )
        if self.status == "clear" and self.reasons:
            raise PydanticCustomError(
                "ambiguity_reasons",
                "clear proposals must not state ambiguity reasons",
            )
        return self


class ProposalEnvelope0C(ResolveFreeModel):
    proposal_id: Identifier
    base_plan_version: Literal["v1", "v2"]
    actor_intent: ActorIntent0C
    sequence: int = Field(ge=0, strict=True)
    confidence: Confidence0C
    ambiguity: ProposalAmbiguity0C
    evidence: Sequence[str] = ()

    @model_validator(mode="before")
    @classmethod
    def reject_decision_fields(cls, value: object) -> object:
        return reject_decision_keys(value)


class CandidateTarget0C(ResolveFreeModel):
    item_id: Identifier
    evidence: str = Field(min_length=1, strict=True)


class SpanBounds0C(ResolveFreeModel):
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_non_empty_half_open(self) -> SpanBounds0C:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted",
                "end_frame must be greater than start_frame",
            )
        if self.end_frame == self.start_frame:
            raise PydanticCustomError(
                "span_empty",
                "spans are non-empty half-open ranges on the edit-source lattice",
            )
        return self


class RemoveSegmentProposal0C(ProposalEnvelope0C):
    command_kind: Literal["remove_segment"]
    candidate_targets: Sequence[CandidateTarget0C] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_candidates(self) -> RemoveSegmentProposal0C:
        ids = [candidate.item_id for candidate in self.candidate_targets]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_candidate",
                "candidate target ids must be unique",
            )
        return self


class AdjustSourceSpanProposal0C(ProposalEnvelope0C):
    command_kind: Literal["adjust_source_span"]
    target: TargetSelector0C
    new_span: SpanBounds0C


class CorrectSubtitleProposal0C(ProposalEnvelope0C):
    command_kind: Literal["correct_subtitle"]
    target: TargetSelector0C
    new_text: str = Field(min_length=1, strict=True)
    language: Literal["ja", "en"]


class HumanApprovalProposal0C(ProposalEnvelope0C):
    @model_validator(mode="after")
    def require_operator_actor(self) -> HumanApprovalProposal0C:
        if self.actor_intent == "model":
            raise PydanticCustomError(
                "model_authored_approval",
                "approval kinds are human-only; models propose, operators decide",
            )
        return self


class ApproveRemainingProposal0C(HumanApprovalProposal0C):
    command_kind: Literal["approve_remaining"]


class ApproveEditorialPlanProposal0C(HumanApprovalProposal0C):
    command_kind: Literal["approve_editorial_plan"]


type ReviewCommandProposal0C = Annotated[
    RemoveSegmentProposal0C
    | AdjustSourceSpanProposal0C
    | CorrectSubtitleProposal0C
    | ApproveRemainingProposal0C
    | ApproveEditorialPlanProposal0C,
    Field(discriminator="command_kind"),
]

type EditCommandProposal0C = (
    RemoveSegmentProposal0C | AdjustSourceSpanProposal0C | CorrectSubtitleProposal0C
)

_PROPOSAL_ADAPTER: TypeAdapter[ReviewCommandProposal0C] = TypeAdapter(ReviewCommandProposal0C)


def parse_proposal(payload: bytes | str) -> ReviewCommandProposal0C:
    return _PROPOSAL_ADAPTER.validate_json(payload)
