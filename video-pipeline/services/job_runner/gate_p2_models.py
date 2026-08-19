"""Observation and marker models for the Phase-2 Finalization Gate (Todo 54).

Observations record RAW outcomes only — never an authored pass flag. The
evaluator re-derives every declared route from the raw artifact bytes the
fields point at (compiled packages, build outputs, render files, QC
reports, final-review bundles and ledgers). ``Phase2ExitMarker`` is the
truthful checkpoint record the gate emits on PASS: the five fixture Final
records stay ``fixture_only`` and are never publication decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.job_runner.gate_cp_models import GateObservation

RESULT_NAME = "gate-result.json"
EXIT_MARKER_NAME = "phase-2-exit.json"
OBSERVATION_NAME = "observation.json"
ROUTE_KEYS = (
    "package_compilation",
    "failure_code",
    "readback",
    "render",
    "qc",
    "retry",
    "human_route",
)


class P2FixtureObservation(GateObservation):
    """One fixture's raw Phase-2 evidence: fault routing + clean-path artifacts."""

    kind: str = "phase2-finalization"
    fixture_only: Literal[True] = True
    declared_route: dict[str, str] = Field(default_factory=dict)
    observed_route: dict[str, str] = Field(default_factory=dict)
    manual_resolve_ui_used: Literal["none", "true"] = "none"


class FixtureExitBinding(StrictModel):
    fixture_id: str
    fixture_record_fixture_only: bool
    bundle_path: str | None = None
    render_sha256: Sha256 | None = None
    qc_verdict: str | None = None
    build_output_path: str | None = None
    approval_path: str | None = None
    approval_refusal_path: str | None = None
    human_route_path: str | None = None


class Phase2ExitMarker(StrictModel):
    """The Todo-54 exit record: fixtures complete, nothing published."""

    schema_version: Literal["phase-2-exit-v1"]
    record_type: Literal["phase_2_exit"] = "phase_2_exit"
    todo: Literal[54] = 54
    gate_id: Literal["phase-2"]
    gate_version: Literal["v1"]
    policy_sha256: Sha256
    next_checkpoint: Literal["PHASE_2_EXIT"] = "PHASE_2_EXIT"
    status: Literal["FIXTURES_COMPLETE_FIXTURE_ONLY"] = "FIXTURES_COMPLETE_FIXTURE_ONLY"
    fixture_final_records: Literal["fixture_only"] = "fixture_only"
    fixture_records_are_publication_decisions: Literal[False] = False
    human_time: Literal["not_evaluated"] = "not_evaluated"
    manual_resolve_ui_used: Literal["none"] = "none"
    fixtures: tuple[FixtureExitBinding, ...] = Field(min_length=1)


__all__ = [
    "EXIT_MARKER_NAME",
    "OBSERVATION_NAME",
    "RESULT_NAME",
    "ROUTE_KEYS",
    "FixtureExitBinding",
    "P2FixtureObservation",
    "Phase2ExitMarker",
]
