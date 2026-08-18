"""Edit Plan reconciliation against its base Selection Plan (Todo 43).

``reconcile`` recomputes EVERYTHING the plan claims: item and decision
identities, candidate bindings and spans, A/V link integrity in both
directions, record-span contiguity and totals, and the decision ledger —
every planner-selected candidate appears exactly once (video) with its audio
link, removes are recorded but place nothing, and adjusts chain to their
parents. A tampered plan (constructed via frozen copies that bypass the
model validators) fails here with a typed error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.plan.edit_plan_ids import compute_decision_id, compute_edit_item_id

if TYPE_CHECKING:
    from services.editorial.candidate_models import Candidate, SelectionPlanProposal
    from services.plan.edit_plan_models import EditPlan, EditPlanItem
    from services.plan.planner_models import PlannerSolution

ReconcileErrorCode = Literal[
    "episode_mismatch",
    "item_identity_mismatch",
    "decision_identity_mismatch",
    "unknown_item_candidate",
    "item_span_mismatch",
    "broken_link",
    "record_not_contiguous",
    "total_mismatch",
    "selected_missing",
    "selected_duplicate",
    "unrecorded_candidate",
    "remove_has_items",
    "decision_count_mismatch",
    "adjust_parent_unresolved",
]


class EditPlanReconcileError(Exception):
    """The edit plan does not reconcile against its base selection plan."""

    def __init__(self, code: ReconcileErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _check_identities(plan: EditPlan) -> None:
    for item in plan.items:
        expected = compute_edit_item_id(
            source_id=item.source_ref.source_id,
            edit_source_sha=item.source_ref.edit_source_sha,
            span=item.span,
            track_kind=item.track_kind,
            decision_id=item.decision_id,
        )
        if item.item_id != expected:
            raise EditPlanReconcileError(
                "item_identity_mismatch",
                f"item {item.item_id} does not equal its recomputed identity",
            )
    ref = plan.base_selection_plan
    for row in plan.decisions:
        expected = compute_decision_id(
            base_episode_id=ref.episode_id,
            base_plan_version=ref.plan_version,
            base_plan_sha256=ref.plan_sha256,
            candidate_id=row.candidate_id,
            kind=row.kind,
            parent_candidate_id=row.parent_candidate_id,
        )
        if row.decision_id != expected:
            raise EditPlanReconcileError(
                "decision_identity_mismatch",
                f"decision {row.decision_id} does not equal its recomputed identity",
            )


def _check_candidate_bindings(
    plan: EditPlan, by_id: dict[str, Candidate]
) -> dict[str, EditPlanItem]:
    videos: dict[str, EditPlanItem] = {}
    for item in plan.items:
        candidate = by_id.get(item.provenance.candidate_id)
        if candidate is None:
            raise EditPlanReconcileError(
                "unknown_item_candidate",
                f"item {item.item_id} references candidate "
                f"{item.provenance.candidate_id} absent from the selection plan",
            )
        if item.source_ref != candidate.source_ref:
            raise EditPlanReconcileError(
                "item_span_mismatch",
                f"item {item.item_id} binds a different source than its candidate",
            )
        if item.track_kind == "video":
            if item.span != candidate.span:
                raise EditPlanReconcileError(
                    "item_span_mismatch",
                    f"video item {item.item_id} span differs from its candidate span",
                )
            if item.provenance.candidate_id in videos:
                raise EditPlanReconcileError(
                    "selected_duplicate",
                    f"candidate {item.provenance.candidate_id} places more than one item",
                )
            videos[item.provenance.candidate_id] = item
    return videos


def _check_links(plan: EditPlan) -> None:
    videos = {item.item_id: item for item in plan.items if item.track_kind == "video"}
    audios = [item for item in plan.items if item.track_kind == "audio"]
    anchors = [audio.link_group_id for audio in audios]
    if len(set(anchors)) != len(anchors) or set(anchors) != set(videos):
        raise EditPlanReconcileError(
            "broken_link", "every video item has exactly one audio counterpart"
        )
    for audio in audios:
        anchor = videos[audio.link_group_id]
        if (
            anchor.decision_id != audio.decision_id
            or anchor.record_span != audio.record_span
            or anchor.span.length != audio.span.length
        ):
            raise EditPlanReconcileError(
                "broken_link",
                f"audio item {audio.item_id} does not resolve to its video counterpart",
            )


def _check_record_layout(plan: EditPlan) -> None:
    cursor = 0
    for video in (item for item in plan.items if item.track_kind == "video"):
        if (
            video.record_span.start_frame != cursor
            or video.record_span.length != video.span.length
        ):
            raise EditPlanReconcileError(
                "record_not_contiguous",
                f"video item {video.item_id} record span is not contiguous from zero "
                "or does not mirror its source length",
            )
        cursor = video.record_span.end_frame
    if cursor != plan.total_duration_frames:
        raise EditPlanReconcileError(
            "total_mismatch",
            f"total_duration_frames {plan.total_duration_frames} differs from the "
            f"record sum {cursor}",
        )


def _check_ledger(
    plan: EditPlan,
    by_id: dict[str, Candidate],
    videos: dict[str, EditPlanItem],
) -> None:
    recorded = {row.candidate_id: row for row in plan.decisions}
    if set(recorded) != set(by_id):
        raise EditPlanReconcileError(
            "decision_count_mismatch",
            "the decision ledger covers exactly the selection plan candidates",
        )
    for row in plan.decisions:
        if row.kind == "remove" and row.candidate_id in videos:
            raise EditPlanReconcileError(
                "remove_has_items", f"remove decision {row.decision_id} places items"
            )
        if row.kind == "adjust":
            parent = row.parent_candidate_id
            if parent is None or parent not in by_id:
                raise EditPlanReconcileError(
                    "adjust_parent_unresolved",
                    f"adjust decision {row.decision_id} does not chain to a parent",
                )
    for candidate_id, item in videos.items():
        row = recorded[candidate_id]
        if row.kind == "remove" or row.decision_id != item.decision_id:
            raise EditPlanReconcileError(
                "unrecorded_candidate",
                f"placed candidate {candidate_id} has no matching keep/adjust decision",
            )


def _check_solution(plan: EditPlan, solution: PlannerSolution) -> None:
    placed = [
        item.provenance.candidate_id for item in plan.items if item.track_kind == "video"
    ]
    if placed != list(solution.selected_candidate_ids):
        raise EditPlanReconcileError(
            "selected_missing",
            "the placed video items equal the planner selection exactly once, in order",
        )


def reconcile(
    plan: EditPlan,
    selection_plan: SelectionPlanProposal,
    planner_solution: PlannerSolution | None = None,
) -> None:
    """Recompute the plan against its base; any mismatch is a typed error."""

    episodes = {
        plan.episode_id,
        selection_plan.episode_id,
        plan.base_selection_plan.episode_id,
    }
    if len(episodes) != 1:
        raise EditPlanReconcileError(
            "episode_mismatch", "the plan, base ref, and selection share one episode"
        )
    if planner_solution is not None and planner_solution.episode_id != plan.episode_id:
        raise EditPlanReconcileError(
            "episode_mismatch", "the planner solution episode differs from the plan"
        )
    by_id = {candidate.candidate_id: candidate for candidate in selection_plan.candidates}
    _check_identities(plan)
    videos = _check_candidate_bindings(plan, by_id)
    _check_links(plan)
    _check_record_layout(plan)
    _check_ledger(plan, by_id, videos)
    if planner_solution is not None:
        _check_solution(plan, planner_solution)


__all__ = ["EditPlanReconcileError", "ReconcileErrorCode", "reconcile"]
