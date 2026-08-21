from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.models import Phase0CFreezeReceipt
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase0c import Phase0CPrepareRequest, prepare_phase0c
from services.gates import PHASE_0C_CRITERIA, PHASE_0C_FIXTURES, GatePolicy, canonical_gate_bytes
from services.gates.verify_policy import PolicyVerificationError, verify_policy

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-0c-v2.json")
RECEIPT = ATTEMPT / "gate-cascade/receipts/phase-0c-v2.json"


def test_frozen_policy_is_canonical_and_bound_to_the_0b_parent() -> None:
    raw = POLICY.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.gate_id == "phase-0c"
    assert policy.gate_version == "v2"
    assert policy.criteria == PHASE_0C_CRITERIA
    assert len(policy.parent_gate_result_hashes) == 1
    assert (
        policy.parent_gate_result_hashes[0]
        == hashlib.sha256(
            (ATTEMPT / "phase-0b/gate-result.json").read_bytes()
        ).hexdigest()
    )
    assert policy.prerequisite_bindings == ()
    assert policy.capability_allowlist == ()
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None
    assert policy.golden_sha256 is not None


def test_freeze_receipt_matches_policy_bytes() -> None:
    receipt = Phase0CFreezeReceipt.model_validate_json(RECEIPT.read_bytes())

    assert receipt.todo == 26
    assert receipt.gate_id == "phase-0c"
    assert receipt.policy_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert len(receipt.fixture_manifests) == 5
    parent = json.loads((ATTEMPT / "phase-0b/gate-result.json").read_bytes())
    assert parent["gate_version"] == "v2"


def test_policy_verification_passes_with_frozen_artifacts() -> None:
    policy = verify_policy(
        POLICY, RECEIPT, None, ATTEMPT / "gate-cascade/execution-contract.canonical.json"
    )

    assert policy.gate_id == "phase-0c"


def test_post_result_policy_edit_is_rejected(tmp_path: Path) -> None:
    edited = tmp_path / "policy.json"
    payload = json.loads(POLICY.read_bytes())
    payload["gate_version"] = "v9"
    edited.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PolicyVerificationError, match="policy hash"):
        verify_policy(edited, RECEIPT, None, None)


def _policy_payload() -> dict[str, object]:
    return {
        "schema_version": "gate-policy-v1",
        "gate_id": "phase-0c",
        "gate_version": "v1",
        "parent_gate_result_hashes": ["a" * 64],
        "criteria": list(PHASE_0C_CRITERIA),
        "toolchain_lock_sha256": "b" * 64,
        "fixture_manifest_sha256": "c" * 64,
        "golden_sha256": "d" * 64,
        "prerequisite_bindings": [],
        "capability_allowlist": [],
    }


def test_criteria_drift_is_rejected() -> None:
    payload = _policy_payload()
    criteria = payload["criteria"]
    assert isinstance(criteria, list)
    payload["criteria"] = [*criteria, "phase-0c-extra-stop"]

    with pytest.raises(ValidationError, match="phase_0c_criteria"):
        GatePolicy.model_validate(payload)


def test_missing_parent_is_rejected() -> None:
    payload = _policy_payload()
    payload["parent_gate_result_hashes"] = []

    with pytest.raises(ValidationError, match="phase_0c_parent"):
        GatePolicy.model_validate(payload)


def test_capability_allowlist_is_rejected() -> None:
    payload = _policy_payload()
    payload["capability_allowlist"] = ["base_cut"]

    with pytest.raises(ValidationError, match="phase_0c_capabilities"):
        GatePolicy.model_validate(payload)


def test_observed_compiler_output_embedded_in_policy_is_rejected() -> None:
    payload = _policy_payload()
    payload["observed_plan_sha256"] = "e" * 64

    with pytest.raises(ValidationError, match="extra_forbidden"):
        GatePolicy.model_validate_json(json.dumps(payload))


def test_stale_parent_gate_result_is_rejected_at_prepare_time(tmp_path: Path) -> None:
    parent: dict[str, object] = json.loads((ATTEMPT / "phase-0b/gate-result.json").read_bytes())
    parent["passed"] = False
    criteria_results = parent["criteria_results"]
    assert isinstance(criteria_results, list)
    for result in criteria_results:
        assert isinstance(result, dict)
        result["passed"] = False
    stale = tmp_path / "stale-parent.json"
    stale.write_bytes(json.dumps(parent, sort_keys=True, separators=(",", ":")).encode())
    request = Phase0CPrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0c-v1.json"),
        fixture_ids=PHASE_0C_FIXTURES,
        parent_result=stale,
        pre_source_snapshot=ATTEMPT / "task-26-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=tmp_path / "policy.json",
        freeze_receipt=tmp_path / "receipt.json",
        staging=tmp_path / "prepared",
        intent=tmp_path / "intent.json",
    )

    with pytest.raises(PrepareError, match="did not pass"):
        prepare_phase0c(request)
