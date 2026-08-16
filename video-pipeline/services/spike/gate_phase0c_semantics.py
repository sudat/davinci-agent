"""Decision-semantic checks for one Phase-0C gate case.

Clear-case conversion (exactly one operator apply to v2, golden plan items,
manifest-exact transformation, preview-1 binding and decision-mapped
coverage) and defer-case invariants (no applied events, reasoned defer, plan
bytes identical, preview-1 never re-rendered).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.spike.gate_phase0c_case import base_plan
from services.spike.gate_phase0c_checks import (
    expected_coverage,
    plan_items_view,
    transformation_diff,
)
from services.spike.gate_phase0c_models import (
    CODE_AMBIGUOUS_AUTO_APPLIED,
    CODE_CLEAR_NOT_APPLIED,
    CODE_DEFER_MUTATED_PLAN,
    CODE_GOLDEN_PLAN,
    CODE_NEEDS_HUMAN_MISSING,
    CODE_PREVIEW_MISSING,
    CODE_TRACE_BINDING,
    CRITERION_CLEAR,
)

if TYPE_CHECKING:
    from services.review_command.events import ReviewEvent0C
    from services.spike.gate_phase0c_checks import CheckState0c, GoldenCase0C
    from services.spike.gate_phase0c_evidence import CaseEvidence


def check_decision_semantics(
    bundle: CaseEvidence,
    golden: GoldenCase0C,
    state: CheckState0c,
    deterministic_hash: str,
    class_criterion: str,
) -> tuple[str, str, str]:
    applied = tuple(event for event in bundle.events if event.kind == "decision_applied")
    deferred = tuple(event for event in bundle.events if event.kind == "command_deferred")
    if golden.decision == "apply":
        check_clear_case(bundle, golden, applied, state)
    else:
        check_defer_case(bundle, applied, deferred, state, class_criterion)
    state.note(class_criterion, deterministic_hash)
    return ("", "", deterministic_hash)


def check_clear_case(
    bundle: CaseEvidence,
    golden: GoldenCase0C,
    applied: tuple[ReviewEvent0C, ...],
    state: CheckState0c,
) -> None:
    fid = bundle.fixture_id
    if (
        len(applied) != 1
        or applied[0].actor_intent != "operator"
        or applied[0].result_plan_version != "v2"
    ):
        state.fail(
            CRITERION_CLEAR,
            CODE_CLEAR_NOT_APPLIED,
            f"{fid}: expected exactly one operator apply to v2, got {len(applied)}",
        )
        return
    if plan_items_view(bundle.plan_head) != golden.plan_items:
        state.fail(CRITERION_CLEAR, CODE_GOLDEN_PLAN, f"{fid}: head plan items != golden")
    diff = transformation_diff(bundle.plan_base, bundle.plan_head)
    expected = bundle.manifest.expected
    if (
        diff.removed_item_ids != expected.removed_item_ids
        or diff.span_changes
        != tuple(
            (change.item_id, change.new_span.start_frame, change.new_span.end_frame)
            for change in expected.span_changes
        )
        or diff.text_changes
        != tuple((change.item_id, change.new_text) for change in expected.text_changes)
    ):
        state.fail(
            CRITERION_CLEAR, CODE_GOLDEN_PLAN, f"{fid}: transformation != manifest expectation"
        )
    check_trace1(bundle, state, applied[0].decision_id or "")


def check_defer_case(
    bundle: CaseEvidence,
    applied: tuple[ReviewEvent0C, ...],
    deferred: tuple[ReviewEvent0C, ...],
    state: CheckState0c,
    criterion: str,
) -> None:
    fid = bundle.fixture_id
    if applied:
        state.fail(
            criterion,
            CODE_AMBIGUOUS_AUTO_APPLIED,
            f"{fid}: deferred case carries {len(applied)} applied events",
        )
    if not deferred or deferred[-1].reason is None:
        state.fail(criterion, CODE_NEEDS_HUMAN_MISSING, f"{fid}: no reasoned defer event")
    unchanged = (
        bundle.head_version == 1
        and canonical_model_bytes(bundle.plan_head)
        == canonical_model_bytes(base_plan(bundle.manifest))
    )
    if not unchanged:
        state.fail(criterion, CODE_DEFER_MUTATED_PLAN, f"{fid}: defer changed the plan")
    if bundle.preview1_dir.exists():
        state.fail(criterion, CODE_PREVIEW_MISSING, f"{fid}: defer must not re-render preview-1")


def check_trace1(bundle: CaseEvidence, state: CheckState0c, decision_id: str) -> None:
    fid = bundle.fixture_id
    if bundle.trace1 is None:
        state.fail(CRITERION_CLEAR, CODE_PREVIEW_MISSING, f"{fid}: preview-1 trace missing")
        return
    if (
        bundle.trace1.timeline_binding.plan_version != "v2"
        or bundle.trace1.timeline_binding.ir_sha256 != bundle.ir_head_sha256
    ):
        state.fail(CRITERION_CLEAR, CODE_TRACE_BINDING, f"{fid}: preview-1 binding drift")
    previous = {
        entry.item_id: (entry.record_span.start_frame, entry.record_span.end_frame)
        for entry in bundle.trace0.inputs
        if entry.kind == "video"
    }
    recorded = tuple(
        (entry.span.start_frame, entry.span.end_frame, entry.decision_id)
        for entry in bundle.trace1.record_to_decision
    )
    if recorded != expected_coverage(previous, bundle.ir_head, decision_id):
        state.fail(
            CRITERION_CLEAR, CODE_TRACE_BINDING, f"{fid}: preview-1 coverage mapping drift"
        )


__all__ = [
    "check_clear_case",
    "check_decision_semantics",
    "check_defer_case",
    "check_trace1",
]
