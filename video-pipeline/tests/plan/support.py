"""Shared Todo-42 rig: planner inputs derived only from frozen Phase-1 inputs.

Every planner input here is assembled through the Todo-40 builders over a
frozen Phase-1 fixture manifest and the frozen golden tables
(``tests/goldens/reference/phase-1-technical/expected.json``).

The director document is the PRE-budget selection: golden rows whose
``reason_code`` is exactly ``budget-dropped`` are the keeps the PLANNER
itself is responsible for dropping under the declared
``drop-lowest-score-non-must`` rule, so they re-enter the director proposal
as selected and the solver must reproduce the golden drop. Weights, budget,
ordering, and must-include sets come from the manifest's declared rule spec;
observations come from the manifest's declared analyzer inputs. No clocks,
no randomness, no network, no invented candidates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.editorial_model import (
    EditorialSelectionProposal,
    SelectionEntry,
)
from services.editorial.proposal_builder import build_selection_proposal
from services.editorial.reconcile import ReconciliationResult, reconcile
from services.fixtures.manifest_phase1 import DurationBudgetRules
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.plan.planner_models import (
    CandidateObservation,
    DeclaredOrderLock,
    PlannerInput,
    ScoreWeights,
)
from services.validate.selection_models import EditSourceFacts
from tests.editorial.support import GOLDEN_EXPECTED_PATH, load_manifest
from tests.editorial.test_selection_proposal import index_for, pool_for

if TYPE_CHECKING:
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

ALL_FIXTURE_IDS = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)


def _golden_fixture_entry(fixture_id: str) -> dict[str, object]:
    document: object = json.loads(Path(GOLDEN_EXPECTED_PATH).read_bytes())
    assert isinstance(document, dict)
    fixtures = document["fixtures"]
    assert isinstance(fixtures, dict)
    entry = fixtures[fixture_id]
    assert isinstance(entry, dict)
    return entry


def golden_selection(fixture_id: str) -> dict[str, object]:
    selection = _golden_fixture_entry(fixture_id)["selection"]
    assert isinstance(selection, dict)
    return selection


def manifest_for(fixture_id: str) -> Phase1TechnicalFixtureManifest:
    return load_manifest(fixture_id)


def _golden_table_rows(fixture_id: str) -> list[dict[str, object]]:
    table = _golden_fixture_entry(fixture_id)["candidate_table"]
    assert isinstance(table, list)
    rows: list[dict[str, object]] = []
    for row in table:
        assert isinstance(row, dict)
        rows.append(row)
    return rows


def _pre_budget_entries(fixture_id: str) -> tuple[tuple[SelectionEntry, ...], int]:
    """Golden rows as PRE-budget entries; planner-owned drops re-select."""

    entries: list[SelectionEntry] = []
    selected_count = 0
    for row in _golden_table_rows(fixture_id):
        budget_dropped = row["reason_code"] == "budget-dropped"
        action = "selected" if row["decision"] == "selected" or budget_dropped else "dropped"
        if action == "selected":
            selected_count += 1
        entries.append(
            SelectionEntry(
                segment_id=str(row["segment_id"]),
                action=action,  # type: ignore[arg-type]
                reason_code="score-selected"
                if budget_dropped
                else str(row["reason_code"]),
            )
        )
    return tuple(entries), selected_count


def pre_budget_document(fixture_id: str) -> dict[str, object]:
    """The director proposal document BEFORE planner budget enforcement."""

    entries, selected_count = _pre_budget_entries(fixture_id)
    return {
        "schema_version": "editorial-selection-proposal-v1",
        "proposal_id": f"sel-{fixture_id}-pre-budget-v1",
        "episode_id": fixture_id,
        "actor_intent": "model",
        "selection": [entry.model_dump(mode="json") for entry in entries],
        "confidence": (selected_count, len(entries)),
    }


def _reconciled(fixture_id: str) -> ReconciliationResult:
    entries, selected_count = _pre_budget_entries(fixture_id)
    manifest = load_manifest(fixture_id)
    proposal = EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id=f"sel-{fixture_id}-pre-budget-v1",
        episode_id=fixture_id,
        actor_intent="model",
        selection=entries,
        confidence=(selected_count, len(entries)),
    )
    return reconcile(proposal, pool_for(manifest), index_for(manifest))


def segment_map(fixture_id: str) -> dict[str, str]:
    """candidate_id -> segment_id over the pre-budget reconcile linkage."""

    return {
        candidate_id: link.segment_id
        for link in _reconciled(fixture_id).segment_links
        for candidate_id in link.candidate_ids
    }


def candidate_id_of_segment(fixture_id: str, segment_id: str, *, intent: str = "keep") -> str:
    """The reconciled candidate id of one segment with the given intent."""

    mapping = segment_map(fixture_id)
    for candidate in _reconciled(fixture_id).candidates:
        if mapping[candidate.candidate_id] == segment_id and candidate.intent == intent:
            return candidate.candidate_id
    raise AssertionError(f"no {intent} candidate for segment {segment_id}")


def order_lock(before_segment: str, after_segment: str, fixture_id: str) -> DeclaredOrderLock:
    return DeclaredOrderLock(
        before_candidate_id=candidate_id_of_segment(fixture_id, before_segment),
        after_candidate_id=candidate_id_of_segment(fixture_id, after_segment),
        basis="tests/plan/support:declared-order-lock",
    )


def planner_input_for(  # noqa: PLR0913 (frozen fixture rig parameters)
    fixture_id: str,
    *,
    max_output_frames: int | None = None,
    min_output_frames: int | None = None,
    edit_total_frames: int | None = None,
    edit_source_sha: str | None = None,
    must_include: tuple[str, ...] | None = None,
    order_locks: tuple[DeclaredOrderLock, ...] = (),
    allowlist: tuple[str, ...] = PHASE_0A_CAPABILITIES,
) -> PlannerInput:
    manifest = load_manifest(fixture_id)
    pool = pool_for(manifest)
    proposal: SelectionPlanProposal = build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool,
        director_document=pre_budget_document(fixture_id),
        evidence_index=index_for(manifest),
        plan_base_version="plan-base-v0",
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )
    segments = {segment.segment_id: segment for segment in manifest.transcript.segments}
    mapping = segment_map(fixture_id)
    observations = tuple(
        CandidateObservation(
            candidate_id=candidate.candidate_id,
            kind=segments[mapping[candidate.candidate_id]].kind,
            content_score=segments[mapping[candidate.candidate_id]].observed.content_score,
            clarity_score=segments[mapping[candidate.candidate_id]].observed.clarity_score,
        )
        for candidate in proposal.candidates
    )
    declared = manifest.editorial_rules.duration_budget
    budget = DurationBudgetRules(
        rule_id=declared.rule_id,
        max_output_frames=(
            declared.max_output_frames if max_output_frames is None else max_output_frames
        ),
        min_output_frames=(
            declared.min_output_frames if min_output_frames is None else min_output_frames
        ),
        enforcement=declared.enforcement,
    )
    return PlannerInput(
        episode_id=proposal.episode_id,
        candidates=proposal.candidates,
        must_include=proposal.must_include if must_include is None else must_include,
        budget=budget,
        ordering=proposal.ordering,
        weights=ScoreWeights(
            content_weight=manifest.editorial_rules.scoring.content_weight,
            clarity_weight=manifest.editorial_rules.scoring.clarity_weight,
        ),
        observations=observations,
        order_locks=order_locks,
        capability_allowlist=tuple(allowlist),
        edit_source=EditSourceFacts(
            source_id=pool.source_id,
            edit_source_sha=pool.edit_source_sha
            if edit_source_sha is None
            else edit_source_sha,
            total_frames=pool.total_frames if edit_total_frames is None else edit_total_frames,
        ),
    )
