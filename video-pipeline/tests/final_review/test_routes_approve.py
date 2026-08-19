"""Final-review approve route: TTY-gated FINAL_APPROVED, typed refusals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from services.final_review.bundle import FinalReviewBundle, assemble_final_review_bundle
from services.final_review.ledger import FinalReviewLedger
from services.final_review.routes import RouteRefusal, route_approve

if TYPE_CHECKING:
    from services.qc.models import QcReport
    from services.qc.privacy_gate import PrivacyDeclarations

from tests.final_review.support import (
    EPISODE,
    critical_blocked_qc_report,
    empty_declarations,
    fixture_record,
    make_records_store,
    operator_record,
    passed_qc_report,
    privacy_blocked_qc_report,
    resolved_declarations,
    sha,
    unresolved_declarations,
)


def make_bundle(
    *,
    render_sha256: str = sha("final-render"),
    qc_report: QcReport | None = None,
    privacy_declarations: PrivacyDeclarations | None = None,
) -> FinalReviewBundle:
    return assemble_final_review_bundle(
        episode_id=EPISODE,
        fixture_only=True,
        render_sha256=render_sha256,
        build_output_sha256=sha("build-output"),
        conformance_fingerprint=sha("conformance"),
        qc_report=passed_qc_report() if qc_report is None else qc_report,
        privacy_declarations=(
            empty_declarations() if privacy_declarations is None else privacy_declarations
        ),
        editorial_diff={
            "checkpoint_target_set_hash": sha("checkpoint"),
            "components": (),
        },
    )


def rig(tmp_path, bundle) -> tuple:
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    ledger.bind_bundle(bundle.target_set_hash)
    store = make_records_store(tmp_path)
    return ledger, store


def approve(ledger, store, record, bundle, *, fixture_mode: bool):
    return route_approve(
        ledger,
        records=store.all_records(),
        record=record,
        active_bundle=bundle,
        fixture_mode=fixture_mode,
    )


def test_fixture_final_approval_records_against_displayed_target(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    result = approve(ledger, store, record, bundle, fixture_mode=True)
    assert result.record_id == record.record_id
    assert result.target_set_hash == bundle.target_set_hash
    approval = ledger.active_final_approval()
    assert approval is not None
    assert approval.record_id == record.record_id


def test_real_operator_record_approves_in_production_mode(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = operator_record(store, purpose="final", target_hash=bundle.target_set_hash)
    result = approve(ledger, store, record, bundle, fixture_mode=False)
    assert result.record_id == record.record_id


def test_automation_approval_refused_in_production_mode(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    with pytest.raises(RouteRefusal, match="automation-refused"):
        approve(ledger, store, record, bundle, fixture_mode=False)
    assert ledger.active_final_approval() is None


def test_fixture_record_presented_as_real_refused(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(
        store,
        purpose="final",
        target_hash=bundle.target_set_hash,
        runner_class="operator",
    )
    with pytest.raises(RouteRefusal, match="fixture-record"):
        approve(ledger, store, record, bundle, fixture_mode=False)


def test_wrong_purpose_record_cannot_authorize_final(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="editorial", target_hash=bundle.target_set_hash)
    with pytest.raises(RouteRefusal, match="purpose"):
        approve(ledger, store, record, bundle, fixture_mode=True)


def test_wrong_target_hash_refused(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=sha("other-target"))
    with pytest.raises(RouteRefusal, match="target"):
        approve(ledger, store, record, bundle, fixture_mode=True)


def test_unresolved_critical_qc_blocks_approve(tmp_path) -> None:
    bundle = make_bundle(qc_report=critical_blocked_qc_report())
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    with pytest.raises(RouteRefusal, match="qc-blocked"):
        approve(ledger, store, record, bundle, fixture_mode=True)
    assert ledger.active_final_approval() is None


def test_unresolved_privacy_blocks_approve(tmp_path) -> None:
    bundle = make_bundle(
        qc_report=privacy_blocked_qc_report(),
        privacy_declarations=unresolved_declarations(),
    )
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    with pytest.raises(RouteRefusal, match="privacy-unresolved"):
        approve(ledger, store, record, bundle, fixture_mode=True)


def test_resolved_privacy_does_not_block_approve(tmp_path) -> None:
    bundle = make_bundle(privacy_declarations=resolved_declarations())
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    result = approve(ledger, store, record, bundle, fixture_mode=True)
    assert result.target_set_hash == bundle.target_set_hash


def test_stale_bundle_target_refused_after_bundle_change(tmp_path) -> None:
    bundle = make_bundle()
    ledger, store = rig(tmp_path, bundle)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    rebuilt = make_bundle(render_sha256=sha("final-render-v2"))
    ledger.bind_bundle(rebuilt.target_set_hash)
    with pytest.raises(RouteRefusal, match="target"):
        approve(ledger, store, record, rebuilt, fixture_mode=True)
