from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.fixtures.models import (
    FreezeIntent,
    GateVersion,
    GoldenHashes,
    ManifestBinding,
    Phase0BFreezeReceipt,
    SourceSnapshot,
)
from services.fixtures.prepare import PrepareError, _load_canonical
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import PHASE_0B_CRITERIA, PHASE_0B_VARIANTS, GatePolicy, GateResult
from services.gates.serialization import canonical_gate_bytes
from services.toolchain.models import LockError, Phase0BToolchainLock, load_lock
from services.toolchain.verify import verify_binary

PHASE_0B_GOLDEN_DIR = Path("tests/goldens/reference/phase-0b")
PHASE_0B_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0b")


@dataclass(frozen=True, slots=True)
class Phase0BPrepareRequest:
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


def _load_manifest(root: Path, fixture_id: str) -> tuple[Phase0BFixtureManifest, bytes]:
    path = root / PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json"
    raw = path.read_bytes()
    manifest = Phase0BFixtureManifest.model_validate_json(raw)
    if raw != manifest.canonical_bytes():
        raise PrepareError(f"fixture manifest is noncanonical: {fixture_id}")
    return manifest, raw


def _golden_hashes(root: Path, manifests: dict[str, bytes]) -> GoldenHashes:
    phase_dir = root / PHASE_0B_GOLDEN_DIR
    source = phase_dir / "derive.py"
    expected = phase_dir / "expected.json"
    audit = phase_dir / "import-audit.json"
    index = phase_dir / "index.json"
    audited = audit_derivation_source(source)
    audit_payload = _canonical_json_file(audit)
    if audit_payload.get("audit_result") != "pass":
        raise PrepareError("frozen Golden import audit did not pass")
    if audit_payload.get("derivation_source_sha256") != audited.derivation_source_sha256:
        raise PrepareError("Golden derivation source hash drift")
    index_payload = _canonical_json_file(index)
    bindings = {
        "audit_sha256": sha256_file(audit),
        "derivation_source_sha256": sha256_file(source),
        "expected_sha256": sha256_file(expected),
        "fixture_manifest_sha256s": {
            fixture_id: hashlib.sha256(raw).hexdigest() for fixture_id, raw in manifests.items()
        },
    }
    if any(index_payload.get(key) != value for key, value in bindings.items()):
        raise PrepareError("Golden index binding drift")
    _assert_golden_agreement(json.loads(expected.read_bytes()), manifests)
    return GoldenHashes(
        derivation_source_sha256=sha256_file(source),
        expected_sha256=sha256_file(expected),
        audit_sha256=sha256_file(audit),
        index_sha256=sha256_file(index),
    )


def _canonical_json_file(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if raw != canonical.encode():
        raise PrepareError(f"Golden artifact is noncanonical: {path.name}")
    return payload


def _assert_golden_agreement(
    expected: dict[str, object],
    manifests: dict[str, bytes],
) -> None:
    variants = expected.get("variants")
    if not isinstance(variants, dict):
        raise PrepareError("Golden expected table is malformed")
    for fixture_id, raw in manifests.items():
        golden = variants.get(fixture_id)
        if not isinstance(golden, dict):
            raise PrepareError(f"Golden expected table is missing {fixture_id}")
        manifest = Phase0BFixtureManifest.model_validate_json(raw)
        for target in ("cfr30", "cfr24"):
            table = golden.get(target)
            if not isinstance(table, dict):
                raise PrepareError(f"Golden {target} table is missing {fixture_id}")
            conversion = manifest.conversions[target]
            if (
                table.get("output_frames") != conversion.output_frames
                or table.get("dropped_source_frames") != list(conversion.dropped_source_frames)
                or table.get("duplicated_source_frames")
                != list(conversion.duplicated_source_frames)
            ):
                raise PrepareError(f"manifest/golden conversion drift: {fixture_id} {target}")


def _verify_parent_gate_result(path: Path, root: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    result = GateResult.model_validate_json(raw)
    result_sha256 = hashlib.sha256(raw).hexdigest()
    from services.fixtures.prepare import parent_policy_path  # noqa: PLC0415

    frozen_0a_policy = parent_policy_path(root, "phase-0a", result.gate_version)
    policy_sha256 = sha256_file(frozen_0a_policy)
    if result.gate_id != "phase-0a":
        raise PrepareError("parent gate result must be a frozen phase-0a result")
    if not result.passed:
        raise PrepareError("parent phase-0a gate result did not pass")
    if result.policy_sha256 != policy_sha256:
        raise PrepareError("parent gate result is bound to a different phase-0a policy")
    return result_sha256, policy_sha256


def _verify_toolchain(lock_path: Path) -> Phase0BToolchainLock:
    try:
        lock = load_lock(lock_path)
        if not isinstance(lock, Phase0BToolchainLock):
            raise PrepareError("phase-0b requires the phase-0b toolchain lock")
        if (
            lock.smoke.resolve_readonly.status != "passed"
            or lock.smoke.ffmpeg_probe.status != "passed"
            or lock.smoke.ffmpeg_normalize.status != "passed"
        ):
            raise PrepareError("incomplete Phase 0B toolchain smoke results")
        recipes = tuple(recipe.fixture_id for recipe in lock.normalization.recipes)
        if recipes != PHASE_0B_VARIANTS:
            raise PrepareError("phase-0b lock is missing a variant normalization recipe")
        verify_binary(lock.ffmpeg.ffmpeg)
        verify_binary(lock.ffmpeg.ffprobe)
    except LockError as error:
        raise PrepareError(f"phase-0b toolchain lock rejected: {error}") from error
    return lock


def prepare_phase0b(request: Phase0BPrepareRequest) -> FreezeIntent:
    if request.fixture_ids != PHASE_0B_VARIANTS:
        raise PrepareError("phase-0b accepts exactly the six canonical fixture variants")
    if request.policy_out.exists() or request.freeze_receipt.exists():
        raise PrepareError("same-version re-freeze is forbidden")
    root = Path.cwd().resolve()
    manifests: dict[str, bytes] = {}
    for fixture_id in request.fixture_ids:
        _manifest, raw = _load_manifest(root, fixture_id)
        manifests[fixture_id] = raw
    _verify_toolchain(request.toolchain_lock)
    parent_sha256, _policy_sha256 = _verify_parent_gate_result(request.parent_result, root)
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
    golden_hashes = _golden_hashes(root, manifests)
    toolchain_hash = sha256_file(request.toolchain_lock)
    combined = hashlib.sha256()
    for fixture_id in request.fixture_ids:
        combined.update(manifests[fixture_id])
    combined_hash = combined.hexdigest()
    policy = GatePolicy(
        schema_version="gate-policy-v1",
        gate_id="phase-0b",
        gate_version=request.gate_version,
        parent_gate_result_hashes=(parent_sha256,),
        criteria=PHASE_0B_CRITERIA,
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_sha256=combined_hash,
        golden_sha256=golden_hashes.index_sha256,
        prerequisite_bindings=(),
        capability_allowlist=(),
    )
    policy_bytes = canonical_gate_bytes(policy)
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    receipt = Phase0BFreezeReceipt(
        gate_version=request.gate_version,
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        toolchain_lock_path=str(request.toolchain_lock.resolve(strict=True)),
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifests=tuple(
            ManifestBinding(
                fixture_id=fixture_id,
                path=str(
                    (root / PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json").resolve(strict=True)
                ),
                sha256=hashlib.sha256(manifests[fixture_id]).hexdigest(),
            )
            for fixture_id in request.fixture_ids
        ),
        fixture_manifests_combined_sha256=combined_hash,
        parent_gate_result_path=str(request.parent_result.resolve(strict=True)),
        parent_gate_result_sha256=parent_sha256,
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
        todo=24,
        gate_id="phase-0b",
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


def prepare_phase0b_errors() -> tuple[type[Exception], ...]:
    return (GoldenAuditError, LockError, OSError, PrepareError, ValidationError)
