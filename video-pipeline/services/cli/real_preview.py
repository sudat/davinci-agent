"""The real-chain preview tail: edit commit -> IR -> review plane -> bundle.

Everything after the selection commit that turns the committed artifacts into
the operator-facing PREVIEW_READY view: the Todo-43 edit-plan commit, the
Todo-44 production compile, the review-plane projection (speech subtitles
``st1..stN`` over segments ``s1..sN``), the Todo-27 preview render, and the
review bundle plus its resolved production policy binding.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli.bundle import ReviewTarget, assemble_real_bundle, save_bundle
from services.cli.compile_ir import qc_policy
from services.cli.preview_render import render_review_preview
from services.cli.project import ReviewSubtitle, init_review_store, plan_sha256, project_plan
from services.cli.real_commit import advance, commit_edit_plan
from services.cli.real_episode import EPISODE_MANIFEST_NAME
from services.cli.real_plan import RealPlanError, compile_ir
from services.cli.real_pool import SpeechSegment, cue_source_for
from services.cli.real_report import (
    RealChainError,
    RealRunOutcome,
    RunFacts,
    build_report,
)
from services.cli.review_common import store_ir
from services.contracts.primitives import RationalFrameRate
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.tools import PinnedTools, load_pinned_tools

if TYPE_CHECKING:
    from services.editorial.candidate_models import CandidatePool, SelectionPlanProposal
    from services.editorial.reconcile import ReconciliationResult
    from services.job_runner.state_store import StateStore
    from services.validate.selection_models import (
        CommittedEpisodeRecord,
        EditSourceFacts,
        SelectionCommitOutcome,
    )

REPORT_NAME = "run-report.json"
BUNDLE_NAME = "review-bundle.json"
PREVIEW_TOOLS_LOCK = Path("config/toolchains/phase-0c-v1.json")


def render_preview_tail(  # noqa: PLR0913 (the preview tail wires every committed artifact)
    *,
    state: StateStore,
    out_dir: Path,
    facts: RunFacts,
    pool: CandidatePool,
    selection: SelectionPlanProposal,
    reconciled: ReconciliationResult,
    episode_record: CommittedEpisodeRecord,
    edit_facts: EditSourceFacts,
    selection_outcome: SelectionCommitOutcome,
    speech: tuple[SpeechSegment, ...],
    mezzanine: Path,
    policy_file: Path,
) -> RealRunOutcome:
    try:
        edit_outcome, plan = commit_edit_plan(
            state=state,
            artifacts=ArtifactStore(out_dir / "artifacts"),
            registry=ArtifactRegistry(out_dir / "registry"),
            out_dir=out_dir,
            selection=selection,
            pool=pool,
            reconciled=reconciled,
            facts=edit_facts,
            episode=episode_record,
            selection_outcome=selection_outcome,
        )
        production = compile_ir(
            plan,
            source_id=edit_facts.source_id,
            total_frames=edit_facts.total_frames,
            cue_source=cue_source_for(speech),
            policy=qc_policy(),
            episode_id=facts.episode_id,
        )
        plane = project_plan(
            plan,
            reconciled,
            episode_id=facts.episode_id,
            source_id=edit_facts.source_id,
            total_frames=edit_facts.total_frames,
            rate=RationalFrameRate(num=30, den=1),
            subtitles=tuple(
                ReviewSubtitle(
                    subtitle_id=f"st{position}",
                    segment_id=segment.segment_id,
                    text=segment.text,
                    start_frame=segment.start_frame,
                    end_frame=segment.end_frame,
                )
                for position, segment in enumerate(speech, start=1)
            ),
        )
        store_dir = out_dir / "review-store"
        init_review_store(plane.plan, store_dir)
        ir_v1 = store_ir(store_dir / "ir-v1.json")
        tools: PinnedTools = load_pinned_tools(PREVIEW_TOOLS_LOCK)
        preview_dir = out_dir / "preview-v1"
        render_review_preview(plane.plan, ir_v1, mezzanine, preview_dir, tools=tools)
    except (RealPlanError, ValueError, OSError) as error:
        raise RealChainError("plan_or_preview_failed", str(error)) from error
    preview_sha = sha256_file(preview_dir / "preview.mp4")
    trace_sha = sha256_file(preview_dir / "preview-trace.json")
    target = ReviewTarget(
        plan_version="v1",
        plan_sha256=plan_sha256(plane.plan),
        ir_sha256=sha256_file(store_dir / "ir-v1.json"),
        preview_dir="preview-v1",
        preview_sha256=preview_sha,
        trace_sha256=trace_sha,
    )
    bundle = assemble_real_bundle(
        episode_id=facts.episode_id,
        eligibility_status=facts.eligibility.status,
        mezzanine_sha256=sha256_file(mezzanine),
        edit_source_world_sha256=facts.edit_source_world_sha256,
        episode_manifest_sha256=sha256_file(out_dir / EPISODE_MANIFEST_NAME),
        policy_sha256=sha256_file(policy_file),
        target=target,
    )
    save_bundle(bundle, out_dir / BUNDLE_NAME)
    advance(state, "PREVIEW_READY", preview_sha)
    report = build_report(facts, "PREVIEW_READY").model_copy(
        update={
            "edit_plan_version": edit_outcome.version,
            "edit_plan_sha256": edit_outcome.plan_sha256,
            "production_ir_sha256": production.ir_sha256(),
            "review_plan_sha256": target.plan_sha256,
            "review_ir_sha256": target.ir_sha256,
            "preview_sha256": preview_sha,
            "preview_trace_sha256": trace_sha,
            "bundle_path": str(out_dir / BUNDLE_NAME),
        }
    )
    atomic_write(out_dir / REPORT_NAME, canonical_model_bytes(report))
    return RealRunOutcome(report=report, bundle_file=out_dir / BUNDLE_NAME)




__all__ = ["BUNDLE_NAME", "render_preview_tail"]
