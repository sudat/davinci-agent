from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.models import ControlPlaneFreezeReceipt
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_control_plane import (
    ControlPlanePrepareRequest,
    prepare_control_plane,
)
from services.gates import (
    CONTROL_PLANE_FIXTURES,
    PHASE_1_CONTROL_PLANE_CRITERIA,
    GatePolicy,
    canonical_gate_bytes,
)
from services.gates.verify_policy import PolicyVerificationError, verify_policy

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-1-control-plane-v2.json")
RECEIPT = ATTEMPT / "gate-cascade/receipts/control-plane-v2.json"
PARENT_RESULT = ATTEMPT / "phase-0c/gate-result.json"


def test_frozen_policy_is_canonical_and_bound_to_the_0c_parent() -> None:
    raw = POLICY.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.gate_id == "control-plane-baseline"
    assert policy.gate_version == "v2"
    assert policy.criteria == PHASE_1_CONTROL_PLANE_CRITERIA
    assert len(policy.parent_gate_result_hashes) == 1
    assert policy.parent_gate_result_hashes[0] == hashlib.sha256(
        PARENT_RESULT.read_bytes()
    ).hexdigest()
    assert policy.prerequisite_bindings == ()
    assert policy.capability_allowlist == ()
    assert policy.golden_sha256 is None
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None


def test_freeze_receipt_binds_the_six_control_plane_fixtures() -> None:
    receipt = ControlPlaneFreezeReceipt.model_validate_json(RECEIPT.read_bytes())

    assert receipt.todo == 7
    assert receipt.gate_id == "control-plane-baseline"
    assert receipt.policy_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert tuple(binding.fixture_id for binding in receipt.fixture_manifests) == (
        CONTROL_PLANE_FIXTURES
    )
    combined = hashlib.sha256()
    for binding in receipt.fixture_manifests:
        manifest_bytes = Path(binding.path).read_bytes()
        assert hashlib.sha256(manifest_bytes).hexdigest() == binding.sha256
        combined.update(manifest_bytes)
    assert combined.hexdigest() == receipt.fixture_manifests_combined_sha256
    assert receipt.fixture_manifests_combined_sha256 == (
        GatePolicy.model_validate_json(POLICY.read_bytes()).fixture_manifest_sha256
    )
    assert receipt.parent_gate_result_sha256 == hashlib.sha256(
        PARENT_RESULT.read_bytes()
    ).hexdigest()
    parent = json.loads(PARENT_RESULT.read_bytes())
    assert parent["passed"] is True


def test_policy_verification_passes_with_frozen_artifacts() -> None:
    policy = verify_policy(
        POLICY, RECEIPT, None, ATTEMPT / "gate-cascade/execution-contract.canonical.json"
    )

    assert policy.gate_id == "control-plane-baseline"


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
        "gate_id": "control-plane-baseline",
        "gate_version": "v1",
        "parent_gate_result_hashes": ["a" * 64],
        "criteria": list(PHASE_1_CONTROL_PLANE_CRITERIA),
        "toolchain_lock_sha256": "b" * 64,
        "fixture_manifest_sha256": "c" * 64,
        "golden_sha256": None,
        "prerequisite_bindings": [],
        "capability_allowlist": [],
    }


def test_criteria_drift_is_rejected() -> None:
    payload = _policy_payload()
    criteria = payload["criteria"]
    assert isinstance(criteria, list)
    payload["criteria"] = [*criteria, "phase-1-cp-extra"]

    with pytest.raises(ValidationError, match="phase_1_control_plane_criteria"):
        GatePolicy.model_validate(payload)


def test_missing_parent_is_rejected() -> None:
    payload = _policy_payload()
    payload["parent_gate_result_hashes"] = []

    with pytest.raises(ValidationError, match="phase_1_control_plane_parent"):
        GatePolicy.model_validate(payload)


def test_prerequisite_bindings_are_rejected() -> None:
    payload = _policy_payload()
    payload["prerequisite_bindings"] = [
        {
            "kind": "gate_result",
            "gate_id": "phase-0c",
            "result_sha256": "d" * 64,
        }
    ]

    with pytest.raises(ValidationError, match="phase_1_control_plane_prerequisites"):
        GatePolicy.model_validate(payload)


def test_golden_binding_is_rejected() -> None:
    payload = _policy_payload()
    payload["golden_sha256"] = "e" * 64

    with pytest.raises(ValidationError, match="phase_1_control_plane_golden"):
        GatePolicy.model_validate(payload)


def _prepare_request(
    tmp_path: Path, *, policy_out: Path | None = None
) -> ControlPlanePrepareRequest:
    return ControlPlanePrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0c-v1.json"),
        fixture_ids=CONTROL_PLANE_FIXTURES,
        parent_result=PARENT_RESULT,
        pre_source_snapshot=ATTEMPT / "task-7-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=policy_out if policy_out is not None else Path("config/gates/never.json"),
        freeze_receipt=tmp_path / "receipt.json",
        staging=tmp_path / "prepared",
        intent=tmp_path / "intent.json",
    )


def test_same_version_refreeze_is_forbidden(tmp_path: Path) -> None:
    request = _prepare_request(tmp_path, policy_out=POLICY)

    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_control_plane(request)


def test_non_canonical_fixture_set_is_rejected(tmp_path: Path) -> None:
    request = ControlPlanePrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0c-v1.json"),
        fixture_ids=CONTROL_PLANE_FIXTURES[:5],
        parent_result=PARENT_RESULT,
        pre_source_snapshot=ATTEMPT / "task-7-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=tmp_path / "policy.json",
        freeze_receipt=tmp_path / "receipt.json",
        staging=tmp_path / "prepared",
        intent=tmp_path / "intent.json",
    )

    with pytest.raises(PrepareError, match="exactly the six canonical fixture ids"):
        prepare_control_plane(request)


def test_stale_parent_gate_result_is_rejected_at_prepare_time(tmp_path: Path) -> None:
    parent: dict[str, object] = json.loads(PARENT_RESULT.read_bytes())
    parent["passed"] = False
    criteria_results = parent["criteria_results"]
    assert isinstance(criteria_results, list)
    for result in criteria_results:
        assert isinstance(result, dict)
        result["passed"] = False
    stale = tmp_path / "stale-parent.json"
    stale.write_bytes(json.dumps(parent, sort_keys=True, separators=(",", ":")).encode())
    request = replace(
        _prepare_request(tmp_path, policy_out=tmp_path / "policy.json"), parent_result=stale
    )

    with pytest.raises(PrepareError, match="did not pass"):
        prepare_control_plane(request)
