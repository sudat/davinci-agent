"""Phase-1-technical freeze preparation: five fixtures, TWO parent gate results."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.golden import GoldenAuditError
from services.fixtures.goldens_phase1 import phase1_golden_hashes
from services.fixtures.manifest_phase1 import (
    PHASE_1_FIXTURE_IDS,
    Phase1TechnicalFixtureManifest,
)
from services.fixtures.models import (
    FreezeIntent,
    GateVersion,
    ManifestBinding,
    ParentResultLink,
    Phase1TechnicalFreezeReceipt,
    SourceSnapshot,
)
from services.fixtures.prepare import PrepareError, _load_canonical
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import (
    PHASE_1_TECHNICAL_CRITERIA,
    PHASE_1_TECHNICAL_FIXTURES,
    GatePolicy,
    GateResult,
)
from services.gates.serialization import canonical_gate_bytes
from services.toolchain.models import LockError, Phase1TechnicalToolchainLock, load_lock

PHASE_1_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-1-technical")
PHASE_1_PARENT_GATES = ("phase-0c", "control-plane-baseline")


@dataclass(frozen=True, slots=True)
class Phase1PrepareRequest:
    toolchain_lock: Path
    fixture_ids: tuple[str, ...]
    parent_results: tuple[Path, Path]
    pre_source_snapshot: Path
    execution_contract: Path
    policy_out: Path
    freeze_receipt: Path
    staging: Path
    intent: Path
    gate_version: GateVersion = "v1"


def _load_manifest(root: Path, fixture_id: str) -> tuple[Phase1TechnicalFixtureManifest, bytes]:
    path = root / PHASE_1_MANIFEST_DIR / f"{fixture_id}.json"
    raw = path.read_bytes()
    manifest = Phase1TechnicalFixtureManifest.model_validate_json(raw)
    if manifest.fixture_id != fixture_id:
        raise PrepareError(f"fixture manifest id drift: {fixture_id}")
    if manifest.phase != "phase-1-technical" or not manifest.fixture_only:
        raise PrepareError(f"fixture manifest is not a phase-1 technical fixture: {fixture_id}")
    if raw != manifest.canonical_bytes():
        raise PrepareError(f"fixture manifest is noncanonical: {fixture_id}")
    return manifest, raw


def verify_phase1_toolchain(lock_path: Path) -> Phase1TechnicalToolchainLock:
    try:
        lock = load_lock(lock_path)
    except LockError as error:
        raise PrepareError(f"phase-1 toolchain lock rejected: {error}") from error
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise PrepareError("phase-1-technical requires the phase-1-technical toolchain lock")
    for record in (
        lock.smoke.resolve_readonly,
        lock.smoke.ffmpeg_probe,
        lock.smoke.ffmpeg_normalize,
        lock.smoke.preview_review,
        lock.smoke.whisper_ja,
        lock.smoke.editorial_model,
    ):
        if record.status != "passed":
            raise PrepareError("incomplete Phase 1 toolchain smoke results")
    if lock.whisper_ja.adapter != "whisper-cpp-cli":
        raise PrepareError("phase-1-technical pins the whisper.cpp CLI adapter")
    if lock.editorial_model.external_credentials != "none":
        raise PrepareError("phase-1-technical pins no external credentials")
    return lock


def _verify_parent_gate_results(
    paths: tuple[Path, Path],
    root: Path,
) -> tuple[ParentResultLink, ...]:
    from services.fixtures.prepare import parent_policy_path  # noqa: PLC0415

    links: list[ParentResultLink] = []
    for path, gate_id in zip(paths, PHASE_1_PARENT_GATES, strict=True):
        raw = path.read_bytes()
        result = GateResult.model_validate_json(raw)
        if result.gate_id != gate_id:
            raise PrepareError(f"parent gate result must be a frozen {gate_id} result")
        if not result.passed:
            raise PrepareError(f"parent {gate_id} gate result did not pass")
        policy_hash = sha256_file(parent_policy_path(root, gate_id, result.gate_version))
        if result.policy_sha256 != policy_hash:
            raise PrepareError(f"parent gate result is bound to a different {gate_id} policy")
        links.append(
            ParentResultLink(
                gate_id=gate_id,
                path=str(path.resolve(strict=True)),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(links)


def prepare_phase1_technical(request: Phase1PrepareRequest) -> FreezeIntent:
    if request.fixture_ids != PHASE_1_TECHNICAL_FIXTURES or request.fixture_ids != (
        PHASE_1_FIXTURE_IDS
    ):
        raise PrepareError("phase-1-technical accepts exactly the five canonical fixture ids")
    if request.policy_out.exists() or request.freeze_receipt.exists():
        raise PrepareError("same-version re-freeze is forbidden")
    root = Path.cwd().resolve()
    manifests: dict[str, bytes] = {}
    for fixture_id in request.fixture_ids:
        _manifest, raw = _load_manifest(root, fixture_id)
        manifests[fixture_id] = raw
    verify_phase1_toolchain(request.toolchain_lock)
    parents = _verify_parent_gate_results(request.parent_results, root)
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
    golden_hashes = phase1_golden_hashes(root, manifests)
    toolchain_hash = sha256_file(request.toolchain_lock)
    combined = hashlib.sha256()
    for fixture_id in request.fixture_ids:
        combined.update(manifests[fixture_id])
    combined_hash = combined.hexdigest()
    policy = GatePolicy(
        schema_version="gate-policy-v1",
        gate_id="phase-1-technical",
        gate_version=request.gate_version,
        parent_gate_result_hashes=tuple(link.sha256 for link in parents),
        criteria=PHASE_1_TECHNICAL_CRITERIA,
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_sha256=combined_hash,
        golden_sha256=golden_hashes.index_sha256,
        prerequisite_bindings=(),
        capability_allowlist=(),
    )
    policy_bytes = canonical_gate_bytes(policy)
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    receipt = Phase1TechnicalFreezeReceipt(
        gate_version=request.gate_version,
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        toolchain_lock_path=str(request.toolchain_lock.resolve(strict=True)),
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifests=tuple(
            ManifestBinding(
                fixture_id=fixture_id,
                path=str(
                    (root / PHASE_1_MANIFEST_DIR / f"{fixture_id}.json").resolve(strict=True)
                ),
                sha256=hashlib.sha256(manifests[fixture_id]).hexdigest(),
            )
            for fixture_id in request.fixture_ids
        ),
        fixture_manifests_combined_sha256=combined_hash,
        parent_gate_results=parents,
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
        todo=32,
        gate_id="phase-1-technical",
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


def prepare_phase1_errors() -> tuple[type[Exception], ...]:
    return (GoldenAuditError, LockError, OSError, PrepareError, ValidationError)
