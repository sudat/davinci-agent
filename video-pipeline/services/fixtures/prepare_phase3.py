"""Phase-3 freeze preparation: two brand fixtures, ONE parent (phase-2).

Guards (in order): canonical fixture ids; no pre-existing Phase-3 product
module (the freeze must precede implementation); no same-version re-freeze;
manifest/pair validation (identical editorial structure, declared-only
presentation differences, owner-created rights on every stored asset byte);
complete toolchain smoke; the passed phase-2 parent result bound to the
frozen policy bytes; and the independent Golden binding.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.golden import GoldenAuditError
from services.fixtures.goldens_phase3 import phase3_golden_hashes
from services.fixtures.manifest_phase3 import (
    PHASE_3_FIXTURE_IDS,
    Phase3FixtureManifest,
)
from services.fixtures.models import (
    FreezeIntent,
    GateVersion,
    ManifestBinding,
    ParentResultLink,
    Phase3FreezeReceipt,
    SourceSnapshot,
)
from services.fixtures.prepare import PrepareError, _load_canonical
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import (
    PHASE_3_CRITERIA,
    PHASE_3_FIXTURES,
    GatePolicy,
    GateResult,
)
from services.gates.serialization import canonical_gate_bytes
from services.toolchain.models import LockError, Phase3ToolchainLock, load_lock

PHASE_3_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
PHASE_3_PARENT_GATE = "phase-2"
PHASE_3_PARENT_POLICY = "config/gates/phase-2-v1.json"
PHASE_3_PRODUCT_MODULE = Path("services/presentation")


@dataclass(frozen=True, slots=True)
class Phase3PrepareRequest:
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


def _load_manifest(root: Path, fixture_id: str) -> tuple[Phase3FixtureManifest, bytes]:
    path = root / PHASE_3_MANIFEST_DIR / f"{fixture_id}.json"
    raw = path.read_bytes()
    manifest = Phase3FixtureManifest.model_validate_json(raw)
    if manifest.fixture_id != fixture_id:
        raise PrepareError(f"fixture manifest id drift: {fixture_id}")
    if manifest.phase != "phase-3" or not manifest.fixture_only:
        raise PrepareError(f"fixture manifest is not a phase-3 fixture: {fixture_id}")
    if raw != manifest.canonical_bytes():
        raise PrepareError(f"fixture manifest is noncanonical: {fixture_id}")
    return manifest, raw


def verify_phase3_toolchain(lock_path: Path) -> Phase3ToolchainLock:
    try:
        lock = load_lock(lock_path)
    except LockError as error:
        raise PrepareError(f"phase-3 toolchain lock rejected: {error}") from error
    if not isinstance(lock, Phase3ToolchainLock):
        raise PrepareError("phase-3 requires the phase-3 toolchain lock")
    for record in (
        lock.smoke.resolve_readonly,
        lock.smoke.ffmpeg_probe,
        lock.smoke.ffmpeg_normalize,
        lock.smoke.preview_review,
        lock.smoke.whisper_ja,
        lock.smoke.editorial_model,
        lock.smoke.resolve_package,
        lock.smoke.render_qc,
        lock.smoke.presentation_assets,
    ):
        if record.status != "passed":
            raise PrepareError("incomplete Phase 3 toolchain smoke results")
    return lock


def _verify_parent_gate_result(path: Path, root: Path) -> ParentResultLink:
    raw = path.read_bytes()
    result = GateResult.model_validate_json(raw)
    if result.gate_id != PHASE_3_PARENT_GATE:
        raise PrepareError("parent gate result must be a frozen phase-2 result")
    if not result.passed:
        raise PrepareError("parent phase-2 gate result did not pass")
    from services.fixtures.prepare import parent_policy_path  # noqa: PLC0415

    policy_hash = sha256_file(parent_policy_path(root, "phase-2", result.gate_version))
    if result.policy_sha256 != policy_hash:
        raise PrepareError("parent gate result is bound to a different phase-2 policy")
    return ParentResultLink(
        gate_id=PHASE_3_PARENT_GATE,
        path=str(path.resolve(strict=True)),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _verify_pair(manifests: dict[str, Phase3FixtureManifest]) -> None:
    base_a = manifests["p3-brand-a"].editorial
    base_b = manifests["p3-brand-b"].editorial
    if base_a.editorial_structure_sha256 != base_b.editorial_structure_sha256:
        raise PrepareError("editorial structure hashes differ between the brands")
    if base_a.model_dump(exclude={"editorial_structure_sha256"}) != base_b.model_dump(
        exclude={"editorial_structure_sha256"}
    ):
        raise PrepareError("editorial structures differ between the brands")
    if (
        manifests["p3-brand-a"].declared_diff.dimensions
        != manifests["p3-brand-b"].declared_diff.dimensions
    ):
        raise PrepareError("declared diff dimensions differ between the brands")
    for fixture_id, manifest in manifests.items():
        for asset in manifest.presentation.assets:
            asset_path = Path.cwd() / PHASE_3_MANIFEST_DIR / asset.path
            if sha256_file(asset_path) != asset.sha256:
                raise PrepareError(f"stored asset bytes drift: {fixture_id}/{asset.kind}")


def _guard_product_module(root: Path, gate_version: GateVersion) -> None:
    """v1 input-freeze precedes implementation; a re-freeze re-binds the code."""
    if gate_version == "v1":
        if (root / PHASE_3_PRODUCT_MODULE).exists():
            raise PrepareError(
                "phase-3 product code already exists; the input freeze must precede implementation"
            )
    elif not (root / PHASE_3_PRODUCT_MODULE).exists():
        raise PrepareError(
            "a same-gate re-freeze requires the implemented product module to re-bind"
        )


def prepare_phase3(request: Phase3PrepareRequest) -> FreezeIntent:
    if request.fixture_ids != PHASE_3_FIXTURES or request.fixture_ids != PHASE_3_FIXTURE_IDS:
        raise PrepareError("phase-3 accepts exactly the two canonical brand fixture ids")
    root = Path.cwd().resolve()
    _guard_product_module(root, request.gate_version)
    if request.policy_out.exists() or request.freeze_receipt.exists():
        raise PrepareError("same-version re-freeze is forbidden")
    parsed: dict[str, Phase3FixtureManifest] = {}
    manifests: dict[str, bytes] = {}
    for fixture_id in request.fixture_ids:
        manifest, raw = _load_manifest(root, fixture_id)
        parsed[fixture_id] = manifest
        manifests[fixture_id] = raw
    _verify_pair(parsed)
    verify_phase3_toolchain(request.toolchain_lock)
    parent = _verify_parent_gate_result(request.parent_result, root)
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
    golden_hashes = phase3_golden_hashes(root, manifests)
    toolchain_hash = sha256_file(request.toolchain_lock)
    combined = hashlib.sha256()
    for fixture_id in request.fixture_ids:
        combined.update(manifests[fixture_id])
    combined_hash = combined.hexdigest()
    policy = GatePolicy(
        schema_version="gate-policy-v1",
        gate_id="phase-3",
        gate_version=request.gate_version,
        parent_gate_result_hashes=(parent.sha256,),
        criteria=PHASE_3_CRITERIA,
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_sha256=combined_hash,
        golden_sha256=golden_hashes.index_sha256,
        prerequisite_bindings=(),
        capability_allowlist=(),
    )
    policy_bytes = canonical_gate_bytes(policy)
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    receipt = Phase3FreezeReceipt(
        gate_version=request.gate_version,
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        toolchain_lock_path=str(request.toolchain_lock.resolve(strict=True)),
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifests=tuple(
            ManifestBinding(
                fixture_id=fixture_id,
                path=str((root / PHASE_3_MANIFEST_DIR / f"{fixture_id}.json").resolve(strict=True)),
                sha256=hashlib.sha256(manifests[fixture_id]).hexdigest(),
            )
            for fixture_id in request.fixture_ids
        ),
        fixture_manifests_combined_sha256=combined_hash,
        parent_gate_results=(parent,),
        golden_hashes=golden_hashes,
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
        todo=56,
        gate_id="phase-3",
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


def prepare_phase3_errors() -> tuple[type[Exception], ...]:
    return (GoldenAuditError, LockError, OSError, PrepareError, ValidationError)
