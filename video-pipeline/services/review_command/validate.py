from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.compile.classify import (
    AMBIGUITY_CANDIDATE_THRESHOLD,
    ClassifyError,
    classify_command,
    resolve_candidates,
)
from services.conform.errors import CoordinateOverflowError
from services.conform.guards import guard_int64
from services.contracts.edit_plan_0c import (
    Classification0C,
    ConflictRecord0C,
    EditPlan0C,
    ItemIdSelector0C,
    ReviewCommand0C,
    SourceFrameSpan,
    TargetSelector0C,
)
from services.review_command.models import (
    AdjustSourceSpanProposal0C,
    ApproveEditorialPlanProposal0C,
    ApproveRemainingProposal0C,
    CorrectSubtitleProposal0C,
    EditCommandProposal0C,
    RemoveSegmentProposal0C,
    ReviewCommandProposal0C,
)

type RequiredAction0C = Literal["apply", "defer"]
type ProposalErrorCode0C = Literal[
    "stale_plan_version",
    "unresolved_target",
    "out_of_bounds",
    "model_authored_approval",
]
type EditOperation0C = Literal["remove_segment", "adjust_source_span", "correct_subtitle"]


class ProposalValidationError(Exception):
    def __init__(self, code: ProposalErrorCode0C, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ProposalValidationOutcome:
    proposal_id: str
    classification: Classification0C
    required_action: RequiredAction0C
    needs_human: bool
    candidate_item_ids: tuple[str, ...]
    ambiguity_reasons: tuple[str, ...]
    conflict: ConflictRecord0C | None


def _synthetic_command(
    plan: EditPlan0C,
    proposal: EditCommandProposal0C,
    target: TargetSelector0C,
) -> ReviewCommand0C:
    operation: EditOperation0C
    new_span: SourceFrameSpan | None = None
    new_text: str | None = None
    language: Literal["ja", "en"] = "ja"
    if isinstance(proposal, AdjustSourceSpanProposal0C):
        operation = "adjust_source_span"
        new_span = SourceFrameSpan(
            start_frame=proposal.new_span.start_frame,
            end_frame=proposal.new_span.end_frame,
            rate=plan.frame_rate,
        )
    elif isinstance(proposal, CorrectSubtitleProposal0C):
        operation = "correct_subtitle"
        new_text = proposal.new_text
        language = proposal.language
    else:
        operation = "remove_segment"
    return ReviewCommand0C(
        command_id=proposal.proposal_id,
        language=language,
        instruction=f"proposal:{proposal.proposal_id}",
        operation=operation,
        base_plan_version=proposal.base_plan_version,
        target=target,
        new_span=new_span,
        new_text=new_text,
    )


def _require_current_plan(plan: EditPlan0C, proposal: ReviewCommandProposal0C) -> None:
    if proposal.base_plan_version != plan.plan.plan_version:
        raise ProposalValidationError(
            "stale_plan_version",
            f"proposal {proposal.proposal_id} targets plan {proposal.base_plan_version} "
            f"but the current committed plan is {plan.plan.plan_version}",
        )


def _require_span_in_bounds(plan: EditPlan0C, proposal: AdjustSourceSpanProposal0C) -> None:
    span = proposal.new_span
    try:
        guard_int64(span.start_frame, "proposal span start_frame")
        guard_int64(span.end_frame, "proposal span end_frame")
    except CoordinateOverflowError as error:
        raise ProposalValidationError("out_of_bounds", str(error)) from error
    total_frames = plan.plan.edit_source.total_frames
    if span.end_frame > total_frames:
        raise ProposalValidationError(
            "out_of_bounds",
            f"span end {span.end_frame} exceeds edit source bounds [0, {total_frames})",
        )


def _viable_candidates(plan: EditPlan0C, proposal: EditCommandProposal0C) -> tuple[str, ...]:
    if isinstance(proposal, RemoveSegmentProposal0C):
        viable: list[str] = []
        for candidate in proposal.candidate_targets:
            target = ItemIdSelector0C(kind="item_id", item_id=candidate.item_id)
            if resolve_candidates(plan, _synthetic_command(plan, proposal, target)):
                viable.append(candidate.item_id)
        return tuple(viable)
    return resolve_candidates(plan, _synthetic_command(plan, proposal, proposal.target))


def _approval_outcome(proposal: ReviewCommandProposal0C) -> ProposalValidationOutcome:
    if proposal.actor_intent != "operator":
        raise ProposalValidationError(
            "model_authored_approval",
            f"proposal {proposal.proposal_id} is an approval kind; approvals are human-only",
        )
    return ProposalValidationOutcome(
        proposal_id=proposal.proposal_id,
        classification="clear",
        required_action="apply",
        needs_human=False,
        candidate_item_ids=(),
        ambiguity_reasons=(),
        conflict=None,
    )


def _classified_outcome(
    plan: EditPlan0C,
    proposal: EditCommandProposal0C,
    candidates: tuple[str, ...],
) -> ProposalValidationOutcome:
    if isinstance(proposal, RemoveSegmentProposal0C):
        target = ItemIdSelector0C(kind="item_id", item_id=candidates[0])
    else:
        target = proposal.target
    try:
        result = classify_command(plan, _synthetic_command(plan, proposal, target))
    except ClassifyError as error:
        raise ProposalValidationError("unresolved_target", str(error)) from error
    if result.classification == "conflict":
        return ProposalValidationOutcome(
            proposal_id=proposal.proposal_id,
            classification="conflict",
            required_action="defer",
            needs_human=True,
            candidate_item_ids=candidates,
            ambiguity_reasons=(),
            conflict=result.conflict,
        )
    return ProposalValidationOutcome(
        proposal_id=proposal.proposal_id,
        classification="clear",
        required_action="apply",
        needs_human=False,
        candidate_item_ids=candidates,
        ambiguity_reasons=(),
        conflict=None,
    )


def validate_proposal(
    plan: EditPlan0C,
    proposal: ReviewCommandProposal0C,
) -> ProposalValidationOutcome:
    """Deterministically validate a proposal against the current committed plan.

    Classification is recomputed from the frozen candidate rules and never
    trusted from the proposal-authored ambiguity fields: a proposal claiming
    ``clear`` with two viable targets still classifies as ``ambiguous`` with
    ``required_action=defer`` and ``needs_human=True``.
    """

    _require_current_plan(plan, proposal)
    if isinstance(proposal, AdjustSourceSpanProposal0C):
        _require_span_in_bounds(plan, proposal)
    if isinstance(proposal, ApproveRemainingProposal0C | ApproveEditorialPlanProposal0C):
        return _approval_outcome(proposal)
    candidates = _viable_candidates(plan, proposal)
    if not candidates:
        raise ProposalValidationError(
            "unresolved_target",
            f"proposal {proposal.proposal_id} resolves no item of plan "
            f"{plan.plan.plan_version}",
        )
    if len(candidates) >= AMBIGUITY_CANDIDATE_THRESHOLD:
        return ProposalValidationOutcome(
            proposal_id=proposal.proposal_id,
            classification="ambiguous",
            required_action="defer",
            needs_human=True,
            candidate_item_ids=candidates,
            ambiguity_reasons=(f"multiple viable targets: {', '.join(candidates)}",),
            conflict=None,
        )
    return _classified_outcome(plan, proposal, candidates)
