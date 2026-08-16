from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.fixtures.manifest import Phase0AFixtureManifest, Sequence

GateId = Literal["phase-0a", "phase-0b", "phase-0c"]
FreezeTodo = Literal[6, 24, 26]


class GoldenHashes(StrictModel):
    derivation_source_sha256: Sha256
    expected_sha256: Sha256
    audit_sha256: Sha256
    index_sha256: Sha256


class ManifestBinding(StrictModel):
    fixture_id: str
    path: str
    sha256: Sha256


class FreezeReceipt(StrictModel):
    schema_version: Literal["freeze-receipt-v1"] = "freeze-receipt-v1"
    record_type: Literal["freeze_receipt"] = "freeze_receipt"
    todo: Literal[6] = 6
    gate_id: Literal["phase-0a"] = "phase-0a"
    gate_version: Literal["v1"] = "v1"
    policy_path: str
    policy_sha256: Sha256
    toolchain_lock_path: str
    toolchain_lock_sha256: Sha256
    fixture_manifest_path: str
    fixture_manifest_sha256: Sha256
    golden_hashes: GoldenHashes
    pre_source_snapshot_path: str
    pre_source_snapshot_sha256: Sha256
    execution_contract_path: str
    execution_contract_sha256: Sha256


class Phase0BFreezeReceipt(StrictModel):
    schema_version: Literal["freeze-receipt-v1"] = "freeze-receipt-v1"
    record_type: Literal["freeze_receipt"] = "freeze_receipt"
    todo: Literal[24] = 24
    gate_id: Literal["phase-0b"] = "phase-0b"
    gate_version: Literal["v1"] = "v1"
    policy_path: str
    policy_sha256: Sha256
    toolchain_lock_path: str
    toolchain_lock_sha256: Sha256
    fixture_manifests: tuple[ManifestBinding, ...]
    fixture_manifests_combined_sha256: Sha256
    parent_gate_result_path: str
    parent_gate_result_sha256: Sha256
    golden_hashes: GoldenHashes
    pre_source_snapshot_path: str
    pre_source_snapshot_sha256: Sha256
    execution_contract_path: str
    execution_contract_sha256: Sha256


class Phase0CFreezeReceipt(StrictModel):
    schema_version: Literal["freeze-receipt-v1"] = "freeze-receipt-v1"
    record_type: Literal["freeze_receipt"] = "freeze_receipt"
    todo: Literal[26] = 26
    gate_id: Literal["phase-0c"] = "phase-0c"
    gate_version: Literal["v1"] = "v1"
    policy_path: str
    policy_sha256: Sha256
    toolchain_lock_path: str
    toolchain_lock_sha256: Sha256
    fixture_manifests: tuple[ManifestBinding, ...]
    fixture_manifests_combined_sha256: Sha256
    parent_gate_result_path: str
    parent_gate_result_sha256: Sha256
    golden_hashes: GoldenHashes
    pre_source_snapshot_path: str
    pre_source_snapshot_sha256: Sha256
    execution_contract_path: str
    execution_contract_sha256: Sha256


class FreezeIntent(StrictModel):
    schema_version: Literal["freeze-intent-v1"] = "freeze-intent-v1"
    event_type: Literal["freeze-intent"] = "freeze-intent"
    todo: FreezeTodo
    gate_id: GateId
    gate_version: Literal["v1"] = "v1"
    staged_policy_path: str
    policy_path: str
    policy_sha256: Sha256
    staged_receipt_path: str
    receipt_path: str
    receipt_sha256: Sha256
    execution_contract_sha256: Sha256


class FreezeEventRow(StrictModel):
    schema_version: Literal["freeze-event-v1"] = "freeze-event-v1"
    sequence: int = Field(gt=0, strict=True)
    previous_event_hash: Sha256
    event_type: Literal["freeze-intent", "freeze-completed"]
    todo: FreezeTodo
    gate_id: GateId
    gate_version: Literal["v1"] = "v1"
    intent_sha256: Sha256
    policy_sha256: Sha256
    receipt_sha256: Sha256


class SourceSnapshotEntry(StrictModel):
    path: str
    size: int = Field(ge=0, strict=True)
    sha256: Sha256


class SourceSnapshot(StrictModel):
    schema_version: Literal["source-snapshot-v1"]
    execution_contract_sha256: Sha256
    file_count: int = Field(ge=0, strict=True)
    tree_sha256: Sha256
    entries: Sequence[SourceSnapshotEntry]


__all__ = [
    "FreezeEventRow",
    "FreezeIntent",
    "FreezeReceipt",
    "GoldenHashes",
    "ManifestBinding",
    "Phase0AFixtureManifest",
    "Phase0BFreezeReceipt",
    "Phase0CFreezeReceipt",
    "SourceSnapshot",
]
