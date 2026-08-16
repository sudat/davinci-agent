from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.execution.preflight import ExecutionContract
from services.fixtures.models import FreezeReceipt, SourceSnapshot
from services.foundation_io import canonical_model_bytes, sha256_file
from services.gates import GatePolicy, canonical_gate_bytes


@dataclass(frozen=True, slots=True)
class PolicyVerificationError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


def _load_receipt(path: Path) -> FreezeReceipt:
    raw = path.read_bytes()
    receipt = FreezeReceipt.model_validate_json(raw)
    if raw != canonical_model_bytes(receipt):
        raise PolicyVerificationError("freeze receipt is noncanonical")
    return receipt


def _load_snapshot(path: Path) -> SourceSnapshot:
    raw = path.read_bytes()
    snapshot = SourceSnapshot.model_validate_json(raw)
    if raw != canonical_model_bytes(snapshot):
        raise PolicyVerificationError(f"source snapshot is noncanonical: {path}")
    return snapshot


def _verify_referenced_bytes(policy: GatePolicy, receipt: FreezeReceipt) -> None:
    bindings = (
        (policy.toolchain_lock_sha256, receipt.toolchain_lock_sha256, receipt.toolchain_lock_path),
        (
            policy.fixture_manifest_sha256,
            receipt.fixture_manifest_sha256,
            receipt.fixture_manifest_path,
        ),
        (policy.golden_sha256, receipt.golden_hashes.index_sha256, None),
    )
    for policy_hash, receipt_hash, source_path in bindings:
        if policy_hash != receipt_hash:
            raise PolicyVerificationError("policy referenced hash differs from freeze receipt")
        if source_path is not None and sha256_file(Path(source_path)) != receipt_hash:
            raise PolicyVerificationError("freeze receipt referenced bytes drift")


def _verify_execution_contract(path: Path, expected_hash: str) -> None:
    contract_raw = path.read_bytes()
    contract = ExecutionContract.model_validate_json(contract_raw)
    if contract_raw != canonical_model_bytes(contract):
        raise PolicyVerificationError("execution contract is noncanonical")
    if hashlib.sha256(contract_raw).hexdigest() != expected_hash:
        raise PolicyVerificationError("execution contract binding drift")


def _verify_precedence(
    policy_path: Path,
    policy_hash: str,
    pre_snapshot: SourceSnapshot,
    post_snapshot_path: Path,
    contract_hash: str,
) -> None:
    post_snapshot = _load_snapshot(post_snapshot_path)
    if post_snapshot.execution_contract_sha256 != contract_hash:
        raise PolicyVerificationError("post-source snapshot execution-contract binding drift")
    root = Path.cwd().resolve()
    relative_policy = policy_path.resolve().relative_to(root).as_posix()
    if any(entry.path == relative_policy for entry in pre_snapshot.entries):
        raise PolicyVerificationError("policy did not follow the pre-source snapshot")
    matching = tuple(entry for entry in post_snapshot.entries if entry.path == relative_policy)
    if len(matching) != 1 or matching[0].sha256 != policy_hash:
        raise PolicyVerificationError("policy does not precede the required source snapshot")


def verify_policy(
    policy_path: Path,
    receipt_path: Path,
    post_source_snapshot: Path | None,
    execution_contract: Path | None,
) -> None:
    policy_raw = policy_path.read_bytes()
    policy = GatePolicy.model_validate_json(policy_raw)
    if policy_raw != canonical_gate_bytes(policy):
        raise PolicyVerificationError("policy is noncanonical")
    actual_policy_hash = hashlib.sha256(policy_raw).hexdigest()
    receipt = _load_receipt(receipt_path)
    if receipt.policy_sha256 != actual_policy_hash:
        raise PolicyVerificationError("policy hash differs from freeze receipt")
    if Path(receipt.policy_path).resolve() != policy_path.resolve():
        raise PolicyVerificationError("policy path differs from freeze receipt")
    _verify_referenced_bytes(policy, receipt)
    pre_snapshot_path = Path(receipt.pre_source_snapshot_path)
    if sha256_file(pre_snapshot_path) != receipt.pre_source_snapshot_sha256:
        raise PolicyVerificationError("pre-source snapshot hash drift")
    pre_snapshot = _load_snapshot(pre_snapshot_path)
    contract_hash = receipt.execution_contract_sha256
    if pre_snapshot.execution_contract_sha256 != contract_hash:
        raise PolicyVerificationError("pre-source snapshot execution-contract binding drift")
    if execution_contract is not None:
        _verify_execution_contract(execution_contract, contract_hash)
    if post_source_snapshot is not None:
        _verify_precedence(
            policy_path,
            actual_policy_hash,
            pre_snapshot,
            post_source_snapshot,
            contract_hash,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--freeze-receipt", type=Path, required=True)
    parser.add_argument("--require-precedes-source-snapshot", type=Path)
    parser.add_argument("--execution-contract", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        verify_policy(
            arguments.policy,
            arguments.freeze_receipt,
            arguments.require_precedes_source_snapshot,
            arguments.execution_contract,
        )
    except (OSError, PolicyVerificationError, ValidationError) as error:
        print(error)
        return 2
    print("policy verified: phase-0a v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
