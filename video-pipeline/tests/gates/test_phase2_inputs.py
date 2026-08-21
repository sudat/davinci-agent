"""Todo-47 gate-input acceptance: the frozen phase-2 policy and its bindings.

The frozen policy must be canonical, carry exactly three parent gate result
hashes and ONE H1 operator-checkpoint prerequisite binding, freeze the five
canonical fixture manifests through the receipt, and verify through
``verify_policy``. Negative probes: a synthetic (fixture-marked) checkpoint
and a stale parent are refused at prepare time on copies.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.models import Phase2FreezeReceipt
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase2 import Phase2PrepareRequest, prepare_phase2
from services.gates import (
    PHASE_2_CAPABILITIES,
    PHASE_2_CRITERIA,
    PHASE_2_FIXTURES,
    GatePolicy,
    canonical_gate_bytes,
)
from services.gates.verify_policy import PolicyVerificationError, verify_policy
from services.toolchain.models import Phase2ToolchainLock, load_lock

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-2-v4.json")
RECEIPT = ATTEMPT / "gate-cascade/receipts/phase-2-v4.json"
LOCK = Path("config/toolchains/phase-2-v1.json")


def test_frozen_policy_is_canonical_with_three_parents_and_h1_prerequisite() -> None:
    raw = POLICY.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.gate_id == "phase-2"
    assert policy.gate_version == "v4"
    assert policy.criteria == PHASE_2_CRITERIA
    assert policy.capability_allowlist == PHASE_2_CAPABILITIES
    assert policy.parent_gate_result_hashes == (
        hashlib.sha256((ATTEMPT / "phase-0a/gate-result.json").read_bytes()).hexdigest(),
        hashlib.sha256((ATTEMPT / "phase-0b/gate-result.json").read_bytes()).hexdigest(),
        hashlib.sha256((ATTEMPT / "phase-1-technical/gate-result.json").read_bytes()).hexdigest(),
    )
    assert len(policy.prerequisite_bindings) == 1
    binding = policy.prerequisite_bindings[0]
    assert binding.kind == "operator_checkpoint"
    assert binding.purpose == "EDITORIAL_APPROVED"
    assert binding.episode_id == "real-01"
    assert binding.checkpoint_sha256 == hashlib.sha256(
        (ATTEMPT / "h1/checkpoint-result.json").read_bytes()
    ).hexdigest()
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None
    assert policy.golden_sha256 is not None


def test_freeze_receipt_binds_five_fixtures_three_parents_lock_and_checkpoint() -> None:
    receipt = Phase2FreezeReceipt.model_validate_json(RECEIPT.read_bytes())

    assert receipt.todo == 47
    assert receipt.gate_id == "phase-2"
    assert receipt.policy_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert [binding.fixture_id for binding in receipt.fixture_manifests] == list(PHASE_2_FIXTURES)
    for binding in receipt.fixture_manifests:
        assert hashlib.sha256(Path(binding.path).read_bytes()).hexdigest() == binding.sha256
    assert tuple(link.gate_id for link in receipt.parent_gate_results) == (
        "phase-0a",
        "phase-0b",
        "phase-1-technical",
    )
    assert receipt.toolchain_lock_sha256 == hashlib.sha256(LOCK.read_bytes()).hexdigest()
    assert receipt.execution_contract_sha256 == hashlib.sha256(
        (ATTEMPT / "gate-cascade/execution-contract.canonical.json").read_bytes()
    ).hexdigest()
    assert receipt.prerequisite_checkpoint.episode_id == "real-01"
    assert receipt.golden_hashes.index_sha256 == hashlib.sha256(
        Path("tests/goldens/reference/phase-2/index.json").read_bytes()
    ).hexdigest()


def test_policy_verification_passes_with_frozen_artifacts() -> None:
    policy = verify_policy(
        POLICY, RECEIPT, None, ATTEMPT / "gate-cascade/execution-contract.canonical.json"
    )

    assert policy.gate_id == "phase-2"


def test_lock_carries_passed_phase2_smokes_and_pinned_matrix() -> None:
    lock = load_lock(LOCK)

    assert isinstance(lock, Phase2ToolchainLock)
    assert lock.smoke.resolve_package.status == "passed"
    assert lock.smoke.render_qc.status == "passed"
    assert lock.resolve_package.capability_matrix_sha256 == hashlib.sha256(
        Path("capabilities/resolve-21.0.4/capability-matrix.json").read_bytes()
    ).hexdigest()
    assert lock.render_qc.completion.completion_value == 100
    assert lock.render_qc.completion.status_strings_parsed is False


def _policy_payload() -> dict[str, object]:
    return json.loads(POLICY.read_bytes())


def test_two_prerequisite_bindings_are_rejected() -> None:
    payload = _policy_payload()
    bindings = payload["prerequisite_bindings"]
    assert isinstance(bindings, list)
    payload["prerequisite_bindings"] = [*bindings, bindings[0]]

    with pytest.raises(ValidationError, match="phase_2_prerequisites"):
        GatePolicy.model_validate(payload)


def test_gate_result_prerequisite_kind_is_rejected() -> None:
    payload = _policy_payload()
    payload["prerequisite_bindings"] = [
        {"kind": "gate_result", "gate_id": "phase-1-technical", "result_sha256": "a" * 64}
    ]

    with pytest.raises(ValidationError, match="phase_2_prerequisites"):
        GatePolicy.model_validate(payload)


def test_two_parents_are_rejected() -> None:
    payload = _policy_payload()
    parents = payload["parent_gate_result_hashes"]
    assert isinstance(parents, list)
    payload["parent_gate_result_hashes"] = parents[:2]

    with pytest.raises(ValidationError, match="phase_2_parent"):
        GatePolicy.model_validate(payload)


def test_edited_policy_is_rejected_by_receipt_binding(tmp_path: Path) -> None:
    edited = tmp_path / "policy.json"
    payload = _policy_payload()
    payload["gate_version"] = "v9"
    edited.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PolicyVerificationError, match="policy hash"):
        verify_policy(edited, RECEIPT, None, None)


def _request(tmp_path: Path, **overrides: object) -> Phase2PrepareRequest:
    base: dict[str, object] = {
        "toolchain_lock": LOCK,
        "fixture_ids": PHASE_2_FIXTURES,
        "parent_results": (
            ATTEMPT / "phase-0a/gate-result.json",
            ATTEMPT / "phase-0b/gate-result.json",
            ATTEMPT / "phase-1-technical/gate-result.json",
        ),
        "prerequisite": ATTEMPT / "h1/checkpoint-result.json",
        "pre_source_snapshot": ATTEMPT / "task-47-pre-source.json",
        "execution_contract": ATTEMPT / "execution-contract.json",
        "policy_out": tmp_path / "policy.json",
        "freeze_receipt": tmp_path / "receipt.json",
        "staging": tmp_path / "prepared",
        "intent": tmp_path / "intent.json",
    }
    base.update(overrides)
    return Phase2PrepareRequest(**base)  # type: ignore[arg-type]


def test_synthetic_checkpoint_is_refused_at_prepare_time(tmp_path: Path) -> None:
    synthetic_dir = tmp_path / "h1"
    synthetic_dir.mkdir()
    payload = json.loads((ATTEMPT / "h1/checkpoint-result.json").read_bytes())
    payload["fixture_only"] = True
    synthetic = synthetic_dir / "checkpoint-result.json"
    synthetic.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    shutil.copy(ATTEMPT / "h1/display-receipt.json", synthetic_dir / "display-receipt.json")

    with pytest.raises(PrepareError, match="prerequisite rejected"):
        prepare_phase2(_request(tmp_path, prerequisite=synthetic))


def test_stale_parent_gate_result_is_refused_at_prepare_time(tmp_path: Path) -> None:
    parent: dict[str, object] = json.loads(
        (ATTEMPT / "phase-1-technical/gate-result.json").read_bytes()
    )
    parent["policy_sha256"] = "0" * 64
    stale = tmp_path / "stale-parent.json"
    stale.write_text(json.dumps(parent, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PrepareError, match="different phase-1-technical policy"):
        prepare_phase2(
            _request(
                tmp_path,
                parent_results=(
                    ATTEMPT / "phase-0a/gate-result.json",
                    ATTEMPT / "phase-0b/gate-result.json",
                    stale,
                ),
            )
        )


def test_same_version_refreeze_is_refused(tmp_path: Path) -> None:
    existing_policy = tmp_path / "policy.json"
    existing_policy.write_bytes(POLICY.read_bytes())

    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_phase2(_request(tmp_path))


def test_wrong_fixture_set_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PrepareError, match="exactly the five canonical fixture ids"):
        prepare_phase2(_request(tmp_path, fixture_ids=PHASE_2_FIXTURES[:4]))
