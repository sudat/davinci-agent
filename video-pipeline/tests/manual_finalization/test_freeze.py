"""FreezePackage assembly: MANUAL_FREEZE-gated, complete-or-refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.approvals.chain_key import load_chain_key
from services.final_review.bundle import (
    PrivacyAlignmentError,
    assemble_final_review_bundle,
    verify_privacy_alignment,
)
from services.manual_finalization.freeze import (
    FreezeInputs,
    FreezePackage,
    FreezeRefusal,
    assemble_freeze_package,
)
from services.qc.privacy_gate import (
    PrivacyDeclarations,
    evaluate_privacy_gate,
    guard_no_automated_privacy_detectors,
)
from tests.final_review.support import (
    EPISODE,
    fixture_record,
    make_records_store,
    operator_record,
    passed_qc_report,
    privacy_blocked_qc_report,
    sha,
    unresolved_declarations,
)


def inputs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "episode_id": EPISODE,
        "fixture_only": True,
        "target_set_hash": sha("freeze-target"),
        "drt_drp": {
            "toolchain_lock_sha256": sha("toolchain-lock"),
            "input_hashes": ({"name": "edit-plan", "sha256": sha("plan-v1")},),
            "reproduction_commands": (
                "uv run python -m services.build.check --episode ep",
            ),
        },
        "render": {"name": "final-render", "sha256": sha("final-render")},
        "timeline_fingerprint": sha("timeline"),
        "conformance_fingerprint": sha("conformance"),
        "change_log": ({"entry": "manual intro trim"},),
        "last_committed_plan": {"name": "edit-plan", "sha256": sha("plan-v1")},
        "reports": (
            {"kind": "rights", "sha256": sha("rights-report")},
            {"kind": "privacy", "sha256": sha("privacy-report")},
            {"kind": "qc", "sha256": sha("qc-report")},
        ),
    }
    payload.update(overrides)
    return payload


def assemble(tmp_path, payload: dict[str, object], *, fixture_mode: bool = True):
    store = make_records_store(tmp_path)
    fixture_record(store, purpose="manual_freeze", target_hash=sha("freeze-target"))
    return assemble_freeze_package(
        records=store.all_records(),
        inputs=payload,
        reason_detail="unsupported",
        chain_key=load_chain_key(store.records_path, create=True),
        fixture_mode=fixture_mode,
    )


def test_complete_package_assembles_with_fixture_freeze_record(tmp_path) -> None:
    store = make_records_store(tmp_path)
    record = fixture_record(
        store, purpose="manual_freeze", target_hash=sha("freeze-target")
    )
    package = assemble_freeze_package(
        records=store.all_records(),
        inputs=FreezeInputs.model_validate(inputs()),
        reason_detail="Resolve cannot automate this transition",
        chain_key=load_chain_key(store.records_path, create=True),
        fixture_mode=True,
    )
    assert package.automation_frozen is True
    assert package.operator_record.record_id == record.record_id
    assert package.package_sha256 == package.computed_package_hash()


def test_missing_manual_freeze_record_refused(tmp_path) -> None:
    store = make_records_store(tmp_path)
    with pytest.raises(FreezeRefusal, match="manual_freeze"):
        assemble_freeze_package(
            records=store.all_records(),
            inputs=FreezeInputs.model_validate(inputs()),
            reason_detail="unsupported",
            chain_key=load_chain_key(store.records_path, create=True),
            fixture_mode=True,
        )


def test_wrong_purpose_record_cannot_authorize_freeze(tmp_path) -> None:
    store = make_records_store(tmp_path)
    fixture_record(store, purpose="final", target_hash=sha("freeze-target"))
    with pytest.raises(FreezeRefusal):
        assemble_freeze_package(
            records=store.all_records(),
            inputs=FreezeInputs.model_validate(inputs()),
            reason_detail="unsupported",
            chain_key=load_chain_key(store.records_path, create=True),
            fixture_mode=True,
        )


def test_fixture_freeze_record_presented_as_real_refused(tmp_path) -> None:
    store = make_records_store(tmp_path)
    fixture_record(store, purpose="manual_freeze", target_hash=sha("freeze-target"))
    with pytest.raises(FreezeRefusal, match="fixture"):
        assemble_freeze_package(
            records=store.all_records(),
            inputs=FreezeInputs.model_validate(inputs()),
            reason_detail="unsupported",
            chain_key=load_chain_key(store.records_path, create=True),
            fixture_mode=False,
        )


def test_real_operator_freeze_record_authorizes_production_freeze(tmp_path) -> None:
    store = make_records_store(tmp_path)
    operator_record(store, purpose="manual_freeze", target_hash=sha("freeze-target"))
    package = assemble_freeze_package(
        records=store.all_records(),
        inputs=FreezeInputs.model_validate(inputs(fixture_only=False)),
        reason_detail="manual finish",
        chain_key=load_chain_key(store.records_path, create=True),
        fixture_mode=False,
    )
    assert package.fixture_only is False
    assert package.operator_record.fixture_only is False


def test_incomplete_package_missing_drt_drp_refused(tmp_path) -> None:
    payload = inputs(
        drt_drp={
            "toolchain_lock_sha256": sha("toolchain-lock"),
            "input_hashes": (),
            "reproduction_commands": (),
        }
    )
    with pytest.raises(FreezeRefusal, match="incomplete-freeze-package"):
        assemble(tmp_path, payload)


def test_incomplete_package_missing_reports_refused(tmp_path) -> None:
    payload = inputs(
        reports=(
            {"kind": "rights", "sha256": sha("rights-report")},
            {"kind": "privacy", "sha256": sha("privacy-report")},
        )
    )
    with pytest.raises(FreezeRefusal, match="incomplete-freeze-package"):
        assemble(tmp_path, payload)


def test_incomplete_package_missing_change_log_refused(tmp_path) -> None:
    with pytest.raises(FreezeRefusal, match="incomplete-freeze-package"):
        assemble(tmp_path, inputs(change_log=()))


def test_package_tamper_refused_at_validation(tmp_path) -> None:
    package = assemble(tmp_path, inputs())
    payload = package.model_dump()
    payload["render"]["sha256"] = sha("evil-render")
    with pytest.raises(ValidationError, match="seal"):
        FreezePackage.model_validate(payload)


def test_todo53_modules_carry_no_automated_privacy_detectors() -> None:
    services_root = Path(__file__).resolve().parents[2] / "services"
    for package in ("final_review", "manual_finalization"):
        for source in sorted((services_root / package).glob("*.py")):
            guard_no_automated_privacy_detectors(source.read_text(), str(source))


def test_auto_dismissed_privacy_cannot_reach_freeze_package() -> None:
    issues, gates = evaluate_privacy_gate(
        unresolved_declarations(), "qc-thresholds-test-v1", (sha("render"),)
    )
    assert gates
    assert issues
    report = privacy_blocked_qc_report()
    assert report.verdict == "blocked"
    assert report.unresolved_human_gates
    bundle = assemble_final_review_bundle(
        episode_id=EPISODE,
        fixture_only=True,
        render_sha256=sha("final-render"),
        build_output_sha256=sha("build-output"),
        conformance_fingerprint=sha("conformance"),
        qc_report=report,
        privacy_declarations=unresolved_declarations(),
        editorial_diff={"checkpoint_target_set_hash": sha("cp"), "components": ()},
    )
    with pytest.raises(PrivacyAlignmentError, match="dismiss"):
        verify_privacy_alignment(bundle, PrivacyDeclarations.empty())
    passed = passed_qc_report()
    assert passed.verdict == "passed"
