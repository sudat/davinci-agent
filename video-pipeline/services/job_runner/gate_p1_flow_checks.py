"""Criterion recomputation (part 2) for the Phase-1 Technical Gate (Todo 46).

- ``phase-1-ordering-stable``: deterministic critical defects 0 — coordinate
  and placement parity of the initial and post-correction plan/IR against the
  goldens (positional s*<->v* label mapping, partner-based A/V pairing) plus
  record/source ordering stability.
- ``phase-1-review-structured-coverage``: declared review commands that
  round-trip the structured translator with a typed classification
  (clear/ambiguous) over all declared commands >= 80% (integer form
  ``num*5 >= den*4``); per-command golden outcome parity; and the
  non-declared schema-gap probe refused. Ambiguous-but-structured commands
  count as covered: the structured vocabulary represents them; only the
  APPLY decision is deferred to the human.
- ``phase-1-deterministic-plan-ir``: two independent chain runs and the
  replayed correction sequence regenerate identical plan/IR hashes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.gates.phase1_technical import PHASE_1_TECHNICAL_CRITERIA
from services.job_runner.gate_p1_checks import (
    CheckState,
    MalformedEvidenceError,
    load_reports,
    mapping_of,
    op_sha,
)
from services.job_runner.gate_p1_golden_tables import load_ir_golden, load_plan_golden
from services.job_runner.gate_p1_models import COVERAGE_NUM_X5
from services.job_runner.gate_p1_parity import (
    ParityError,
    check_ordering,
    compare,
    load_ir,
)

if TYPE_CHECKING:
    from services.job_runner.gate_p1_models import FixtureObservation

C_ORDER = PHASE_1_TECHNICAL_CRITERIA[2]
C_COVER = PHASE_1_TECHNICAL_CRITERIA[3]
C_DET = PHASE_1_TECHNICAL_CRITERIA[4]
BUNDLE = "review-bundle.json"


def check_placement(
    observation: FixtureObservation, golden: Mapping[str, object], state: CheckState
) -> None:
    fixture = observation.fixture_id
    store = Path(observation.run1_dir) / "review-store"
    ir1_path = store / "ir-v1.json"
    ir1 = load_ir(ir1_path)
    notes = [sha256_file(ir1_path)]
    try:
        compare(ir1, load_plan_golden(golden, "plan_items"))
        compare(ir1, load_ir_golden(golden, "ir_records"), av_labels=False)
        check_ordering(ir1)
    except ParityError as error:
        state.fail(C_ORDER, error.code, f"{fixture}: {error.detail}")
    bundle = mapping_of(json.loads((Path(observation.run1_dir) / BUNDLE).read_bytes()))
    current = mapping_of(bundle["current"])
    version = str(current["plan_version"])
    final_plan = store / f"plan-{version}.json"
    final_ir = store / f"ir-{version}.json"
    if not final_ir.is_file() or not final_plan.is_file():
        state.fail(C_ORDER, "evidence-incomplete", f"{fixture}: final plan/IR v{version} missing")
        return
    rows = load_ir(final_ir)
    notes.extend((sha256_file(final_ir), sha256_file(final_plan)))
    try:
        compare(rows, load_plan_golden(golden, "review_final_plan_items"))
        compare(rows, load_ir_golden(golden, "review_final_ir_records"), av_labels=False)
        check_ordering(rows)
    except ParityError as error:
        state.fail(C_ORDER, error.code, f"{fixture} final: {error.detail}")
    state.note(C_ORDER, *notes)


def check_outcomes(
    observation: FixtureObservation, golden: Mapping[str, object], state: CheckState
) -> None:
    fixture = observation.fixture_id
    outcomes = golden["review_outcomes"]
    if not isinstance(outcomes, list):
        raise MalformedEvidenceError("review_outcomes is not an array")
    if len(outcomes) != len(observation.review_steps):
        state.fail(
            C_COVER, "review-outcome-mismatch", f"{fixture}: step count != golden outcomes"
        )
        return
    for expected_raw, step in zip(outcomes, observation.review_steps, strict=True):
        expected = mapping_of(expected_raw)
        decision = "applied" if expected["decision"] == "apply" else "deferred"
        targets = expected["target_candidate_ids"]
        if not isinstance(targets, list):
            raise MalformedEvidenceError("target_candidate_ids is not an array")
        if (
            step.command_index != expected["command_index"]
            or step.classification != expected["classification"]
            or step.decision != decision
            or list(step.candidate_item_ids) != [str(item) for item in targets]
        ):
            state.fail(
                C_COVER,
                "review-outcome-mismatch",
                f"{fixture}#{step.command_index}: {step.decision}/{step.classification} "
                f"!= golden {expected['decision']}/{expected['classification']}",
            )
            return


def check_coverage(
    observations: Mapping[str, FixtureObservation],
    goldens: Mapping[str, object],
    state: CheckState,
) -> None:
    num = den = 0
    for fixture_id, observation in observations.items():
        state.note(C_COVER, op_sha(observation))
        for step in observation.review_steps:
            den += 1
            num += 1 if step.structured else 0
        golden = mapping_of(goldens[fixture_id])
        check_outcomes(observation, golden, state)
        if not observation.schema_gap_refused:
            state.fail(
                C_COVER,
                "schema-gap-coerced",
                f"{fixture_id}: non-declared instruction was not refused",
            )
    if den == 0 or num * 5 < den * COVERAGE_NUM_X5:
        state.fail(
            C_COVER,
            "coverage-below-80",
            f"structured coverage {num}/{den} below the 80% threshold",
        )


def check_determinism(observation: FixtureObservation, state: CheckState) -> None:
    fixture = observation.fixture_id
    run1, run2 = load_reports(observation)
    for key in (
        "selection_plan_sha256",
        "edit_plan_sha256",
        "production_ir_sha256",
        "review_plan_sha256",
        "review_ir_sha256",
    ):
        if run1[key] != run2[key]:
            state.fail(
                C_DET, "nondeterministic-regeneration", f"{fixture}: {key} differs across runs"
            )
            return
    bundle1 = mapping_of(json.loads((Path(observation.run1_dir) / BUNDLE).read_bytes()))
    bundle2 = mapping_of(json.loads((Path(observation.run2_dir) / BUNDLE).read_bytes()))
    current1 = mapping_of(bundle1["current"])
    current2 = mapping_of(bundle2["current"])
    for key in ("plan_sha256", "ir_sha256"):
        if current1[key] != current2[key]:
            state.fail(
                C_DET,
                "nondeterministic-correction",
                f"{fixture}: final {key} differs across correction replays",
            )
            return
    state.note(
        C_DET,
        sha256_file(Path(observation.fields["run1_report"])),
        sha256_file(Path(observation.fields["run2_report"])),
    )


__all__ = [
    "C_COVER",
    "C_DET",
    "C_ORDER",
    "check_coverage",
    "check_determinism",
    "check_outcomes",
    "check_placement",
]
