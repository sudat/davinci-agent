"""Strict models for the final-review decision routes.

Input and result value models only (transient retry envelope, structured
correction, cycle artifacts, approval and unsupported results); the route
functions and their typed refusals live in ``services.final_review.routes``.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from services.contracts.primitives import PositiveInteger, Sha256, StrictModel
from services.manual_finalization.freeze_models import (  # noqa: TC001 (pydantic resolves it at runtime)
    FreezePackage,
)

MAX_TRANSIENT_ATTEMPTS: int = 3

CorrectionKind = Literal["remove_segment", "adjust_source_span", "correct_subtitle"]


class TransientFailureInput(StrictModel):
    failure_code: str = Field(min_length=1, strict=True)
    attempts_used: PositiveInteger
    detail: str = ""


class RunnerRetryEnvelope(StrictModel):
    failure_code: str
    failure_class: Literal["transient"]
    attempts_used: int
    max_attempts: int
    retry: bool


class StructuredCorrection(StrictModel):
    instruction: str = Field(min_length=1, strict=True)
    classification: CorrectionKind
    base_plan_sha256: Sha256


class CycleArtifacts(StrictModel):
    plan_sha256: Sha256
    preview_sha256: Sha256
    version: PositiveInteger


class CorrectionExecutor(Protocol):
    """Re-enters the Todo-45 propose/apply machinery for a new plan version."""

    def reenter(self, correction: StructuredCorrection) -> CycleArtifacts: ...


class CorrectionCycleResult(StrictModel):
    event_seq: PositiveInteger
    artifacts: CycleArtifacts
    superseded_approvals: tuple[str, ...]


class FinalApprovalResult(StrictModel):
    record_id: str
    target_set_hash: Sha256
    event_seq: PositiveInteger


class UnsupportedRouteResult(StrictModel):
    package: FreezePackage
    version: PositiveInteger


__all__ = [
    "MAX_TRANSIENT_ATTEMPTS",
    "CorrectionCycleResult",
    "CorrectionExecutor",
    "CorrectionKind",
    "CycleArtifacts",
    "FinalApprovalResult",
    "RunnerRetryEnvelope",
    "StructuredCorrection",
    "TransientFailureInput",
    "UnsupportedRouteResult",
]
