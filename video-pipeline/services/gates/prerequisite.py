"""Operator-checkpoint prerequisite verification for the Phase-2 freeze.

Reuses the ``services.cli.checkpoint`` verify rules on the exported H1
``operator_checkpoint`` artifact: the purpose must be ``EDITORIAL_APPROVED``,
the episode must be real (fixture-marked checkpoints and the frozen Phase-1
synthetic episode ids are refused), and every display binding is recomputed
from bytes — the sibling display receipt must still hash to the checkpoint's
``display_receipt_sha256``, its target digest must be self-consistent, and it
must display the same episode the checkpoint binds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.cli.checkpoint_models import DisplayReceipt, OperatorCheckpoint
from services.foundation_io import canonical_model_bytes
from services.gates.models import OperatorCheckpointBinding
from services.gates.phase1_technical import PHASE_1_TECHNICAL_FIXTURES

DISPLAY_RECEIPT_NAME = "display-receipt.json"


@dataclass(frozen=True, slots=True)
class PrerequisiteError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


def verify_editorial_approved_checkpoint(checkpoint_path: Path) -> OperatorCheckpoint:
    """Validate the H1 checkpoint artifact and its sibling display receipt."""

    raw = checkpoint_path.read_bytes()
    try:
        checkpoint = OperatorCheckpoint.model_validate_json(raw)
    except ValidationError as error:
        first = error.errors()[0]
        raise PrerequisiteError(
            f"operator checkpoint invalid ({first.get("type")}): a synthetic "
            "(fixture-marked) checkpoint can never satisfy the Phase-2 prerequisite"
        ) from error
    if checkpoint.purpose != "EDITORIAL_APPROVED":
        raise PrerequisiteError(
            f"operator checkpoint purpose is {checkpoint.purpose}, required EDITORIAL_APPROVED"
        )
    if checkpoint.fixture_only or checkpoint.episode_id in PHASE_1_TECHNICAL_FIXTURES:
        raise PrerequisiteError(
            f"operator checkpoint episode {checkpoint.episode_id} is a synthetic fixture; "
            "a real owner-supplied episode is required"
        )
    receipt_path = checkpoint_path.parent / DISPLAY_RECEIPT_NAME
    try:
        receipt_raw = receipt_path.read_bytes()
        receipt = DisplayReceipt.model_validate_json(receipt_raw)
    except (OSError, ValidationError) as error:
        raise PrerequisiteError(
            f"operator checkpoint display receipt unreadable beside {checkpoint_path}"
        ) from error
    if hashlib.sha256(canonical_model_bytes(receipt)).hexdigest() != (
        checkpoint.display_receipt_sha256
    ):
        raise PrerequisiteError(
            "operator checkpoint display receipt no longer hashes to the binding"
        )
    if receipt.target_bundle_sha256 != checkpoint.displayed_target_sha256:
        raise PrerequisiteError("operator checkpoint displayed target drift")
    if receipt.target_digest() != receipt.target_bundle_sha256:
        raise PrerequisiteError("operator checkpoint display receipt is self-inconsistent")
    if receipt.targets.episode_id != checkpoint.episode_id:
        raise PrerequisiteError("operator checkpoint receipt displays a different episode")
    return checkpoint


def checkpoint_binding(checkpoint_path: Path) -> OperatorCheckpointBinding:
    checkpoint = verify_editorial_approved_checkpoint(checkpoint_path)
    return OperatorCheckpointBinding(
        kind="operator_checkpoint",
        purpose=checkpoint.purpose,
        episode_id=checkpoint.episode_id,
        target_plan_sha256=checkpoint.final_plan_sha256,
        target_timeline_ir_sha256=checkpoint.final_ir_sha256,
        target_preview_sha256=checkpoint.final_preview_sha256,
        operation_record_sha256=checkpoint.operation_record_sha256,
        checkpoint_sha256=hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
    )
