"""Correction, transient, and unsupported routes with decision invalidation."""

from __future__ import annotations

import pytest

from services.final_review.bundle import FinalReviewBundle, assemble_final_review_bundle
from services.final_review.ledger import FinalReviewLedger
from services.final_review.routes import (
    CycleArtifacts,
    RouteRefusal,
    RunnerRetryEnvelope,
    StructuredCorrection,
    TransientFailureInput,
    route_correction,
    route_transient,
    route_unsupported,
)
from services.manual_finalization.freeze import FreezeInputs
from services.manual_finalization.store import FreezeStore
from tests.final_review.support import (
    EPISODE,
    empty_declarations,
    fixture_record,
    make_records_store,
    passed_qc_report,
    sha,
)


def make_bundle() -> FinalReviewBundle:
    return assemble_final_review_bundle(
        episode_id=EPISODE,
        fixture_only=True,
        render_sha256=sha("final-render"),
        build_output_sha256=sha("build-output"),
        conformance_fingerprint=sha("conformance"),
        qc_report=passed_qc_report(),
        privacy_declarations=empty_declarations(),
        editorial_diff={
            "checkpoint_target_set_hash": sha("checkpoint"),
            "components": (),
        },
    )


class StubExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reenter(self, correction: StructuredCorrection) -> CycleArtifacts:
        self.calls.append(correction.instruction)
        return CycleArtifacts(
            plan_sha256=sha("plan-v2"),
            preview_sha256=sha("preview-v2"),
            version=2,
        )


def correction() -> StructuredCorrection:
    return StructuredCorrection.model_validate(
        {
            "instruction": "cut the first pause entirely",
            "classification": "remove_segment",
            "base_plan_sha256": sha("plan-v1"),
        }
    )


def test_correction_creates_new_cycle_and_supersedes_approval(tmp_path) -> None:
    bundle = make_bundle()
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    ledger.bind_bundle(bundle.target_set_hash)
    store = make_records_store(tmp_path)
    record = fixture_record(store, purpose="final", target_hash=bundle.target_set_hash)
    ledger.record_final_approval(record.record_id, bundle.target_set_hash)

    executor = StubExecutor()
    result = route_correction(
        ledger, correction=correction(), executor=executor, frozen=False
    )
    assert executor.calls == ["cut the first pause entirely"]
    assert result.artifacts.plan_sha256 == sha("plan-v2")
    assert result.artifacts.preview_sha256 == sha("preview-v2")
    assert result.superseded_approvals == (record.record_id,)
    assert ledger.active_final_approval() is None
    assert record.record_id in ledger.approval_invalidations()


def test_correction_refused_for_frozen_job(tmp_path) -> None:
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    ledger.bind_bundle(make_bundle().target_set_hash)
    with pytest.raises(RouteRefusal, match="job-frozen"):
        route_correction(
            ledger, correction=correction(), executor=StubExecutor(), frozen=True
        )


def test_transient_routes_to_runner_with_bounded_attempts() -> None:
    envelope = route_transient(
        TransientFailureInput.model_validate(
            {"failure_code": "timeout", "attempts_used": 1, "detail": "render poll"}
        )
    )
    assert isinstance(envelope, RunnerRetryEnvelope)
    assert envelope.failure_class == "transient"
    assert envelope.retry is True
    assert envelope.max_attempts == 3


def test_transient_never_mutates_review_state(tmp_path) -> None:
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    before = ledger.events()
    route_transient(
        TransientFailureInput.model_validate(
            {"failure_code": "resolve_disconnect", "attempts_used": 2}
        )
    )
    assert ledger.events() == before


def test_non_transient_code_refused_from_runner_route() -> None:
    with pytest.raises(RouteRefusal, match="not-transient"):
        route_transient(
            TransientFailureInput.model_validate(
                {"failure_code": "schema_invalid", "attempts_used": 1}
            )
        )


def test_exhausted_retry_budget_refused() -> None:
    with pytest.raises(RouteRefusal, match="retry-budget-exhausted"):
        route_transient(
            TransientFailureInput.model_validate(
                {"failure_code": "timeout", "attempts_used": 3}
            )
        )


def freeze_inputs(target: str) -> dict[str, object]:
    return {
        "episode_id": EPISODE,
        "fixture_only": True,
        "target_set_hash": target,
        "drt_drp": {
            "toolchain_lock_sha256": sha("toolchain-lock"),
            "input_hashes": ({"name": "edit-plan", "sha256": sha("plan-v1")},),
            "reproduction_commands": (
                "uv run python -m services.cli.checkpoint show --bundle b.json",
            ),
        },
        "render": {"name": "final-render", "sha256": sha("final-render")},
        "timeline_fingerprint": sha("timeline"),
        "conformance_fingerprint": sha("conformance"),
        "change_log": ({"entry": "manual trim of intro"},),
        "last_committed_plan": {"name": "edit-plan", "sha256": sha("plan-v1")},
        "reports": (
            {"kind": "rights", "sha256": sha("rights-report")},
            {"kind": "privacy", "sha256": sha("privacy-report")},
            {"kind": "qc", "sha256": sha("qc-report")},
        ),
    }


def route_to_freeze(tmp_path, *, with_record: bool):
    bundle = make_bundle()
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    ledger.bind_bundle(bundle.target_set_hash)
    store = make_records_store(tmp_path)
    if with_record:
        fixture_record(store, purpose="manual_freeze", target_hash=bundle.target_set_hash)
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    from services.approvals.chain_key import load_chain_key  # noqa: PLC0415

    chain_key = load_chain_key(store.records_path, create=True)

    result = route_unsupported(
        ledger,
        records=store.all_records(),
        inputs=FreezeInputs.model_validate(freeze_inputs(bundle.target_set_hash)),
        freeze_store=freeze_store,
        reason_detail="Fairlight fine-grained automation is not exposed",
        chain_key=chain_key,
        fixture_mode=True,
    )
    return result, freeze_store, ledger


def test_unsupported_routes_to_complete_freeze_package(tmp_path) -> None:
    result, freeze_store, ledger = route_to_freeze(tmp_path, with_record=True)
    assert result.package.automation_frozen is True
    assert result.package.reason.code == "unsupported_capability"
    assert freeze_store.is_frozen()
    assert freeze_store.frozen_state() is not None
    assert [event.event for event in ledger.events()][-1] == "freeze-routed"


def test_unsupported_without_freeze_record_refused(tmp_path) -> None:
    with pytest.raises(RouteRefusal, match="manual_freeze"):
        route_to_freeze(tmp_path, with_record=False)
