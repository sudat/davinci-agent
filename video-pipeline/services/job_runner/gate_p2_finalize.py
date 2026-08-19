"""Phase-2 gate finalize: QC policy/runs and the Final Review fixture seam.

QC follows the Todo-52 workflow exactly: the deterministic ``build-policy``
authoring step derives a canonical resolved policy from the clean fixture
render (a proposal tool — the engine still verifies), then ``run_qc``
recomputes every check from the render bytes. The Final Review side
assembles the sealed bundle from the real render/build/QC artifacts and
consumes ONLY fixture-marked operation records; the privacy fixture's
unresolved declaration must refuse the approve route behind the human
gate, and nothing here may auto-dismiss it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.final_review.bundle import assemble_final_review_bundle
from services.final_review.bundle_models import DiffComponent, EditorialDiff
from services.final_review.ledger import FinalReviewLedger
from services.final_review.routes import RouteRefusal, route_approve
from services.fixtures.manifest_phase2 import BlockingQcPrivacyFault, Phase2FixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.qc.policy_build import build_policy
from services.qc.privacy_gate import (
    DeclaredPrivacyIssue,
    PrivacyDeclarations,
)
from services.qc.run import run_qc

if TYPE_CHECKING:
    from services.qc.models import QcReport

GATE_ACTOR: Final = "phase2-gate"
POLICY_NAME: Final = "qc-policy.json"
REPORT_NAME: Final = "qc-report.json"
REPORT_REPEAT_NAME: Final = "qc-report-repeat.json"
DECLARATIONS_NAME: Final = "privacy-declarations.json"
IR_NAME: Final = "timeline-ir.json"


def fixture_privacy_declarations(manifest: Phase2FixtureManifest) -> PrivacyDeclarations:
    """The manifest-declared blocking privacy flag, as an operator declaration."""

    fault = manifest.fault
    if not isinstance(fault, BlockingQcPrivacyFault):
        raise TypeError("not a blocking-qc-privacy fixture")
    return PrivacyDeclarations(
        schema_version="privacy-declarations-v1",
        declared_issues=(
            DeclaredPrivacyIssue(
                issue_id=fault.privacy_flag,
                category="privacy",
                declared_by="local-operator",
                detail=(
                    f"fixture-declared blocking privacy flag on segment "
                    f"{fault.flagged_segment_id}: {fault.privacy_flag}"
                ),
                fixture_only=True,
            ),
        ),
    )


def qc_policy_for(render: Path, out_dir: Path) -> Path:
    policy = build_policy(render)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / POLICY_NAME
    atomic_write(target, canonical_model_bytes(policy))
    return target


def qc_run(  # noqa: PLR0913 (the QC run binding surface)
    render: Path,
    policy_path: Path,
    out_dir: Path,
    *,
    ir_path: Path | None = None,
    privacy: PrivacyDeclarations | None = None,
    out_name: str = REPORT_NAME,
) -> QcReport:
    privacy_path: Path | None = None
    if privacy is not None:
        privacy_path = out_dir / DECLARATIONS_NAME
        atomic_write(privacy_path, canonical_model_bytes(privacy))
    from services.qc.inputs import OptionalBindings  # noqa: PLC0415

    return run_qc(
        render,
        policy_path,
        out_dir / out_name,
        OptionalBindings(ir=ir_path, privacy=privacy_path),
    )


def editorial_diff(checkpoint_sha256: str, render_sha256: str) -> EditorialDiff:
    return EditorialDiff(
        checkpoint_target_set_hash=checkpoint_sha256,
        components=(
            DiffComponent(
                component="render",
                checkpoint_sha256=checkpoint_sha256,
                final_sha256=render_sha256,
            ),
        ),
    )


def finalize_clean(  # noqa: PLR0913 (one slot per sealed bundle input)
    *,
    episode_id: str,
    render_path: Path,
    build_output_sha256: str,
    conformance_fingerprint: str,
    qc_report: QcReport,
    checkpoint_sha256: str,
    out_dir: Path,
) -> dict[str, str]:
    """Seal the bundle and consume a MARKED FIXTURE Final record."""

    bundle = assemble_final_review_bundle(
        episode_id=episode_id,
        fixture_only=True,
        render_sha256=sha256_file(render_path),
        build_output_sha256=build_output_sha256,
        conformance_fingerprint=conformance_fingerprint,
        qc_report=qc_report,
        privacy_declarations=PrivacyDeclarations.empty(),
        editorial_diff=editorial_diff(checkpoint_sha256, sha256_file(render_path)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = FinalReviewLedger(out_dir / "final-review-events.jsonl")
    ledger.bind_bundle(bundle.target_set_hash)
    store = OperationRecordStore(out_dir / "operation-records.jsonl")
    record = store.append(
        record_fixture_operation(
            purpose="final",
            target_bundle_hash=bundle.target_set_hash,
            decision="approve",
            actor_id=GATE_ACTOR,
        )
    )
    approval = route_approve(
        ledger,
        records=store.all_records(),
        record=record,
        active_bundle=bundle,
        fixture_mode=True,
    )
    bundle_path = out_dir / "final-review-bundle.json"
    atomic_write(bundle_path, canonical_model_bytes(bundle))
    approval_path = out_dir / "approval.json"
    atomic_write(approval_path, canonical_model_bytes(approval))
    return {
        "bundle_path": str(bundle_path),
        "approval_path": str(approval_path),
        "records_path": str(store.records_path),
        "ledger_path": str(out_dir / "final-review-events.jsonl"),
        "record_fixture_only": str(record.fixture_only),
    }


def finalize_privacy_block(  # noqa: PLR0913 (one slot per sealed bundle input)
    *,
    episode_id: str,
    render_path: Path,
    build_output_sha256: str,
    conformance_fingerprint: str,
    qc_report: QcReport,
    declarations: PrivacyDeclarations,
    checkpoint_sha256: str,
    out_dir: Path,
) -> dict[str, str]:
    """Bundle the unresolved privacy declaration; approve must refuse."""

    bundle = assemble_final_review_bundle(
        episode_id=episode_id,
        fixture_only=True,
        render_sha256=sha256_file(render_path),
        build_output_sha256=build_output_sha256,
        conformance_fingerprint=conformance_fingerprint,
        qc_report=qc_report,
        privacy_declarations=declarations,
        editorial_diff=editorial_diff(checkpoint_sha256, sha256_file(render_path)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = FinalReviewLedger(out_dir / "final-review-events.jsonl")
    ledger.bind_bundle(bundle.target_set_hash)
    store = OperationRecordStore(out_dir / "operation-records.jsonl")
    record = store.append(
        record_fixture_operation(
            purpose="final",
            target_bundle_hash=bundle.target_set_hash,
            decision="approve",
            actor_id=GATE_ACTOR,
        )
    )
    refusal: dict[str, str] = {}
    try:
        route_approve(
            ledger,
            records=store.all_records(),
            record=record,
            active_bundle=bundle,
            fixture_mode=True,
        )
    except RouteRefusal as error:
        refusal = {"code": error.code, "detail": error.detail}
    bundle_path = out_dir / "final-review-bundle.json"
    atomic_write(bundle_path, canonical_model_bytes(bundle))
    refusal_path = out_dir / "approval-refusal.json"
    payload = json.dumps(refusal, sort_keys=True, separators=(",", ":")).encode()
    atomic_write(refusal_path, payload)
    return {
        "bundle_path": str(bundle_path),
        "approval_refusal_path": str(refusal_path),
        "refusal_code": refusal.get("code", ""),
        "record_fixture_only": str(record.fixture_only),
    }


__all__ = [
    "DECLARATIONS_NAME",
    "GATE_ACTOR",
    "IR_NAME",
    "POLICY_NAME",
    "REPORT_NAME",
    "REPORT_REPEAT_NAME",
    "editorial_diff",
    "finalize_clean",
    "finalize_privacy_block",
    "fixture_privacy_declarations",
    "qc_policy_for",
    "qc_run",
]
