"""The ``review apply`` engine: validated single-event commit + regeneration.

Apply accepts ONLY a validated unambiguous proposal whose ``base_plan_hash``
equals the bundle's displayed current hash; stale bases are typed rejections.
Ambiguous/conflict proposals never mutate the plan (Todo-30 defer events
only). A clear apply commits ONE immutable event, recompiles the IR,
regenerates the preview + trace, advances the displayed base, and writes
event-derived correction metrics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.cli.bundle import (
    BundleDriftError,
    ReviewBundle,
    load_bundle,
    save_bundle,
)
from services.cli.preview_render import render_review_preview
from services.cli.project import plan_sha256
from services.cli.review_common import (
    load_tools,
    mezzanine_for,
    previous_trace,
    store_ir,
    store_plan,
    write_metrics,
)
from services.cli.review_replay import ProposalOutcome
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.models import AppliedDecision, PreviewError
from services.review_command.commit import CommitOutcome, commit_command
from services.review_command.models import parse_proposal
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    load_head,
)
from services.review_command.validate import (
    ProposalValidationError,
    validate_proposal,
)

RESULT_NAME = "apply-result.json"


class ApplyResult(StrictModel):
    schema_version: Literal["review-apply-result-v1"]
    episode_id: str
    applied: bool
    idempotent: bool
    reason_code: str | None = None
    reason_detail: str | None = None
    classification: str | None = None
    version: int | None = None
    event_id: Sha256 | None = None
    plan_sha256: Sha256 | None = None
    ir_sha256: Sha256 | None = None
    preview_sha256: Sha256 | None = None
    trace_sha256: Sha256 | None = None


class ApplyError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _refusal(bundle: ReviewBundle, code: str, detail: str) -> ApplyResult:
    return ApplyResult(
        schema_version="review-apply-result-v1",
        episode_id=bundle.episode_id,
        applied=False,
        idempotent=False,
        reason_code=code,
        reason_detail=detail,
    )


def _result_path(out_dir: Path) -> Path:
    return out_dir / RESULT_NAME


def apply_proposal(
    proposal_file: Path, bundle_file: Path, out_dir: Path
) -> tuple[ApplyResult, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        outcome = ProposalOutcome.model_validate_json(proposal_file.read_bytes())
        bundle = load_bundle(bundle_file)
        head = load_head(
            bundle_file.parent / bundle.events_log, bundle_file.parent / bundle.store_dir
        )
    except (OSError, ValidationError, BundleDriftError, ReviewCommitError) as error:
        raise ApplyError("inputs_unreadable", str(error)) from error
    head_hash = plan_sha256(head.plan)
    proposal = None
    if outcome.status == "proposal" and outcome.proposal_json is not None:
        proposal = parse_proposal(outcome.proposal_json)
    already_recorded = proposal is not None and any(
        event.proposal_sha256 == outcome.proposal_sha256 for event in head.events
    )
    if outcome.status != "proposal" or proposal is None:
        result = _refusal(
            bundle, "schema_gap", f"proposal translate failed: {outcome.error_code}"
        )
        return _finish(result, bundle_file, bundle, out_dir), 1
    if not already_recorded and (
        outcome.base_plan_hash != bundle.current.plan_sha256
        or outcome.base_plan_hash != head_hash
    ):
        result = _refusal(
            bundle,
            "stale_base",
            f"proposal base {str(outcome.base_plan_hash)[:12]} is not the displayed "
            f"current hash {bundle.current.plan_sha256[:12]}/store head {head_hash[:12]}",
        )
        return _finish(result, bundle_file, bundle, out_dir), 1
    try:
        validation = validate_proposal(head.plan, proposal)
    except ProposalValidationError as error:
        if already_recorded:
            validation = None
        else:
            result = _refusal(bundle, error.code, error.detail)
            return _finish(result, bundle_file, bundle, out_dir), 1
    decision = OperatorDecision0C(
        decision_id=f"decision-{proposal.proposal_id}",
        actor_intent="operator",
        note="phase1 review apply; operator-role fixture-marked record",
    )
    try:
        commit = commit_command(
            proposal, decision, bundle_file.parent / bundle.events_log,
            bundle_file.parent / bundle.store_dir,
        )
    except (ReviewCommitError, ValueError, ValidationError) as error:
        result = _refusal(bundle, "apply_refused", str(error))
        return _finish(result, bundle_file, bundle, out_dir), 1
    if commit.deferred:
        result = ApplyResult(
            schema_version="review-apply-result-v1",
            episode_id=bundle.episode_id,
            applied=False,
            idempotent=commit.idempotent,
            reason_code=commit.reason,
            classification=(
                validation.classification if validation is not None else "unclassified"
            ),
            version=head.version,
            event_id=commit.event_id,
            plan_sha256=bundle.current.plan_sha256,
            ir_sha256=bundle.current.ir_sha256,
        )
        return _finish(result, bundle_file, bundle, out_dir), 0
    return _applied(outcome, bundle_file, bundle, commit, out_dir)


def _applied(
    outcome: ProposalOutcome,
    bundle_file: Path,
    bundle: ReviewBundle,
    commit: CommitOutcome,
    out_dir: Path,
) -> tuple[ApplyResult, int]:
    mezzanine = mezzanine_for(bundle_file, bundle)
    store_dir = bundle_file.parent / bundle.store_dir
    plan_path = store_dir / f"plan-v{commit.version}.json"
    ir_path = store_dir / f"ir-v{commit.version}.json"
    preview_dir = bundle_file.parent / f"preview-v{commit.version}"
    preview_name = preview_dir / "preview.mp4"
    if commit.idempotent and preview_name.is_file():
        preview_sha = sha256_file(preview_name)
        trace_sha = sha256_file(preview_dir / "preview-trace.json")
    else:
        ir = store_ir(ir_path)
        plan_model = store_plan(plan_path)
        decision = AppliedDecision(
            decision_id=f"decision-prop-{bundle.episode_id}-{outcome.command_index}",
            case_id=bundle.episode_id,
            classification="clear",
            plan_version_after=f"v{commit.version}",
            plan_sha256=sha256_file(plan_path),
            previous_trace=previous_trace(bundle_file, bundle),
        )
        try:
            render_review_preview(
                plan_model, ir, mezzanine, preview_dir, tools=load_tools(), decision=decision
            )
        except (PreviewError, OSError) as error:
            raise ApplyError("preview_failed", str(error)) from error
        preview_sha = sha256_file(preview_name)
        trace_sha = sha256_file(preview_dir / "preview-trace.json")
    applied_ids = bundle.applied_event_ids
    if commit.event_id is not None and commit.event_id not in applied_ids:
        applied_ids = (*applied_ids, commit.event_id)
    updated = bundle.model_copy(
        update={
            "current": bundle.current.model_copy(
                update={
                    "plan_version": f"v{commit.version}",
                    "plan_sha256": sha256_file(plan_path),
                    "ir_sha256": sha256_file(ir_path),
                    "preview_dir": f"preview-v{commit.version}",
                    "preview_sha256": preview_sha,
                    "trace_sha256": trace_sha,
                }
            ),
            "applied_event_ids": applied_ids,
            "production_policy_sha256": outcome.policy.production_policy_sha256,
        }
    )
    save_bundle(updated, bundle_file)
    result = ApplyResult(
        schema_version="review-apply-result-v1",
        episode_id=bundle.episode_id,
        applied=True,
        idempotent=commit.idempotent,
        classification="clear",
        version=commit.version,
        event_id=commit.event_id,
        plan_sha256=sha256_file(plan_path),
        ir_sha256=sha256_file(ir_path),
        preview_sha256=preview_sha,
        trace_sha256=trace_sha,
    )
    return _finish(result, bundle_file, updated, out_dir), 0


def _finish(
    result: ApplyResult, bundle_file: Path, bundle: ReviewBundle, out_dir: Path
) -> ApplyResult:
    atomic_write(_result_path(out_dir), canonical_model_bytes(result))
    write_metrics(bundle_file, bundle, out_dir)
    return result


__all__ = [
    "RESULT_NAME",
    "ApplyError",
    "ApplyResult",
    "apply_proposal",
]
