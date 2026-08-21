"""Todo-56 gate-input acceptance: the frozen phase-3 policy and its bindings.

The frozen policy must be canonical, carry exactly one parent gate result
hash (the passed phase-2 v1 result), freeze no new capability allowlist or
prerequisite, and verify through ``verify_policy`` with the receipt binding
both brand fixture manifests. Negative probes: edited policy, stale parent,
same-version re-freeze, and a pre-existing Phase-3 product module are all
refused; probes run on COPIES and never touch frozen artifacts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.models import Phase3FreezeReceipt
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase3 import Phase3PrepareRequest, prepare_phase3
from services.gates import (
    PHASE_3_CRITERIA,
    PHASE_3_FIXTURES,
    GatePolicy,
    canonical_gate_bytes,
)
from services.gates.verify_policy import PolicyVerificationError, verify_policy
from services.toolchain.models import Phase3ToolchainLock, load_lock

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-3-v3.json")
RECEIPT = ATTEMPT / "gate-cascade/receipts/phase-3-v3.json"
LOCK = Path("config/toolchains/phase-3-v1.json")
PHASE2_RESULT = ATTEMPT / "phase-2/gate-result.json"


def test_frozen_policy_is_canonical_with_single_phase2_parent() -> None:
    raw = POLICY.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.gate_id == "phase-3"
    assert policy.gate_version == "v3"
    assert policy.criteria == PHASE_3_CRITERIA
    assert policy.capability_allowlist == ()
    assert policy.prerequisite_bindings == ()
    assert policy.parent_gate_result_hashes == (
        hashlib.sha256(PHASE2_RESULT.read_bytes()).hexdigest(),
    )
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None
    assert policy.golden_sha256 is not None


def test_freeze_receipt_binds_two_brand_fixtures_parent_lock_and_contract() -> None:
    receipt = Phase3FreezeReceipt.model_validate_json(RECEIPT.read_bytes())

    assert receipt.todo == 56
    assert receipt.gate_id == "phase-3"
    assert receipt.policy_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert [binding.fixture_id for binding in receipt.fixture_manifests] == list(PHASE_3_FIXTURES)
    for binding in receipt.fixture_manifests:
        assert hashlib.sha256(Path(binding.path).read_bytes()).hexdigest() == binding.sha256
    assert tuple(link.gate_id for link in receipt.parent_gate_results) == ("phase-2",)
    assert receipt.toolchain_lock_sha256 == hashlib.sha256(LOCK.read_bytes()).hexdigest()
    assert receipt.execution_contract_sha256 == hashlib.sha256(
        (ATTEMPT / "gate-cascade/execution-contract.canonical.json").read_bytes()
    ).hexdigest()
    assert receipt.golden_hashes.index_sha256 == hashlib.sha256(
        Path("tests/goldens/reference/phase-3/index.json").read_bytes()
    ).hexdigest()


def test_policy_verification_passes_with_frozen_artifacts() -> None:
    policy = verify_policy(
        POLICY, RECEIPT, None, ATTEMPT / "gate-cascade/execution-contract.canonical.json"
    )

    assert policy.gate_id == "phase-3"


def test_lock_carries_passed_presentation_assets_smoke_and_inherited_smokes() -> None:
    lock = load_lock(LOCK)

    assert isinstance(lock, Phase3ToolchainLock)
    assert lock.smoke.presentation_assets.status == "passed"
    assert lock.smoke.resolve_package.status == "passed"
    assert lock.smoke.render_qc.status == "passed"
    assert lock.presentation_assets.brand_package_ids == PHASE_3_FIXTURES
    assert lock.presentation_assets.third_party_material == "none"
    assert lock.presentation_assets.production_brand_claimed is False


def _policy_payload() -> dict[str, object]:
    return json.loads(POLICY.read_bytes())


def test_edited_policy_is_rejected_by_receipt_binding(tmp_path: Path) -> None:
    edited = tmp_path / "policy.json"
    payload = _policy_payload()
    payload["gate_version"] = "v9"
    edited.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PolicyVerificationError, match="policy hash"):
        verify_policy(edited, RECEIPT, None, None)


def test_second_parent_is_rejected() -> None:
    payload = _policy_payload()
    parents = payload["parent_gate_result_hashes"]
    assert isinstance(parents, list)
    payload["parent_gate_result_hashes"] = [*parents, parents[0]]

    with pytest.raises(ValidationError, match="phase_3_parent"):
        GatePolicy.model_validate(payload)


def test_capability_allowlist_is_rejected() -> None:
    payload = _policy_payload()
    payload["capability_allowlist"] = ["base_cut"]

    with pytest.raises(ValidationError, match="phase_3_capabilities"):
        GatePolicy.model_validate(payload)


def test_prerequisite_binding_is_rejected() -> None:
    payload = _policy_payload()
    payload["prerequisite_bindings"] = [
        {"kind": "gate_result", "gate_id": "phase-2", "result_sha256": "a" * 64}
    ]

    with pytest.raises(ValidationError, match="phase_3_prerequisites"):
        GatePolicy.model_validate(payload)


def _request(tmp_path: Path, **overrides: object) -> Phase3PrepareRequest:
    base: dict[str, object] = {
        "toolchain_lock": LOCK,
        "fixture_ids": PHASE_3_FIXTURES,
        "parent_result": PHASE2_RESULT,
        "pre_source_snapshot": ATTEMPT / "task-56-pre-source.json",
        "execution_contract": ATTEMPT / "execution-contract.json",
        "policy_out": tmp_path / "policy.json",
        "freeze_receipt": tmp_path / "receipt.json",
        "staging": tmp_path / "prepared",
        "intent": tmp_path / "intent.json",
    }
    base.update(overrides)
    return Phase3PrepareRequest(**base)  # type: ignore[arg-type]


def test_stale_parent_gate_result_is_refused_at_prepare_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase-3 product code legitimately exists since Todo 55; bypass only the
    # pre-implementation guard so this test still reaches the stale-parent refusal.
    monkeypatch.setattr(
        "services.fixtures.prepare_phase3.PHASE_3_PRODUCT_MODULE",
        Path("services/__absent_since_todo_55__"),
    )
    parent: dict[str, object] = json.loads(PHASE2_RESULT.read_bytes())
    parent["policy_sha256"] = "0" * 64
    stale = tmp_path / "stale-parent.json"
    stale.write_text(json.dumps(parent, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PrepareError, match="different phase-2 policy"):
        prepare_phase3(_request(tmp_path, parent_result=stale))


def test_same_version_refreeze_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.fixtures.prepare_phase3.PHASE_3_PRODUCT_MODULE",
        Path("services/__absent_since_todo_55__"),
    )
    existing_policy = tmp_path / "policy.json"
    existing_policy.write_bytes(POLICY.read_bytes())

    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_phase3(_request(tmp_path))


def test_pre_existing_phase3_product_module_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_module = tmp_path / "services" / "presentation"
    product_module.mkdir(parents=True)
    (product_module / "__init__.py").write_text("")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PrepareError, match="freeze must precede implementation"):
        prepare_phase3(_request(tmp_path))


def test_wrong_fixture_set_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PrepareError, match="exactly the two canonical brand fixture ids"):
        prepare_phase3(_request(tmp_path, fixture_ids=("p3-brand-a",)))
