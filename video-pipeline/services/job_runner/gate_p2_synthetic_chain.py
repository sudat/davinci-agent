"""Synthetic Phase-2 fault-harness chain: parents, H1 checkpoint pair, receipt.

Builds the truthful temporary canonical context the offline fault harness
runs against: three canonical synthetic parent gate results, one clearly
synthetic (fully typed and self-consistent) operator checkpoint +
display-receipt pair that passes the REAL H1 verifier, and a typed freeze
receipt bound to the derived synthetic policy. Nothing here reads the
deleted attempt directory or mutates the frozen policy bytes; the derived
policy rebinds exactly the transient fields (gate version, three parent
hashes, selected toolchain lock, current fixture manifests, and the one
synthetic prerequisite binding) while keeping every frozen binding.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import TypeAdapter

from services.cli.checkpoint_models import (
    DisplayReceipt,
    DisplayTargets,
    OperatorCheckpoint,
)
from services.fixtures.goldens_phase2 import phase2_golden_hashes
from services.fixtures.models import (
    ManifestBinding,
    ParentResultLink,
    Phase2FreezeReceipt,
)
from services.foundation_io import canonical_model_bytes, sha256_file
from services.gates import (
    CriterionResult,
    EvidenceBundleRef,
    GatePolicy,
    GateResult,
)
from services.gates.models import OperatorCheckpointBinding
from services.job_runner import gate_p2_checks

SYNTHETIC_VERSION: Final = "synthetic-fault-harness-v1"
_GATE_VERSION: Final = TypeAdapter(Literal["v1", "v2", "v3", "v4"])
SYNTHETIC_EPISODE_ID: Final = "ep-synthetic-fault-harness-v1"
CHECKPOINT_RELATIVE: Final = "checkpoints/synthetic/operator-checkpoint.json"
DISPLAY_RECEIPT_RELATIVE: Final = "checkpoints/synthetic/display-receipt.json"
PARENT_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("phase-0a", "phase-0a"),
    ("phase-0b", "phase-0b"),
    ("phase-1-technical", "phase-1-technical"),
)


@dataclass(frozen=True, slots=True)
class SyntheticParent:
    relative: str
    result: GateResult
    raw: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class FaultContext:
    policy: GatePolicy
    policy_bytes: bytes
    policy_sha256: str
    parents: tuple[SyntheticParent, ...]
    freeze_receipt: Phase2FreezeReceipt
    checkpoint_relative: str
    checkpoint_sha256: str
    checkpoint_raw: bytes
    display_receipt_raw: bytes


def _seed_hash(domain: str) -> str:
    seed = f"phase2-fault-harness:v1:{domain}".encode()
    return hashlib.sha256(seed).hexdigest()


def _current_fixture_manifests() -> tuple[dict[str, bytes], str]:
    manifests = {
        fixture_id: (gate_p2_checks.MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        for fixture_id in gate_p2_checks.PHASE_2_FIXTURES
    }
    combined = hashlib.sha256()
    for fixture_id in gate_p2_checks.PHASE_2_FIXTURES:
        combined.update(manifests[fixture_id])
    return manifests, combined.hexdigest()


def _synthetic_parent(gate_id: str, relative: str) -> SyntheticParent:
    result = GateResult(
        schema_version="gate-result-v1",
        gate_id=gate_id,
        gate_version=f"{SYNTHETIC_VERSION}:{gate_id}",
        policy_sha256=_seed_hash(f"{gate_id}:policy"),
        passed=True,
        evidence_bundles=(
            EvidenceBundleRef(
                bundle_sha256=_seed_hash(f"{gate_id}:bundle"),
                raw_evidence_sha256s=(_seed_hash(f"{gate_id}:bundle:raw"),),
            ),
        ),
        criteria_results=(
            CriterionResult(
                criterion_id=f"{gate_id}:synthetic-parent-pass",
                passed=True,
                raw_evidence_sha256s=(_seed_hash(f"{gate_id}:criterion:raw"),),
            ),
        ),
    )
    raw = canonical_model_bytes(result)
    return SyntheticParent(
        relative=relative,
        result=result,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )

def build_synthetic_parents() -> tuple[SyntheticParent, ...]:
    return tuple(_synthetic_parent(gate_id, relative) for gate_id, relative in PARENT_SPECS)


def _synthetic_checkpoint(
    toolchain_lock_sha256: str,
) -> tuple[bytes, bytes, str, OperatorCheckpointBinding]:
    """A clearly synthetic but fully typed H1 pair the real verifier accepts."""

    targets = DisplayTargets(
        episode_id=SYNTHETIC_EPISODE_ID,
        stage="PREVIEW_READY",
        plan_version="v1",
        plan_sha256=_seed_hash("targets:plan"),
        ir_sha256=_seed_hash("targets:ir"),
        preview_sha256=_seed_hash("targets:preview"),
        trace_sha256=_seed_hash("targets:trace"),
        edit_source_world_sha256=_seed_hash("targets:world"),
    )
    bundle_sha256 = hashlib.sha256(canonical_model_bytes(targets)).hexdigest()
    receipt = DisplayReceipt(
        purpose="EDITORIAL_APPROVED", targets=targets, target_bundle_sha256=bundle_sha256
    )
    receipt_raw = canonical_model_bytes(receipt)
    checkpoint = OperatorCheckpoint(
        schema_version="operator-checkpoint-v1",
        purpose="EDITORIAL_APPROVED",
        episode_id=SYNTHETIC_EPISODE_ID,
        fixture_only=False,
        eligibility_status="supported",
        fixture_manifest_sha256=_seed_hash("checkpoint:fixture-manifests"),
        edit_source_world_sha256=targets.edit_source_world_sha256,
        media_sha256=(_seed_hash("checkpoint:media"),),
        initial_plan_sha256=targets.plan_sha256,
        initial_ir_sha256=targets.ir_sha256,
        initial_preview_sha256=targets.preview_sha256,
        final_plan_sha256=targets.plan_sha256,
        final_ir_sha256=targets.ir_sha256,
        final_preview_sha256=targets.preview_sha256,
        event_chain=(),
        toolchain_lock_sha256=toolchain_lock_sha256,
        translator_policy_sha256=_seed_hash("checkpoint:translator-policy"),
        production_policy_sha256=_seed_hash("checkpoint:production-policy"),
        displayed_target_sha256=bundle_sha256,
        display_receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
        operation_record_id=f"op-{SYNTHETIC_EPISODE_ID}",
        operation_record_sha256=_seed_hash("checkpoint:operation-record"),
        actor_id=SYNTHETIC_EPISODE_ID,
        uid=0,
        tty="synthetic-fault-harness",
        wall_time_unix=0,
    )
    checkpoint_raw = canonical_model_bytes(checkpoint)
    binding = OperatorCheckpointBinding(
        kind="operator_checkpoint",
        purpose="EDITORIAL_APPROVED",
        episode_id=SYNTHETIC_EPISODE_ID,
        target_plan_sha256=checkpoint.final_plan_sha256,
        target_timeline_ir_sha256=checkpoint.final_ir_sha256,
        target_preview_sha256=checkpoint.final_preview_sha256,
        operation_record_sha256=checkpoint.operation_record_sha256,
        checkpoint_sha256=hashlib.sha256(checkpoint_raw).hexdigest(),
    )
    return checkpoint_raw, receipt_raw, binding.checkpoint_sha256, binding


def build_synthetic_context(source: GatePolicy, policy_path: Path) -> FaultContext:
    """Derive the synthetic context from the supplied current frozen policy."""

    manifests, fixture_manifest_sha256 = _current_fixture_manifests()
    toolchain_lock_sha256 = sha256_file(gate_p2_checks.LOCK_PATH)
    parents = build_synthetic_parents()
    checkpoint_raw, display_receipt_raw, checkpoint_sha256, binding = (
        _synthetic_checkpoint(toolchain_lock_sha256)
    )
    document = source.model_dump(mode="json")
    document["gate_version"] = SYNTHETIC_VERSION
    document["parent_gate_result_hashes"] = [parent.sha256 for parent in parents]
    document["toolchain_lock_sha256"] = toolchain_lock_sha256
    document["fixture_manifest_sha256"] = fixture_manifest_sha256
    document["prerequisite_bindings"] = [binding]
    policy = GatePolicy.model_validate(document)
    policy_bytes = canonical_model_bytes(policy)
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    receipt = Phase2FreezeReceipt(
        gate_version=_GATE_VERSION.validate_python(source.gate_version),
        policy_path=str(policy_path),
        policy_sha256=policy_sha256,
        toolchain_lock_path=str(gate_p2_checks.LOCK_PATH),
        toolchain_lock_sha256=toolchain_lock_sha256,
        fixture_manifests=tuple(
            ManifestBinding(
                fixture_id=fixture_id,
                path=str(gate_p2_checks.MANIFEST_DIR / f"{fixture_id}.json"),
                sha256=hashlib.sha256(manifests[fixture_id]).hexdigest(),
            )
            for fixture_id in gate_p2_checks.PHASE_2_FIXTURES
        ),
        fixture_manifests_combined_sha256=fixture_manifest_sha256,
        parent_gate_results=tuple(
            ParentResultLink(
                gate_id=parent.result.gate_id,
                path=f"{parent.relative}/gate-result.json",
                sha256=parent.sha256,
            )
            for parent in parents
        ),
        prerequisite_checkpoint_path=CHECKPOINT_RELATIVE,
        prerequisite_checkpoint=binding,
        golden_hashes=phase2_golden_hashes(Path.cwd(), manifests),
        # The synthetic chain honors exactly one contract: the supplied frozen
        # policy. These provenance fields name it instead of borrowing real
        # freeze artifacts the synthetic run never saw.
        pre_source_snapshot_path=str(policy_path),
        pre_source_snapshot_sha256=sha256_file(policy_path),
        execution_contract_path=str(policy_path),
        execution_contract_sha256=sha256_file(policy_path),
    )
    return FaultContext(
        policy=policy,
        policy_bytes=policy_bytes,
        policy_sha256=policy_sha256,
        parents=parents,
        freeze_receipt=receipt,
        checkpoint_relative=CHECKPOINT_RELATIVE,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_raw=checkpoint_raw,
        display_receipt_raw=display_receipt_raw,
    )
