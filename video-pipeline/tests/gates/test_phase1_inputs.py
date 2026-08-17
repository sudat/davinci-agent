from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.models import Phase1TechnicalFreezeReceipt
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase1_technical import (
    Phase1PrepareRequest,
    prepare_phase1_technical,
)
from services.gates import (
    PHASE_1_TECHNICAL_CRITERIA,
    PHASE_1_TECHNICAL_FIXTURES,
    GatePolicy,
    canonical_gate_bytes,
)
from services.gates.verify_policy import PolicyVerificationError, verify_policy
from services.toolchain.models import (
    LockError,
    Phase1TechnicalToolchainLock,
    load_lock,
)

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-1-technical-v1.json")
RECEIPT = ATTEMPT / "task-32-freeze-receipt.json"
LOCK = Path("config/toolchains/phase-1-technical-v1.json")


def test_frozen_policy_is_canonical_with_two_parents() -> None:
    raw = POLICY.read_bytes()
    policy = GatePolicy.model_validate_json(raw)

    assert raw == canonical_gate_bytes(policy)
    assert policy.gate_id == "phase-1-technical"
    assert policy.gate_version == "v1"
    assert policy.criteria == PHASE_1_TECHNICAL_CRITERIA
    assert policy.parent_gate_result_hashes == (
        hashlib.sha256((ATTEMPT / "phase-0c/gate-result.json").read_bytes()).hexdigest(),
        hashlib.sha256((ATTEMPT / "control-plane/gate-result.json").read_bytes()).hexdigest(),
    )
    assert policy.prerequisite_bindings == ()
    assert policy.capability_allowlist == ()
    assert policy.toolchain_lock_sha256 is not None
    assert policy.fixture_manifest_sha256 is not None
    assert policy.golden_sha256 is not None


def test_freeze_receipt_binds_five_fixtures_two_parents_and_lock() -> None:
    receipt = Phase1TechnicalFreezeReceipt.model_validate_json(RECEIPT.read_bytes())

    assert receipt.todo == 32
    assert receipt.gate_id == "phase-1-technical"
    assert receipt.policy_sha256 == hashlib.sha256(POLICY.read_bytes()).hexdigest()
    assert [binding.fixture_id for binding in receipt.fixture_manifests] == list(
        PHASE_1_TECHNICAL_FIXTURES
    )
    assert tuple(link.gate_id for link in receipt.parent_gate_results) == (
        "phase-0c",
        "control-plane-baseline",
    )
    assert receipt.toolchain_lock_sha256 == hashlib.sha256(LOCK.read_bytes()).hexdigest()
    assert receipt.execution_contract_sha256 == hashlib.sha256(
        (ATTEMPT / "execution-contract.json").read_bytes()
    ).hexdigest()


def test_policy_verification_passes_with_frozen_artifacts() -> None:
    policy = verify_policy(POLICY, RECEIPT, None, ATTEMPT / "execution-contract.json")

    assert policy.gate_id == "phase-1-technical"


def test_lock_carries_passed_whisper_and_editorial_model_smokes() -> None:
    lock = load_lock(LOCK)

    assert isinstance(lock, Phase1TechnicalToolchainLock)
    assert lock.smoke.whisper_ja.status == "passed"
    assert lock.smoke.editorial_model.status == "passed"
    assert lock.whisper_ja.model.sha256 == hashlib.sha256(
        Path(lock.whisper_ja.model.path).read_bytes()
    ).hexdigest()


def _policy_payload() -> dict[str, object]:
    return json.loads(POLICY.read_bytes())


def test_contract_out_kpi_criterion_is_rejected() -> None:
    payload = _policy_payload()
    criteria = payload["criteria"]
    assert isinstance(criteria, list)
    payload["criteria"] = [*criteria, "phase-1-kpi-30min-median"]

    with pytest.raises(ValidationError, match="phase_1_technical_criteria"):
        GatePolicy.model_validate(payload)


def test_missing_parent_is_rejected() -> None:
    payload = _policy_payload()
    parents = payload["parent_gate_result_hashes"]
    assert isinstance(parents, list)
    payload["parent_gate_result_hashes"] = [parents[0]]

    with pytest.raises(ValidationError, match="phase_1_technical_parent"):
        GatePolicy.model_validate(payload)


def test_three_parents_are_rejected() -> None:
    payload = _policy_payload()
    parents = payload["parent_gate_result_hashes"]
    assert isinstance(parents, list)
    payload["parent_gate_result_hashes"] = [*parents, "f" * 64]

    with pytest.raises(ValidationError, match="phase_1_technical_parent"):
        GatePolicy.model_validate(payload)


def test_edited_policy_is_rejected_by_receipt_binding(tmp_path: Path) -> None:
    edited = tmp_path / "policy.json"
    payload = _policy_payload()
    payload["gate_version"] = "v2"
    edited.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PolicyVerificationError, match="policy hash"):
        verify_policy(edited, RECEIPT, None, None)


def _request(tmp_path: Path, **overrides: object) -> Phase1PrepareRequest:
    base: dict[str, object] = {
        "toolchain_lock": LOCK,
        "fixture_ids": PHASE_1_TECHNICAL_FIXTURES,
        "parent_results": (
            ATTEMPT / "phase-0c/gate-result.json",
            ATTEMPT / "control-plane/gate-result.json",
        ),
        "pre_source_snapshot": ATTEMPT / "task-32-pre-source.json",
        "execution_contract": ATTEMPT / "execution-contract.json",
        "policy_out": tmp_path / "policy.json",
        "freeze_receipt": tmp_path / "receipt.json",
        "staging": tmp_path / "prepared",
        "intent": tmp_path / "intent.json",
    }
    base.update(overrides)
    return Phase1PrepareRequest(**base)  # type: ignore[arg-type]


def test_stale_parent_gate_result_is_rejected_at_prepare_time(tmp_path: Path) -> None:
    parent: dict[str, object] = json.loads(
        (ATTEMPT / "control-plane/gate-result.json").read_bytes()
    )
    parent["policy_sha256"] = "0" * 64
    stale = tmp_path / "stale-parent.json"
    stale.write_text(json.dumps(parent, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PrepareError, match="different control-plane-baseline policy"):
        prepare_phase1_technical(
            _request(tmp_path, parent_results=(ATTEMPT / "phase-0c/gate-result.json", stale))
        )


def test_failed_parent_gate_result_is_rejected(tmp_path: Path) -> None:
    parent: dict[str, object] = json.loads(
        (ATTEMPT / "phase-0c/gate-result.json").read_bytes()
    )
    parent["passed"] = False
    criteria_results = parent["criteria_results"]
    assert isinstance(criteria_results, list)
    for result in criteria_results:
        assert isinstance(result, dict)
        result["passed"] = False
    failed = tmp_path / "failed-parent.json"
    failed.write_text(json.dumps(parent, sort_keys=True, separators=(",", ":")))

    with pytest.raises(PrepareError, match="did not pass"):
        prepare_phase1_technical(
            _request(tmp_path, parent_results=(failed, ATTEMPT / "control-plane/gate-result.json"))
        )


def test_same_version_refreeze_is_rejected(tmp_path: Path) -> None:
    existing_policy = tmp_path / "policy.json"
    existing_policy.write_bytes(POLICY.read_bytes())

    with pytest.raises(PrepareError, match="same-version re-freeze is forbidden"):
        prepare_phase1_technical(_request(tmp_path))


def test_missing_tool_or_model_hash_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(LOCK.read_bytes())
    raw["whisper_ja"]["whisper_cli"]["sha256"] = "0" * 64
    broken = tmp_path / "broken-lock.json"
    broken.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))

    with pytest.raises(LockError):
        load_lock(broken)
    with pytest.raises(PrepareError, match="toolchain lock rejected"):
        prepare_phase1_technical(_request(tmp_path, toolchain_lock=broken))


def test_tampered_fixture_id_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_dir = Path("tests/fixtures/manifests/phase-1-technical")
    for fixture_id in PHASE_1_TECHNICAL_FIXTURES:
        (tmp_path / f"{fixture_id}.json").write_bytes(
            (manifest_dir / f"{fixture_id}.json").read_bytes()
        )
    payload = json.loads((tmp_path / "p1-ref-01-clean-ja.json").read_bytes())
    payload["fixture_id"] = "p1-ref-01-tampered"
    (tmp_path / "p1-ref-01-clean-ja.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )
    monkeypatch.setattr(
        "services.fixtures.prepare_phase1_technical.PHASE_1_MANIFEST_DIR", tmp_path
    )

    with pytest.raises(PrepareError, match="fixture manifest id drift"):
        prepare_phase1_technical(_request(tmp_path))
