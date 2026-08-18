"""Deterministic Edit Plan generation from a solved planner output (Todo 43).

``generate`` is PURE: record spans follow the solution order contiguously
from zero (matching the frozen goldens), every video item gets its linked
audio item (the video item id anchors the link group; declared A/V offsets
are honored through audio bindings), and the FULL decision ledger is
recorded — keeps/adjusts place items while removes (including planner budget
drops) are recorded but place nothing. Every identity is recomputed
deterministically: inputs carrying LLM-authored ``llm_uuid``/``decision_uuid``
fields are rejected outright (PRD 14.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.plan.edit_plan_ids import (
    compute_decision_id,
    compute_edit_item_id,
    offending_uuid_key,
)
from services.plan.edit_plan_models import (
    EDIT_PLAN_SCHEMA_VERSION,
    AudioSpanBinding,
    EditDecisionKind,
    EditItemProvenance,
    EditPlan,
    EditPlanDecision,
    EditPlanItem,
    RecordSpan,
    SelectionPlanRef,
)

if TYPE_CHECKING:
    from services.contracts.primitives import TrackKind
    from services.editorial.candidate_models import (
        Candidate,
        CandidateSpan,
        ProposalProducer,
        SelectionPlanProposal,
    )
    from services.plan.planner_models import PlannerSolution

EditPlanGenerationErrorCode = Literal[
    "episode_mismatch",
    "unknown_candidate",
    "subtitle_intent_unsupported",
    "av_length_mismatch",
    "llm_uuid_field",
]


class EditPlanGenerationError(Exception):
    """Generation was refused before any plan was assembled."""

    def __init__(self, code: EditPlanGenerationErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _decision_id(plan_ref: SelectionPlanRef, candidate: Candidate, kind: str) -> str:
    return compute_decision_id(
        base_episode_id=plan_ref.episode_id,
        base_plan_version=plan_ref.plan_version,
        base_plan_sha256=plan_ref.plan_sha256,
        candidate_id=candidate.candidate_id,
        kind=kind,
        parent_candidate_id=candidate.parent_candidate_id,
    )


def _item(  # noqa: PLR0913 (placement fields are the record)
    candidate: Candidate,
    span: CandidateSpan,
    track_kind: TrackKind,
    *,
    link_group_id: str,
    decision_id: str,
    record: RecordSpan,
    rule: str,
) -> EditPlanItem:
    return EditPlanItem(
        item_id=compute_edit_item_id(
            source_id=candidate.source_ref.source_id,
            edit_source_sha=candidate.source_ref.edit_source_sha,
            span=span,
            track_kind=track_kind,
            decision_id=decision_id,
        ),
        track_kind=track_kind,
        link_group_id=link_group_id,
        source_ref=candidate.source_ref,
        span=span,
        record_span=record,
        decision_id=decision_id,
        provenance=EditItemProvenance(
            candidate_id=candidate.candidate_id,
            analyzer=candidate.analyzer_version,
            rule=rule,
            parent_candidate_id=candidate.parent_candidate_id,
        ),
    )


def _generate_items(
    planner_solution: PlannerSolution,
    plan_ref: SelectionPlanRef,
    by_id: dict[str, Candidate],
    bindings: dict[str, CandidateSpan],
) -> list[EditPlanItem]:
    """Place the linked video/audio item pair for every allocated candidate."""

    items: list[EditPlanItem] = []
    for row in planner_solution.allocated:
        candidate = by_id.get(row.candidate_id)
        if candidate is None:
            raise EditPlanGenerationError(
                "unknown_candidate",
                f"selected candidate {row.candidate_id} is absent from the selection plan",
            )
        if candidate.intent == "subtitle":
            raise EditPlanGenerationError(
                "subtitle_intent_unsupported",
                "subtitle-intent candidates place no A/V items; subtitles compile later",
            )
        decision_id = _decision_id(
            plan_ref, candidate, "adjust" if candidate.intent == "adjust" else "keep"
        )
        video_span = candidate.span
        audio_span = bindings.get(candidate.candidate_id, video_span)
        if audio_span.length != video_span.length:
            raise EditPlanGenerationError(
                "av_length_mismatch",
                f"candidate {candidate.candidate_id} audio binding length "
                f"{audio_span.length} differs from its video span {video_span.length}",
            )
        record = RecordSpan(
            start_frame=row.record_start_frame, end_frame=row.record_end_frame
        )
        video_id = compute_edit_item_id(
            source_id=candidate.source_ref.source_id,
            edit_source_sha=candidate.source_ref.edit_source_sha,
            span=video_span,
            track_kind="video",
            decision_id=decision_id,
        )
        items.append(
            _item(candidate, video_span, "video",
                  link_group_id=video_id, decision_id=decision_id, record=record,
                  rule=planner_solution.ordering_rule_id)
        )
        items.append(
            _item(candidate, audio_span, "audio",
                  link_group_id=video_id, decision_id=decision_id, record=record,
                  rule=planner_solution.ordering_rule_id)
        )
    return items


def _generate_decisions(
    selection_plan: SelectionPlanProposal,
    planner_solution: PlannerSolution,
    plan_ref: SelectionPlanRef,
) -> list[EditPlanDecision]:
    """Record one decision per selection candidate: keeps/adjusts place, removes do not."""

    selected = set(planner_solution.selected_candidate_ids)
    budget_rules = {
        trace.candidate_id: trace.reason_code
        for trace in planner_solution.ordering_trace.budget_dropped
    }
    decisions: list[EditPlanDecision] = []
    for candidate in selection_plan.candidates:
        if candidate.intent == "subtitle":
            raise EditPlanGenerationError(
                "subtitle_intent_unsupported",
                "subtitle-intent candidates place no A/V items; subtitles compile later",
            )
        placed = candidate.candidate_id in selected
        kind: EditDecisionKind
        rule: str
        if placed:
            kind = "adjust" if candidate.intent == "adjust" else "keep"
            rule = planner_solution.ordering_rule_id
        elif candidate.intent == "adjust":
            kind = "adjust"
            rule = candidate.provenance.rule_ids[0]
        else:
            kind = "remove"
            rule = budget_rules.get(candidate.candidate_id, candidate.provenance.rule_ids[0])
        decisions.append(
            EditPlanDecision(
                decision_id=_decision_id(plan_ref, candidate, kind),
                kind=kind,
                candidate_id=candidate.candidate_id,
                parent_candidate_id=candidate.parent_candidate_id,
                rule=rule,
            )
        )
    return decisions


def generate(  # noqa: PLR0913 (frozen generation contract inputs)
    selection_plan: SelectionPlanProposal,
    planner_solution: PlannerSolution,
    *,
    plan_ref: SelectionPlanRef,
    plan_base_version: str,
    producer: ProposalProducer,
    fixture_only: bool,
    audio_bindings: tuple[AudioSpanBinding, ...] = (),
) -> EditPlan:
    """Deterministically assemble the edit plan proposal for a solved plan."""

    for label, document in (
        ("selection_plan", selection_plan.model_dump(mode="json")),
        ("planner_solution", planner_solution.model_dump(mode="json")),
    ):
        offending = offending_uuid_key(document)
        if offending is not None:
            raise EditPlanGenerationError(
                "llm_uuid_field",
                f"{label} carries {offending}; decision ids are recomputed "
                "deterministically and LLM uuid continuity is never trusted",
            )
    if (
        planner_solution.episode_id != selection_plan.episode_id
        or plan_ref.episode_id != selection_plan.episode_id
    ):
        raise EditPlanGenerationError(
            "episode_mismatch",
            "the selection plan, planner solution, and base ref must share one episode",
        )
    by_id = {candidate.candidate_id: candidate for candidate in selection_plan.candidates}
    bindings = {binding.candidate_id: binding.span for binding in audio_bindings}
    items = _generate_items(planner_solution, plan_ref, by_id, bindings)
    decisions = _generate_decisions(selection_plan, planner_solution, plan_ref)
    return EditPlan(
        schema_version=EDIT_PLAN_SCHEMA_VERSION,
        proposal_id=f"epp.{selection_plan.episode_id}.{plan_base_version}",
        episode_id=selection_plan.episode_id,
        plan_base_version=plan_base_version,
        base_selection_plan=plan_ref,
        items=tuple(items),
        decisions=tuple(decisions),
        total_duration_frames=planner_solution.total_duration_frames,
        producer=producer,
        fixture_only=fixture_only,
    )


__all__ = ["EditPlanGenerationError", "EditPlanGenerationErrorCode", "generate"]
