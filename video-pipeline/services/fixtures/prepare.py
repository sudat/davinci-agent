from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.fixtures.models import (
    FreezeIntent,
    FreezeReceipt,
    GateVersion,
    GoldenHashes,
    Phase0AFixtureManifest,
    SourceSnapshot,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import PHASE_0A_CAPABILITIES, PHASE_0A_CRITERIA, GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.toolchain.models import LockError, load_lock


@dataclass(frozen=True, slots=True)
class PrepareError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    toolchain_lock: Path
    fixture_ids: tuple[str, ...]
    pre_source_snapshot: Path
    execution_contract: Path
    policy_out: Path
    freeze_receipt: Path
    staging: Path
    intent: Path
    gate_version: GateVersion = "v1"


POLICY_FILE_STEM: Final[dict[str, str]] = {
    "phase-0a": "phase-0a",
    "phase-0b": "phase-0b",
    "phase-0c": "phase-0c",
    "control-plane-baseline": "phase-1-control-plane",
    "phase-1-technical": "phase-1-technical",
    "phase-2": "phase-2",
    "phase-3": "phase-3",
}


def parent_policy_path(root: Path, gate_id: str, gate_version: str) -> Path:
    """The frozen policy file for a parent gate result of the given version."""
    stem = POLICY_FILE_STEM.get(gate_id)
    if stem is None:
        raise PrepareError(f"unknown gate id for policy lookup: {gate_id}")
    return root / "config" / "gates" / f"{stem}-{gate_version}.json"


def _load_canonical[Model: BaseModel](path: Path, model: type[Model]) -> tuple[bytes, Model]:
    raw = path.read_bytes()
    parsed = model.model_validate_json(raw)
    if raw != canonical_model_bytes(parsed):
        raise PrepareError(f"noncanonical input: {path}")
    return raw, parsed


def _golden_hashes(root: Path, manifest: Path) -> GoldenHashes:
    phase_dir = root / "tests/goldens/reference/phase-0a"
    source = phase_dir / "derive.py"
    expected = phase_dir / "expected.json"
    audit = phase_dir / "import-audit.json"
    index = phase_dir / "index.json"
    audited = audit_derivation_source(source)
    audit_raw = audit.read_bytes()
    audit_payload = json.loads(audit_raw)
    if audit_raw != json.dumps(
        audit_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode():
        raise PrepareError("Golden import audit is noncanonical")
    if audit_payload.get("audit_result") != "pass":
        raise PrepareError("frozen Golden import audit did not pass")
    if audit_payload.get("derivation_source_sha256") != audited.derivation_source_sha256:
        raise PrepareError("Golden derivation source hash drift")
    index_raw = index.read_bytes()
    index_payload = json.loads(index_raw)
    if index_raw != json.dumps(
        index_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode():
        raise PrepareError("Golden index is noncanonical")
    expected_bindings = {
        "audit_sha256": sha256_file(audit),
        "derivation_source_sha256": sha256_file(source),
        "expected_sha256": sha256_file(expected),
        "fixture_manifest_sha256": sha256_file(manifest),
    }
    if any(index_payload.get(key) != value for key, value in expected_bindings.items()):
        raise PrepareError("Golden index binding drift")
    return GoldenHashes(
        derivation_source_sha256=sha256_file(source),
        expected_sha256=sha256_file(expected),
        audit_sha256=sha256_file(audit),
        index_sha256=sha256_file(index),
    )


def prepare_freeze(request: PrepareRequest) -> FreezeIntent:
    if request.fixture_ids != ("p0a-cfr30-fixed",):
        raise PrepareError("phase-0a accepts only fixture p0a-cfr30-fixed")
    if request.policy_out.exists() or request.freeze_receipt.exists():
        raise PrepareError("same-version re-freeze is forbidden")
    root = Path.cwd().resolve()
    manifest_path = root / "tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = Phase0AFixtureManifest.model_validate_json(manifest_raw)
    if manifest_raw != manifest.canonical_bytes():
        raise PrepareError("fixture manifest is noncanonical")
    lock = load_lock(request.toolchain_lock)
    if lock.smoke.resolve_readonly.status != "passed" or lock.smoke.ffmpeg_probe.status != "passed":
        raise PrepareError("incomplete Phase 0A toolchain smoke results")
    contract_raw, _contract = _load_canonical(request.execution_contract, ExecutionContract)
    snapshot_raw, snapshot = _load_canonical(request.pre_source_snapshot, SourceSnapshot)
    contract_hash = hashlib.sha256(contract_raw).hexdigest()
    if snapshot.execution_contract_sha256 != contract_hash:
        raise PrepareError("pre-source snapshot execution-contract binding drift")
    policy_relative = request.policy_out.resolve().relative_to(root).as_posix()
    if any(entry.path == policy_relative for entry in snapshot.entries):
        raise PrepareError("same-version policy already existed in pre-source snapshot")
    golden_hashes = _golden_hashes(root, manifest_path)
    toolchain_hash = sha256_file(request.toolchain_lock)
    fixture_hash = sha256_file(manifest_path)
    policy = GatePolicy(
        schema_version="gate-policy-v1",
        gate_id="phase-0a",
        gate_version=request.gate_version,
        parent_gate_result_hashes=(),
        criteria=PHASE_0A_CRITERIA,
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_sha256=fixture_hash,
        golden_sha256=golden_hashes.index_sha256,
        prerequisite_bindings=(),
        capability_allowlist=PHASE_0A_CAPABILITIES,
    )
    policy_bytes = canonical_gate_bytes(policy)
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    receipt = FreezeReceipt(
        gate_version=request.gate_version,
        policy_path=str(request.policy_out.resolve()),
        policy_sha256=policy_hash,
        toolchain_lock_path=str(request.toolchain_lock.resolve(strict=True)),
        toolchain_lock_sha256=toolchain_hash,
        fixture_manifest_path=str(manifest_path.resolve(strict=True)),
        fixture_manifest_sha256=fixture_hash,
        golden_hashes=golden_hashes,
        pre_source_snapshot_path=str(request.pre_source_snapshot.resolve(strict=True)),
        pre_source_snapshot_sha256=hashlib.sha256(snapshot_raw).hexdigest(),
        execution_contract_path=str(request.execution_contract.resolve(strict=True)),
        execution_contract_sha256=contract_hash,
    )
    receipt_bytes = canonical_model_bytes(receipt)
    staged_policy = request.staging / "policy.json"
    staged_receipt = request.staging / "receipt.json"
    atomic_write(staged_policy, policy_bytes)
    atomic_write(staged_receipt, receipt_bytes)
    intent = FreezeIntent(
        todo=6,
        gate_id="phase-0a",
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


def prepare_errors() -> tuple[type[Exception], ...]:
    return (GoldenAuditError, LockError, OSError, PrepareError, ValidationError)
