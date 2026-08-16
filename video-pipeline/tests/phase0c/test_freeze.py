from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest_phase0c import PHASE_0C_FIXTURE_IDS, Phase0CFixtureManifest
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase0c import Phase0CPrepareRequest, prepare_phase0c
from services.foundation_io import canonical_model_bytes
from services.gates import PHASE_0C_FIXTURES
from services.toolchain.models import Phase0BToolchainLock, Phase0CToolchainLock, load_lock

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0c")
PARENT_RESULT = ATTEMPT / "phase-0b/gate-result.json"


def _request(tmp_path: Path, **overrides: Path) -> Phase0CPrepareRequest:
    defaults: dict[str, Path] = {
        "toolchain_lock": Path("config/toolchains/phase-0c-v1.json"),
        "parent_result": PARENT_RESULT,
        "pre_source_snapshot": ATTEMPT / "task-26-pre-source.json",
        "execution_contract": ATTEMPT / "execution-contract.json",
        "policy_out": tmp_path / "policy.json",
        "freeze_receipt": tmp_path / "receipt.json",
        "staging": tmp_path / "prepared",
        "intent": tmp_path / "intent.json",
    }
    defaults.update(overrides)
    return Phase0CPrepareRequest(
        toolchain_lock=defaults["toolchain_lock"],
        fixture_ids=PHASE_0C_FIXTURES,
        parent_result=defaults["parent_result"],
        pre_source_snapshot=defaults["pre_source_snapshot"],
        execution_contract=defaults["execution_contract"],
        policy_out=defaults["policy_out"],
        freeze_receipt=defaults["freeze_receipt"],
        staging=defaults["staging"],
        intent=defaults["intent"],
    )


def test_all_manifests_are_canonical_and_declare_fixed_language() -> None:
    for fixture_id in PHASE_0C_FIXTURE_IDS:
        raw = (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        manifest = Phase0CFixtureManifest.model_validate_json(raw)
        assert manifest.fixture_id == fixture_id
        assert manifest.phase == "phase-0c"
        assert manifest.command.language == "ja"
        assert manifest.expectation_basis == "pre-registered-integer-frame-reasoning"
        assert raw == manifest.canonical_bytes()


def test_manifest_expected_outcomes_match_independent_golden() -> None:
    golden = json.loads(Path("tests/goldens/reference/phase-0c/expected.json").read_bytes())
    for fixture_id in PHASE_0C_FIXTURE_IDS:
        manifest = Phase0CFixtureManifest.model_validate_json(
            (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        )
        table = golden["fixtures"][fixture_id]
        assert manifest.expected.classification == table["classification"]
        assert manifest.expected.decision == table["decision"]
        assert manifest.expected.resulting_plan_version == table["resulting_plan_version"]
        assert list(manifest.expected.target_candidate_item_ids) == table[
            "target_candidate_item_ids"
        ]


def test_observed_result_field_is_rejected() -> None:
    raw = json.loads((MANIFEST_DIR / "p0c-remove-clear.json").read_bytes())
    raw["observed_compiler_plan_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="observed"):
        Phase0CFixtureManifest.model_validate(raw)


def test_observed_expectation_basis_is_rejected() -> None:
    raw = json.loads((MANIFEST_DIR / "p0c-remove-clear.json").read_bytes())
    raw["expectation_basis"] = "observed-from-compiler-run"
    with pytest.raises(ValidationError):
        Phase0CFixtureManifest.model_validate(raw)


def test_same_version_refreeze_is_rejected() -> None:
    request = Phase0CPrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0c-v1.json"),
        fixture_ids=PHASE_0C_FIXTURES,
        parent_result=PARENT_RESULT,
        pre_source_snapshot=ATTEMPT / "task-26-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=Path("config/gates/phase-0c-v1.json"),
        freeze_receipt=ATTEMPT / "task-26-freeze-receipt.json",
        staging=ATTEMPT / "freeze/26/prepared",
        intent=ATTEMPT / "freeze/26/intent.json",
    )
    with pytest.raises(PrepareError, match="re-freeze"):
        prepare_phase0c(request)


def test_tampered_parent_policy_binding_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(PARENT_RESULT.read_bytes())
    payload["policy_sha256"] = "f" * 64
    tampered = tmp_path / "tampered-parent.json"
    tampered.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(PrepareError, match="different phase-0b policy"):
        prepare_phase0c(_request(tmp_path, parent_result=tampered))


def test_incomplete_preview_smoke_is_rejected(tmp_path: Path) -> None:
    lock = load_lock(Path("config/toolchains/phase-0c-v1.json"))
    assert isinstance(lock, Phase0CToolchainLock)
    pending = lock.smoke.preview_review.model_copy(update={"status": "pending"})
    incomplete = lock.model_copy(
        update={"smoke": lock.smoke.model_copy(update={"preview_review": pending})}
    )
    lock_path = tmp_path / "pending-lock.json"
    lock_path.write_bytes(canonical_model_bytes(incomplete))
    with pytest.raises(PrepareError, match="incomplete"):
        prepare_phase0c(_request(tmp_path, toolchain_lock=lock_path))


def test_phase0b_parent_lock_is_rejected_for_phase0c(tmp_path: Path) -> None:
    stale_lock = Path("config/toolchains/phase-0b-v1.json")
    with pytest.raises(PrepareError, match="phase-0c toolchain lock"):
        prepare_phase0c(_request(tmp_path, toolchain_lock=stale_lock))


def test_frozen_0c_lock_inherits_0b_binaries_and_pins_no_model() -> None:
    parent = load_lock(Path("config/toolchains/phase-0b-v1.json"))
    child = load_lock(Path("config/toolchains/phase-0c-v1.json"))
    assert isinstance(parent, Phase0BToolchainLock)
    assert isinstance(child, Phase0CToolchainLock)

    assert child.python == parent.python
    assert child.ffmpeg == parent.ffmpeg
    assert child.resolve == parent.resolve
    assert child.normalization == parent.normalization
    assert child.smoke.resolve_readonly.status == "passed"
    assert child.smoke.ffmpeg_probe.status == "passed"
    assert child.smoke.ffmpeg_normalize.status == "passed"
    assert child.smoke.preview_review.status == "passed"
    assert child.preview_review.adapter == "pinned-ffmpeg-preview"
    assert child.preview_review.external_model == "none"
    assert child.preview_review.review_translator_policy_profile_id == (
        "phase-0c-deterministic-classifier-v1"
    )
