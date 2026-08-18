"""Observation and marker models for the Phase-1 Technical Gate (Todo 46).

Observations record RAW outcomes only — never an authored pass flag. The
evaluator recomputes every frozen criterion from these files plus the raw
chain artifacts they point at (run reports, review bundles, review-store
plans/IRs, previews). ``H1Waiting`` is the human-checkpoint marker the gate
emits after a technical PASS: the flow STOPS there and stays NEEDS_HUMAN
until the owner records the real-episode ``EDITORIAL_APPROVED`` checkpoint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.job_runner.gate_cp_models import GateObservation, OperationOutcome

RUN_REPORT = "run-report.json"
BUNDLE_FILE = "review-bundle.json"
STORE_DIR = "review-store"
RESULT_NAME = "gate-result.json"
H1_WAITING_NAME = "h1-waiting.json"
APPROVALS_OBSERVATION = "approvals"
COVERAGE_NUM_X5 = 4  # coverage >= 80% <=> num * 5 >= den * 4 (integer math)


class ReviewStepRecord(StrictModel):
    """One declared review command exercised through the real translator."""

    command_index: int = Field(ge=1, strict=True)
    operation: str
    status: Literal["proposal", "error"]
    classification: str | None = None
    decision: Literal["applied", "deferred", "refused", "error"]
    structured: bool
    candidate_item_ids: tuple[str, ...] = ()
    version: int | None = None
    plan_sha256: Sha256 | None = None
    ir_sha256: Sha256 | None = None
    preview_sha256: Sha256 | None = None


class FixtureObservation(GateObservation):
    """One fixture's raw Phase-1 evidence: two chain runs + review steps."""

    kind: str = "phase1-e2e"
    run1_dir: str
    run2_dir: str
    review_steps: tuple[ReviewStepRecord, ...] = ()
    schema_gap_refused: bool = False

    def step(self, command_index: int) -> ReviewStepRecord:
        for candidate in self.review_steps:
            if candidate.command_index == command_index:
                return candidate
        raise KeyError(command_index)


class FixtureH1Binding(StrictModel):
    fixture_id: str
    bundle_path: str
    initial_plan_sha256: Sha256
    final_plan_sha256: Sha256
    initial_preview_sha256: Sha256
    final_preview_sha256: Sha256


class H1Waiting(StrictModel):
    """The Todo-46 H1 stop record: technical PASS, human action pending."""

    schema_version: Literal["h1-waiting-v1"]
    record_type: Literal["h1_waiting"] = "h1_waiting"
    todo: Literal[46] = 46
    gate_id: Literal["phase-1-technical"]
    gate_version: Literal["v1"]
    policy_sha256: Sha256
    next_checkpoint: Literal["H1"] = "H1"
    status: Literal["NEEDS_HUMAN"] = "NEEDS_HUMAN"
    human_time: Literal["not_evaluated"] = "not_evaluated"
    execution_work_id: str = Field(min_length=1)
    fixtures: tuple[FixtureH1Binding, ...] = Field(min_length=1)


__all__ = [
    "APPROVALS_OBSERVATION",
    "BUNDLE_FILE",
    "COVERAGE_NUM_X5",
    "H1_WAITING_NAME",
    "RESULT_NAME",
    "RUN_REPORT",
    "STORE_DIR",
    "FixtureH1Binding",
    "FixtureObservation",
    "H1Waiting",
    "OperationOutcome",
    "ReviewStepRecord",
]
