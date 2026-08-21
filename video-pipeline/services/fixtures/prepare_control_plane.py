from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.manifest_control_plane import (
    CONTROL_PLANE_FIXTURE_IDS,
    ControlPlaneFixtureManifest,
)
from services.fixtures.models import (
    ControlPlaneFreezeReceipt,
    FreezeIntent,
    GateVersion,
    ManifestBinding,
    SourceSnapshot,
)
from services.fixtures.prepare import PrepareError, _load_canonical
from services.fixtures.prepare_phase0c import verify_phase0c_toolchain
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import (
    CONTROL_PLANE_FIXTURES,
    PHASE_1_CONTROL_PLANE_CRITERIA,
    GatePolicy,
    GateResult,
    canonical_gate_bytes,
)
from services.toolchain.models import LockError

CONTROL_PLANE_MANIFEST_DIR = Path("tests/fixtures/manifests/control-plane")


@dataclass(frozen=True, slots=True)
class ControlPlanePrepareRequest:
    toolchain_lock: Path
    fixture_ids: tuple[str, ...]
    parent_result: Path
    pre_source_snapshot: Path
    execution_contract: Path
    policy_out: Path
    freeze_receipt: Path
    staging: Path
    intent: Path
    gate_version: GateVersion = "v1"


def _load_manifest(root: Path, fixture_id: str) -> tuple[ControlPlaneFixtureManifest, bytes]:
    path = root / CONTROL_PLANE_MANIFEST_DIR / f"{fixture_id}.json"
    raw = path.read_bytes()
    manifest = ControlPlaneFixtureManifest.model_validate_json(raw)
    if raw != manifest.canonical_bytes():
        raise PrepareError(f"fixture manifest is noncanonical: {fixture_id}")
    return manifest, raw


def _verify_parent_gate_result(path: Path, root: Path) -> str:
    raw = path.read_bytes()
    result = GateResult.model_validate_json(raw)
    if result.gate_id != "phase-0c":
        raise PrepareError("parent gate result must be the frozen phase-0c v1 result")
    if not result.passed:
        raise PrepareError("parent phase-0c gate result did not pass")
    from services.fixtures.prepare import parent_policy_path  # noqa: PLC0415

    frozen_0c_policy = parent_policy_path(root, "phase-0c", result.gate_version)
    if result.policy_sha256 != sha256_file(frozen_0c_policy):
        raise PrepareError("parent gate result is bound to a different phase-0c policy")
    return hashlib.sha256(raw).hexdigest()


def prepare_control_plane(request: ControlPlanePrepareRequest) -> FreezeIntent:
    if request.fixture_ids != CONTROL_PLANE_FIXTURES or request.fixture_ids != (
        CONTROL_PLANE_FIXTURE_IDS
    ):
        raise PrepareError("control-plane accepts exactly the six canonical fixture ids")
    if request.policy_out.exists() or request.freeze_receipt.exists():
        raise PrepareError("same-version re-freeze is forbidden")
    root = Path.cwd().resolve()
    manifests: dict[str, bytes] = {}
    for fixture_id in request.fixture_ids:
        _manifest, raw = _load_manifest(root, fixture_id)
        manifests[fixture_id] = raw
    verify_phase0c_toolchain(request.toolchain_lock)
    parent_sha256 = _verify_parent_gate_result(request.parent_result, root)
    contract_raw, _contract = _load_canonical(request.execution_contract, ExecutionContract)
    snapshot_raw, snapshot = _load_canonical(request.pre_source_snapshot, SourceSnapshot)
    contract_hash = hashlib.sha256(contract_raw).hexdigest()
    if snapshot.execution_contract_sha256 != contract_hash:
        raise PrepareError("pre-source snapshot execution-contract binding drift")
    try:
        policy_relative = request.policy_out.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise PrepareError("policy must be published under the pipeline root") from error
    if any(entry.path == policy_relative for entry in snapshot.entries):
        raise PrepareError("same-version policy already existed in pre-source snapshot")
    toolchain_hash = sha256_file(request.toolchain_lock)
    combined = hashlib.sha256()
    for fixture_id in request.fixture_ids:
        combined.update(manifests[fixture_id])
    combined_hash = combined.hexdigest()
    policy = GatePolicy(
        schema_version="gate-policy-v1",
        gate_id="control-plane-baseline",
        gate_version=request.gate_version,
        parent_gate_result_hashes=(parent_sha256,),
        criteria=PHASE_1_CONTROL_PLANE_CRITERIA,
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_sha256=combined_hash,
        golden_sha256=None,
        prerequisite_bindings=(),
        capability_allowlist=(),
    )
    policy_bytes = canonical_gate_bytes(policy)
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    receipt = ControlPlaneFreezeReceipt(
        gate_version=request.gate_version,
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        toolchain_lock_path=str(request.toolchain_lock.resolve(strict=True)),
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifests=tuple(
            ManifestBinding(
                fixture_id=fixture_id,
                path=str(
                    (root / CONTROL_PLANE_MANIFEST_DIR / f"{fixture_id}.json").resolve(strict=True)
                ),
                sha256=hashlib.sha256(manifests[fixture_id]).hexdigest(),
            )
            for fixture_id in request.fixture_ids
        ),
        fixture_manifests_combined_sha256=combined_hash,
        parent_gate_result_path=str(request.parent_result.resolve(strict=True)),
        parent_gate_result_sha256=parent_sha256,
        pre_source_snapshot_path=str(request.pre_source_snapshot.resolve(strict=True)),
        pre_source_snapshot_sha256=hashlib.sha256(snapshot_raw).hexdigest(),
        execution_contract_path=str(request.execution_contract.resolve(strict=True)),
        execution_contract_sha256=contract_hash,
    )
    receipt_bytes = canonical_model_bytes(receipt)
    staged_policy = request.staging / "policy.json"
    staged_receipt = request.staging / "receipt.json"
    request.staging.mkdir(parents=True, exist_ok=True)
    atomic_write(staged_policy, policy_bytes)
    atomic_write(staged_receipt, receipt_bytes)
    intent = FreezeIntent(
        todo=7,
        gate_id="control-plane-baseline",
        gate_version=request.gate_version,
        staged_policy_path=str(staged_policy.resolve(strict=True)),
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        staged_receipt_path=str(staged_receipt.resolve(strict=True)),
        receipt_path=str(request.freeze_receipt.resolve()),
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        execution_contract_sha256=contract_hash,
    )
    atomic_write(request.intent, canonical_model_bytes(intent))
    return intent


def prepare_control_plane_errors() -> tuple[type[Exception], ...]:
    return (LockError, OSError, PrepareError, ValidationError)
