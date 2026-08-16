"""Offline fake evidence trees for the Phase-0C gate (no ffmpeg, no Resolve).

``synthesize0c`` drives the REAL pure pipeline pieces (store, translator
replay, reducer/commit) for every fixture case and substitutes synthetic
preview traces for the rendered previews, producing a complete evidence tree
the real evaluator accepts. Fault knobs inject exactly one canonical defect
each: a wrong operator-decision payload, an auto-applied ambiguous input, an
unrelated item mutated outside the decision, and a stale-base event.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from services.contracts.edit_plan_0c import EditPlan0C, EditPlanBody0C
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.models import AppliedDecision
from services.review_command.commit import commit_command
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    build_event,
)
from services.review_command.reducer import version_ir
from services.review_command.store import (
    INDEX_NAME,
    OperatorDecision0C,
    PlanVersionsIndex,
    ReviewCommitError,
    VersionEntry,
    append_events,
    initialize_store,
    load_events,
    load_index,
    load_version_plan,
    sha256_bytes,
)
from services.spike.gate_phase0c_bindings import load_ir_file, preview_ir
from services.spike.gate_phase0c_case import (
    base_plan,
    load_case,
    replay_proposal,
    translator_request,
)
from services.spike.gate_phase0c_driver import translate_case
from services.spike.gate_phase0c_fake_traces import synthetic_trace
from services.spike.gate_phase0c_models import (
    EVENTS_LOG_NAME,
    PHASE_0C_CASES,
    PREVIEW0_DIR,
    PREVIEW1_DIR,
    run_dir,
    store_dir,
    translator_record_path,
)

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C
    from services.review_command.models import ReviewCommandProposal0C

class FakeSynthesisError(Exception):
    """The fake evidence tree could not be assembled."""


class FaultKnobs0c:
    def __init__(
        self,
        *,
        wrong_decision: bool = False,
        auto_applied_ambiguous: bool = False,
        changed_unrelated_item: bool = False,
        stale_event: bool = False,
    ) -> None:
        self.wrong_decision = wrong_decision
        self.auto_applied_ambiguous = auto_applied_ambiguous
        self.changed_unrelated_item = changed_unrelated_item
        self.stale_event = stale_event


def _decision(fixture_id: str) -> OperatorDecision0C:
    return OperatorDecision0C(
        decision_id=f"decision-{fixture_id}",
        actor_intent="operator",
        note="fake-tree fixture-marked operator-role record",
    )


def _synthetic_ir(store: Path, version: int) -> TimelineIr0C:
    return load_ir_file(store / f"ir-v{version}.json")


def _plan_from_store(store: Path, version: int) -> EditPlan0C:
    return load_version_plan(store, load_index(store), version)


def _rewrite_index_entry(
    store: Path, plan: EditPlan0C, version: int, event_id: str | None
) -> None:
    base = _plan_from_store(store, 1)
    ir = version_ir(base, plan, version)
    plan_bytes = canonical_model_bytes(plan)
    ir_bytes = canonical_model_bytes(ir)
    atomic_write(store / f"plan-v{version}.json", plan_bytes)
    atomic_write(store / f"ir-v{version}.json", ir_bytes)
    index = load_index(store)
    entries = dict(index.versions)
    entries[str(version)] = VersionEntry(
        plan_sha256=sha256_bytes(plan_bytes),
        ir_sha256=sha256_bytes(ir_bytes),
        event_id=event_id,
        parent_version=version - 1,
    )
    atomic_write(
        store / INDEX_NAME,
        canonical_model_bytes(
            PlanVersionsIndex(
                schema_version="plan-versions-v1",
                base_artifact_id=index.base_artifact_id,
                versions=entries,
            )
        ),
    )


def _drop_item(
    plan: EditPlan0C, drop_item_id: str, version: Literal["v1", "v2"]
) -> EditPlan0C:
    items = tuple(item for item in plan.plan.items if item.item_id != drop_item_id)
    body = EditPlanBody0C(
        plan_version=version, edit_source=plan.plan.edit_source, items=items
    )
    return plan.model_copy(update={"plan": body})


def _truncate_span(plan: EditPlan0C, item_ids: tuple[str, ...], end_frame: int) -> EditPlan0C:
    """Mutate items unrelated to the decision while keeping the layout linked."""

    items = tuple(
        item.model_copy(update={"span": item.span.model_copy(update={"end_frame": end_frame})})
        if item.item_id in item_ids
        else item
        for item in plan.plan.items
    )
    return plan.model_copy(update={"plan": plan.plan.model_copy(update={"items": items})})


def _forge_ambiguous_apply(
    store: Path,
    log: Path,
    proposal: ReviewCommandProposal0C,
    decision: OperatorDecision0C,
) -> None:
    """Hand-forge an applied event for the ambiguous proposal (the fault)."""

    events = load_events(log)
    anchor_sequence = events[-1].sequence if events else 0
    anchor_hash = events[-1].event_id if events else GENESIS_EVENT_HASH
    recorded = build_event(
        sequence=anchor_sequence + 1,
        kind="proposal_recorded",
        proposal=proposal,
        base_plan_version="v1",
        previous_event_hash=anchor_hash,
        actor_intent="model",
    )
    applied_event = build_event(
        sequence=anchor_sequence + 2,
        kind="decision_applied",
        proposal=proposal,
        base_plan_version="v1",
        result_plan_version="v2",
        applied=True,
        actor_intent="operator",
        decision_id=decision.decision_id,
        previous_event_hash=recorded.event_id,
    )
    append_events(log, (recorded, applied_event))
    mutated = _drop_item(_plan_from_store(store, 1), "s2", "v2")
    _rewrite_index_entry(store, mutated, 2, applied_event.event_id)


def _append_stale_event(log: Path, proposal: ReviewCommandProposal0C) -> None:
    events = load_events(log)
    stale = build_event(
        sequence=events[-1].sequence + 1,
        kind="proposal_recorded",
        proposal=proposal,
        base_plan_version="v1",
        previous_event_hash=events[-1].event_id,
        actor_intent="model",
    )
    append_events(log, (stale,))


def synthesize_case(
    evidence: Path, fixture_id: str, knobs: FaultKnobs0c
) -> None:
    manifest = load_case(fixture_id)
    plan = base_plan(manifest)
    run = run_dir(evidence, fixture_id)
    store = store_dir(evidence, fixture_id)
    log = store / EVENTS_LOG_NAME
    initialize_store(plan, log, store)
    request = translator_request(manifest, plan)
    canonical = replay_proposal(manifest)
    translate_case(request, canonical, translator_record_path(evidence, fixture_id))

    wrong = knobs.wrong_decision and fixture_id == "p0c-span-clear"
    proposal = replay_proposal(manifest, span_override=(150, 230) if wrong else None)
    decision = _decision(fixture_id)
    if knobs.auto_applied_ambiguous and fixture_id == "p0c-ambiguous-two-targets":
        _forge_ambiguous_apply(store, log, canonical, decision)
    else:
        try:
            commit_command(proposal, decision, log, store)
        except ReviewCommitError as error:
            raise FakeSynthesisError(f"{fixture_id}: {error}") from error
    if knobs.changed_unrelated_item and fixture_id == "p0c-remove-clear":
        mutated = _truncate_span(_plan_from_store(store, 2), ("v1", "a1"), 120)
        _rewrite_index_entry(store, mutated, 2, load_index(store).versions["2"].event_id)

    trace0 = synthetic_trace(
        run / PREVIEW0_DIR, preview_ir(_synthetic_ir(store, 1)), "v1", None
    )
    if len(load_index(store).versions) > 1:
        binding = AppliedDecision(
            decision_id=decision.decision_id,
            case_id=fixture_id,
            classification="clear",
            plan_version_after="v2",
            previous_trace=trace0,
        )
        synthetic_trace(run / PREVIEW1_DIR, preview_ir(_synthetic_ir(store, 2)), "v2", binding)
    if knobs.stale_event and fixture_id == "p0c-span-clear":
        _append_stale_event(log, canonical)


def synthesize0c(evidence: Path, knobs: FaultKnobs0c) -> None:
    for fixture_id in PHASE_0C_CASES:
        synthesize_case(evidence, fixture_id, knobs)


__all__ = [
    "FakeSynthesisError",
    "FaultKnobs0c",
    "synthesize0c",
    "synthesize_case",
]
