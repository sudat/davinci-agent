from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.fixtures.models import FreezeReceipt
from services.gates import GatePolicy, canonical_gate_bytes
from services.gates.verify_policy import PolicyVerificationError, verify_policy


def test_frozen_policy_is_canonical_and_has_empty_prerequisites() -> None:
    policy_path = Path("config/gates/phase-0a-v1.json")
    raw = policy_path.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.parent_gate_result_hashes == ()
    assert policy.prerequisite_bindings == ()
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None
    assert policy.golden_sha256 is not None


def test_freeze_receipt_matches_policy_bytes() -> None:
    receipt_path = Path(
        "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
        "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75/"
        "task-6-freeze-receipt.json"
    )
    policy_path = Path("config/gates/phase-0a-v1.json")
    receipt = FreezeReceipt.model_validate_json(receipt_path.read_bytes())

    assert receipt.policy_sha256 == hashlib.sha256(policy_path.read_bytes()).hexdigest()


def test_post_result_policy_edit_is_rejected(tmp_path: Path) -> None:
    policy = Path("config/gates/phase-0a-v1.json")
    receipt = Path(
        "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
        "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75/"
        "task-6-freeze-receipt.json"
    )
    edited = tmp_path / "policy.json"
    payload = json.loads(policy.read_bytes())
    payload["gate_version"] = "v2"
    edited.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PolicyVerificationError, match="policy hash"):
        verify_policy(edited, receipt, None, None)
