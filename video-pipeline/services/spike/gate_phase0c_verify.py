"""Phase-0C per-case criterion recomputation over raw case evidence.

``check_case`` re-derives the Todo-28 validation outcome from the event's
bound proposal, checks candidate resolution/classification against the frozen
manifest and Golden tables, replays the event stream twice through the pure
reducer (determinism + stale/unclassified Stop classification), and delegates
decision semantics to ``gate_phase0c_semantics``. Never trusts an authored
pass field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.gates.phase0c import PHASE_0C_CRITERIA
from services.review_command.events import ReviewEvent0C, event_proposal
from services.review_command.reducer import ReduceConflictError, reduce
from services.review_command.validate import (
    ProposalValidationError,
    ProposalValidationOutcome,
    validate_proposal,
)
from services.spike.gate_phase0c_checks import GoldenCase0C, record_table_view
from services.spike.gate_phase0c_evidence import CaseEvidence, EvidenceLoadError
from services.spike.gate_phase0c_models import (
    CODE_AMBIGUOUS_AUTO_APPLIED,
    CODE_CONFLICT_SHAPE,
    CODE_GOLDEN_RECORD,
    CODE_MODEL_AUTHORED_DECISION,
    CODE_NEEDS_HUMAN_MISSING,
    CODE_TRACE_BINDING,
    CODE_TRANSLATOR_STATUS,
    CODE_WRONG_TARGET,
    CRITERION_AMBIGUOUS,
    CRITERION_CLEAR,
    CRITERION_CONFLICT,
    CRITERION_REBUILD,
    STOP_STALE_BINDING,
    STOP_UNCLASSIFIED,
)
from services.spike.gate_phase0c_semantics import check_decision_semantics

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.spike.gate_phase0c_checks import CheckState0c


def check_case(
    bundle: CaseEvidence, golden: GoldenCase0C, state: CheckState0c
) -> tuple[str, str, str]:
    """Recompute one case; returns (stop_criterion, stop_reason, deterministic_hash)."""

    for criterion in PHASE_0C_CRITERIA:
        for sha in bundle.raw_shas:
            state.note(criterion, sha)
    fid = bundle.fixture_id
    criterion = class_criterion(bundle.manifest.expected.classification)
    _check_trace0(bundle, state, criterion)
    if bundle.record.status != "proposal" or bundle.record.proposal_id is None:
        state.fail(
            criterion,
            CODE_TRANSLATOR_STATUS,
            f"{fid}: translator record status={bundle.record.status}",
        )
        return ("", "", "")
    try:
        outcome = validate_proposal(
            bundle.plan_base, event_proposal(_decision_event(bundle.events))
        )
    except ProposalValidationError as error:
        return (STOP_UNCLASSIFIED, f"{fid}: proposal invalid at validation: {error}", "")
    _check_classification(bundle, golden, outcome, state)
    return _check_replay_and_semantics(bundle, golden, outcome, state)


def class_criterion(classification: str) -> str:
    if classification == "ambiguous":
        return CRITERION_AMBIGUOUS
    if classification == "conflict":
        return CRITERION_CONFLICT
    return CRITERION_CLEAR


def _check_trace0(bundle: CaseEvidence, state: CheckState0c, criterion: str) -> None:
    """Recompute preview-0 coverage from the base IR video track."""

    fid = bundle.fixture_id
    video = tuple(
        item
        for track in bundle.ir_base.tracks
        if track.track.kind == "video"
        for item in track.items
    )
    expected_tiles = tuple(
        (item.record_span.start_frame, item.record_span.end_frame, "initial-plan-v1")
        for item in video
    )
    recorded = tuple(
        (entry.span.start_frame, entry.span.end_frame, entry.decision_id)
        for entry in bundle.trace0.record_to_decision
    )
    if (
        bundle.trace0.timeline_binding.plan_version != "v1"
        or bundle.trace0.timeline_binding.total_record_frames
        != video[-1].record_span.end_frame
        or recorded != expected_tiles
    ):
        state.fail(
            criterion,
            CODE_TRACE_BINDING,
            f"{fid}: preview-0 coverage/binding does not recompute from the base IR",
        )


def _decision_event(events: tuple[ReviewEvent0C, ...]) -> ReviewEvent0C:
    for event in reversed(events):
        if event.kind in ("decision_applied", "command_deferred"):
            return event
    raise EvidenceLoadError("no decision or deferred event in the log")


def _check_classification(
    bundle: CaseEvidence,
    golden: GoldenCase0C,
    outcome: ProposalValidationOutcome,
    state: CheckState0c,
) -> None:
    fid = bundle.fixture_id
    expected = bundle.manifest.expected
    if outcome.candidate_item_ids != expected.target_candidate_item_ids:
        state.fail(
            CRITERION_CONFLICT,
            CODE_WRONG_TARGET,
            f"{fid}: candidates {list(outcome.candidate_item_ids)} != manifest "
            f"{list(expected.target_candidate_item_ids)}",
        )
    if outcome.candidate_item_ids != golden.target_candidate_item_ids:
        state.fail(
            CRITERION_CONFLICT,
            CODE_WRONG_TARGET,
            f"{fid}: candidates != golden {list(golden.target_candidate_item_ids)}",
        )
    if outcome.classification != expected.classification:
        state.fail(
            CRITERION_CONFLICT,
            CODE_CONFLICT_SHAPE,
            f"{fid}: classification {outcome.classification} != manifest "
            f"{expected.classification}",
        )
    if expected.classification == "ambiguous" and not outcome.needs_human:
        state.fail(
            CRITERION_AMBIGUOUS,
            CODE_NEEDS_HUMAN_MISSING,
            f"{fid}: ambiguous outcome must record needs_human",
        )
    if expected.classification == "conflict" and (
        outcome.conflict is None or outcome.conflict.locked_field != "span"
    ):
        state.fail(
            CRITERION_CONFLICT,
            CODE_CONFLICT_SHAPE,
            f"{fid}: conflict case must record the colliding span lock",
        )


def _check_replay_and_semantics(
    bundle: CaseEvidence,
    golden: GoldenCase0C,
    outcome: ProposalValidationOutcome,
    state: CheckState0c,
) -> tuple[str, str, str]:
    fid = bundle.fixture_id
    deterministic_hash = ""
    replay_plan: EditPlan0C | None = None
    try:
        first = reduce(bundle.events, bundle.plan_base)
        second = reduce(bundle.events, bundle.plan_base)
        if first.deterministic_hash != second.deterministic_hash:
            state.fail(CRITERION_REBUILD, CODE_GOLDEN_RECORD, f"{fid}: rebuild not deterministic")
        deterministic_hash = first.deterministic_hash
        replay_plan = first.plan
    except ReduceConflictError as error:
        stop = _reduce_stop_or_fail(fid, outcome, error, state)
        if stop is not None:
            return (*stop, deterministic_hash)
    if replay_plan is not None and canonical_model_bytes(replay_plan) != canonical_model_bytes(
        bundle.plan_head
    ):
        state.fail(
            CRITERION_REBUILD,
            CODE_GOLDEN_RECORD,
            f"{fid}: store head plan does not match the deterministic replay",
        )
    if record_table_view(bundle.ir_head) != golden.record_table:
        state.fail(CRITERION_REBUILD, CODE_GOLDEN_RECORD, f"{fid}: head IR record table != golden")
    return check_decision_semantics(
        bundle, golden, state, deterministic_hash, class_criterion(golden.classification)
    )


def _reduce_stop_or_fail(
    fid: str,
    outcome: ProposalValidationOutcome,
    error: ReduceConflictError,
    state: CheckState0c,
) -> tuple[str, str] | None:
    if error.code == "stale_version":
        return (STOP_STALE_BINDING, f"{fid}: {error}")
    if error.code == "model_authored_decision":
        state.fail(CRITERION_AMBIGUOUS, CODE_MODEL_AUTHORED_DECISION, f"{fid}: {error}")
        return None
    if error.code == "decision_not_applicable":
        if outcome.classification == "ambiguous":
            state.fail(CRITERION_AMBIGUOUS, CODE_AMBIGUOUS_AUTO_APPLIED, f"{fid}: {error}")
        else:
            state.fail(CRITERION_CONFLICT, CODE_CONFLICT_SHAPE, f"{fid}: {error}")
        return None
    return (STOP_UNCLASSIFIED, f"{fid}: unclassified replay conflict: {error}")


__all__ = [
    "check_case",
    "class_criterion",
]
