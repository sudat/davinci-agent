"""Final-review decision routes: correction, transient, unsupported, approve.

Every route is a typed decision boundary over the sealed bundle and the
append-only ledger. ``correction`` re-enters the Todo-45 propose/apply
machinery through an injected executor, binds a new review cycle, and
supersedes prior FINAL approvals (recorded, never deleted). ``transient``
returns a bounded Runner retry envelope and touches NO review state — the
review side never retries by itself. ``unsupported`` routes to the Manual
Finalization Freeze Package, which itself requires a MANUAL_FREEZE
purpose record. ``approve`` accepts ONLY a purpose/target-valid FINAL
operation record against the bundle's displayed target-set hash;
automation is refused in production mode and every refusal is typed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.approvals.verify import evaluate_authorization, validate_supersession_chain
from services.final_review.routes_models import (
    MAX_TRANSIENT_ATTEMPTS,
    CorrectionCycleResult,
    CorrectionExecutor,
    CycleArtifacts,
    FinalApprovalResult,
    RunnerRetryEnvelope,
    StructuredCorrection,
    TransientFailureInput,
    UnsupportedRouteResult,
)
from services.job_runner.stage_classify import classify_error
from services.manual_finalization.freeze import (
    FreezeInputs,
    FreezeRefusal,
    assemble_freeze_package,
)

if TYPE_CHECKING:
    from services.approvals.models import ChainedOperationRecord
    from services.final_review.bundle import FinalReviewBundle
    from services.final_review.ledger import FinalReviewLedger
    from services.manual_finalization.store import FreezeStore

__all__ = [
    "MAX_TRANSIENT_ATTEMPTS",
    "CorrectionCycleResult",
    "CorrectionExecutor",
    "CycleArtifacts",
    "FinalApprovalResult",
    "RouteRefusal",
    "RunnerRetryEnvelope",
    "StructuredCorrection",
    "TransientFailureInput",
    "UnsupportedRouteResult",
    "route_approve",
    "route_correction",
    "route_transient",
    "route_unsupported",
]


class RouteRefusal(Exception):  # noqa: N818 (typed-refusal vocabulary, not an error kind)
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def route_transient(failure: TransientFailureInput) -> RunnerRetryEnvelope:
    """Hand a transient failure to the Runner; the review side never retries."""

    if classify_error(failure.failure_code) != "transient":
        raise RouteRefusal(
            "not-transient",
            f"failure code {failure.failure_code} is not in the Runner transient "
            "taxonomy; route it to correction, unsupported, or refusal",
        )
    if failure.attempts_used >= MAX_TRANSIENT_ATTEMPTS:
        raise RouteRefusal(
            "retry-budget-exhausted",
            f"transient failure {failure.failure_code} already used "
            f"{failure.attempts_used} of {MAX_TRANSIENT_ATTEMPTS} bounded attempts",
        )
    return RunnerRetryEnvelope(
        failure_code=failure.failure_code,
        failure_class="transient",
        attempts_used=failure.attempts_used,
        max_attempts=MAX_TRANSIENT_ATTEMPTS,
        retry=True,
    )


def route_correction(
    ledger: FinalReviewLedger,
    *,
    correction: StructuredCorrection,
    executor: CorrectionExecutor,
    frozen: bool,
) -> CorrectionCycleResult:
    if frozen:
        raise RouteRefusal(
            "job-frozen",
            "a frozen job cannot re-enter the correction loop (PRD 7.4)",
        )
    artifacts = executor.reenter(correction)
    event = ledger.record_correction_cycle(
        correction.instruction, ledger.active_bundle_hash()
    )
    superseded = ledger.invalidate_approvals(
        code="correction-cycle",
        detail=f"correction cycle {event.event_seq} re-binds the plan/preview",
    )
    return CorrectionCycleResult(
        event_seq=event.event_seq,
        artifacts=artifacts,
        superseded_approvals=superseded,
    )


def _gate_bundle_state(
    ledger: FinalReviewLedger, active_bundle: FinalReviewBundle
) -> None:
    if not active_bundle.verify_seals():
        raise RouteRefusal("bundle-seal-invalid", "the bundle seals do not verify")
    if any(entry.resolution_state == "unresolved" for entry in active_bundle.privacy_rights):
        raise RouteRefusal(
            "privacy-unresolved",
            "the bundle carries unresolved privacy/rights declarations; an "
            "operator resolution is required before FINAL_APPROVED",
        )
    if active_bundle.qc_verdict != "passed":
        raise RouteRefusal(
            "qc-blocked",
            f"the bound QC report verdict is {active_bundle.qc_verdict}; a "
            "blocked or unresolved-critical bundle cannot route to approve",
        )
    if ledger.active_bundle_hash() != active_bundle.target_set_hash:
        raise RouteRefusal(
            "stale-target-hash",
            "the ledger no longer displays this bundle's target set",
        )


def _gate_record_binding(
    record: ChainedOperationRecord,
    active_bundle: FinalReviewBundle,
    *,
    fixture_mode: bool,
) -> None:
    if record.purpose != "final" or record.target_type != "final-render":
        raise RouteRefusal(
            "purpose-target-mismatch",
            f"a {record.purpose}/{record.target_type} record can never authorize "
            "FINAL_APPROVED",
        )
    if record.target_hash != active_bundle.target_set_hash:
        raise RouteRefusal(
            "target-hash-mismatch",
            "the record binds a different target-set hash than the displayed one",
        )
    if not fixture_mode:
        if record.runner_class == "automation":
            raise RouteRefusal(
                "automation-refused",
                "an automation/AI runner can never record FINAL_APPROVED",
            )
        if record.fixture_only:
            raise RouteRefusal(
                "fixture-record",
                "a fixture-marked record can never satisfy a production approval",
            )


def route_approve(  # noqa: PLR0913 (route contract fixed by the Todo-45 dispatch surface)
    ledger: FinalReviewLedger,
    *,
    records: tuple[ChainedOperationRecord, ...],
    record: ChainedOperationRecord,
    active_bundle: FinalReviewBundle,
    fixture_mode: bool,
    chain_key: bytes,
) -> FinalApprovalResult:
    _gate_bundle_state(ledger, active_bundle)
    _gate_record_binding(record, active_bundle, fixture_mode=fixture_mode)
    try:
        validate_supersession_chain(records, chain_key=chain_key)
    except ValueError as error:
        raise RouteRefusal("records-chain-invalid", str(error)) from error
    verdict = evaluate_authorization(
        records,
        purpose="final",
        target_hash=active_bundle.target_set_hash,
        target_type="final-render",
        operator_gate=not fixture_mode,
    )
    if not verdict.authorized:
        raise RouteRefusal(
            verdict.refusal_code or "unauthorized",
            f"the FINAL record does not authorize this target ({verdict.refusal_code})",
        )
    event = ledger.record_final_approval(record.record_id, active_bundle.target_set_hash)
    return FinalApprovalResult(
        record_id=record.record_id,
        target_set_hash=active_bundle.target_set_hash,
        event_seq=event.event_seq,
    )


def route_unsupported(  # noqa: PLR0913 (route contract mirrors the Todo-45 dispatch surface)
    ledger: FinalReviewLedger,
    *,
    records: tuple[ChainedOperationRecord, ...],
    inputs: FreezeInputs,
    freeze_store: FreezeStore,
    reason_detail: str,
    fixture_mode: bool,
) -> UnsupportedRouteResult:
    active = ledger.active_bundle_hash()
    if active is not None and active != inputs.target_set_hash:
        raise RouteRefusal(
            "target-mismatch",
            "the freeze target must match the ledger's displayed bundle",
        )
    try:
        package = assemble_freeze_package(
            records=records,
            inputs=inputs,
            reason_detail=reason_detail,
            fixture_mode=fixture_mode,
            reason_code="unsupported_capability",
        )
    except FreezeRefusal as error:
        raise RouteRefusal(error.code, error.detail) from error
    published = freeze_store.publish(package)
    ledger.record_freeze_routed(package.package_sha256, package.target_set_hash)
    return UnsupportedRouteResult(package=published.package, version=published.version)
