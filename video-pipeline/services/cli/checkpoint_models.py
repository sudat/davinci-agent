"""Typed H1 checkpoint artifacts: the TTY display receipt and the checkpoint.

The display receipt is what ``checkpoint show`` prints and writes — the exact
target-hash set the operator saw when confirming. The operator checkpoint is
the strict artifact ``checkpoint export`` assembles (PRD H1): eligibility,
manifests, initial/final plan/IR/preview hashes, the ordered review event
chain, policy hashes, the displayed target hash, and the TTY operation-record
binding. Export rehashes every target before assembly; nothing here trusts an
authored hash.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

type CheckpointPurpose = Literal["EDITORIAL_APPROVED"]


class DisplayTargets(StrictModel):
    schema_version: Literal["display-targets-v1"] = "display-targets-v1"
    episode_id: Identifier
    stage: Literal["PREVIEW_READY"]
    plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    plan_sha256: Sha256
    ir_sha256: Sha256
    preview_sha256: Sha256
    trace_sha256: Sha256
    edit_source_world_sha256: Sha256


class DisplayReceipt(StrictModel):
    schema_version: Literal["display-receipt-v1"] = "display-receipt-v1"
    purpose: CheckpointPurpose
    targets: DisplayTargets
    target_bundle_sha256: Sha256

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)

    def target_digest(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self.targets)).hexdigest()


class CheckpointEventRow(StrictModel):
    event_id: Sha256
    kind: str
    proposal_id: str
    classification: str
    base_plan_version: str
    result_plan_version: str | None
    command_kind: str


class OperatorCheckpoint(StrictModel):
    schema_version: Literal["operator-checkpoint-v1"]
    record_type: Literal["operator_checkpoint"] = "operator_checkpoint"
    purpose: CheckpointPurpose
    episode_id: Identifier
    fixture_only: bool
    eligibility_status: str
    fixture_manifest_sha256: Sha256
    edit_source_world_sha256: Sha256
    media_sha256: tuple[Sha256, ...]
    initial_plan_sha256: Sha256
    initial_ir_sha256: Sha256
    initial_preview_sha256: Sha256
    final_plan_sha256: Sha256
    final_ir_sha256: Sha256
    final_preview_sha256: Sha256
    event_chain: tuple[CheckpointEventRow, ...]
    toolchain_lock_sha256: Sha256
    translator_policy_sha256: Sha256
    production_policy_sha256: Sha256
    displayed_target_sha256: Sha256
    display_receipt_sha256: Sha256
    operation_record_id: Identifier
    operation_record_sha256: Sha256
    actor_id: Identifier
    uid: int = Field(ge=0, strict=True)
    tty: str
    wall_time_unix: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_real_operator(self) -> OperatorCheckpoint:
        if self.fixture_only:
            raise PydanticCustomError(
                "fixture_checkpoint",
                "an operator checkpoint is a real single-user action record; "
                "fixture-marked lineages never produce one",
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)


__all__ = [
    "CheckpointEventRow",
    "DisplayReceipt",
    "DisplayTargets",
    "OperatorCheckpoint",
]
