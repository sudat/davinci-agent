"""The commit/projection half of the Phase-1 run: the serialized chain.

Advances the job state through ANALYZED, commits the replayed selection (Todo
41) and the deterministic edit plan (Todo 43) through their authorities,
compiles the production IR (Todo 44), projects the committed plan onto the
frozen review plane, opens the Todo-30 review store, renders the Todo-27
preview, and assembles the review bundle. ``run_phase1`` is the single entry
point behind ``services.cli.phase1 run``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli.bundle import (
    BUNDLE_NAME,
    ReviewTarget,
    assemble_initial_bundle,
    save_bundle,
)
from services.cli.chain import (
    PREVIEW_TOOLS_LOCK,
    STAGE_ORDER,
    ChainError,
    ChainRunReport,
    director_replay,
    prepare_episode,
)
from services.cli.compile_ir import compile_production_ir
from services.cli.plan_solve import committed_plan_for, selection_ref
from services.cli.planning import (
    EDIT_BASE,
    SELECTION_BASE,
    edit_source_facts,
    reconciled_for,
    selection_proposal_from,
)
from services.cli.preview_render import render_review_preview
from services.cli.project import (
    ReviewPlane,
    init_review_store,
    plan_sha256,
    project_review_plan,
)
from services.cli.review_common import store_ir
from services.editorial.reconcile import ReconciliationResult
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.job_runner.cas import apply_transition, current_job_state
from services.preview.tools import PinnedTools, load_pinned_tools
from services.validate.edit_commit import EditCommitAuthority
from services.validate.edit_commit_models import EditCommitOutcome, EditValidationContext
from services.validate.edit_commit_store import initialize_edit_plan_store
from services.validate.selection_commit import SelectionCommitAuthority
from services.validate.selection_models import (
    SelectionCommitOutcome,
    ValidationContext,
    episode_record_from_manifest,
)
from services.validate.selection_plan_store import initialize_plan_store

if TYPE_CHECKING:
    from services.editorial.reconcile import ReconciliationResult
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.job_runner.state_models import JobStatus
    from services.job_runner.state_store import StateStore

JOB_ID: Final = "job-phase1-run"
REPORT_NAME: Final = "run-report.json"


def _advance(state: StateStore, target: JobStatus, artifact: str) -> None:
    snapshot = current_job_state(state, JOB_ID)
    apply_transition(
        state,
        JOB_ID,
        expected_status=snapshot.status,
        expected_parent_hash=snapshot.adopted_artifact_hash,
        new_status=target,
        new_artifact_hash=hashlib.sha256(artifact.encode()).hexdigest(),
    )


def _as_outcome(result: object, label: str) -> SelectionCommitOutcome | EditCommitOutcome:
    if isinstance(result, SelectionCommitOutcome | EditCommitOutcome):
        return result
    raise ChainError(f"{label}_refused", str(result))


@dataclass(frozen=True, slots=True)
class RunOutcome:
    report: ChainRunReport
    manifest: Phase1TechnicalFixtureManifest
    bundle_file: Path | None
    review_plane: ReviewPlane | None


def _finish(
    report: ChainRunReport,
    manifest: Phase1TechnicalFixtureManifest,
    out_dir: Path,
    plane: ReviewPlane | None,
) -> RunOutcome:
    atomic_write(out_dir / REPORT_NAME, canonical_model_bytes(report))
    return RunOutcome(
        report=report,
        manifest=manifest,
        bundle_file=out_dir / BUNDLE_NAME if report.bundle_path else None,
        review_plane=plane,
    )


def run_phase1(
    episode_root: Path, stop: str, out_dir: Path
) -> RunOutcome:
    if stop not in STAGE_ORDER:
        raise ChainError("stop_unknown", f"--stop must be one of {STAGE_ORDER}, got {stop}")
    prepared = prepare_episode(episode_root, out_dir)
    manifest = prepared.manifest
    eligibility = prepared.eligibility
    manifest_copy = prepared.manifest_copy
    state = prepared.state
    media = prepared.media
    record = prepared.record
    base = {
        "episode_id": manifest.fixture_id,
        "fixture_only": manifest.fixture_only,
        "stop_stage": stop,
        "eligibility_status": eligibility.status,
        "edit_source_world_sha256": record.edit_source_sha256,
    }
    if stop == "ANALYZED":
        report = ChainRunReport(schema_version="phase1-run-report-v1", **base)
        return _finish(report, manifest, out_dir, None)

    director, request_key = director_replay(manifest, Path(record.index_path))
    artifacts = ArtifactStore(out_dir / "artifacts")
    registry = ArtifactRegistry(out_dir / "registry")
    selection_dir = out_dir / "selection-plan"
    initialize_plan_store(
        selection_dir, episode_id=manifest.fixture_id, base_version=SELECTION_BASE
    )
    _advance(state, "PLAN_PROPOSED", request_key)
    selection = selection_proposal_from(manifest, director)
    context = ValidationContext(
        episode=episode_record_from_manifest(manifest),
        edit_source=edit_source_facts(manifest),
        capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
        locks=(),
    )
    selection_outcome = _as_outcome(
        SelectionCommitAuthority(
            artifact_store=artifacts,
            registry=registry,
            state_store=state,
            job_id=JOB_ID,
            plan_dir=selection_dir,
            context=context,
            holder="phase1-run",
        ).commit(selection, now=100, ttl_seconds=100),
        "selection",
    )
    base["director_request_hash"] = request_key
    base["selection_version"] = selection_outcome.version
    base["selection_plan_sha256"] = selection_outcome.plan_sha256
    _advance(state, "PLAN_COMMITTED", selection_outcome.plan_sha256)
    if stop == "PLAN_COMMITTED":
        report = ChainRunReport(schema_version="phase1-run-report-v1", **base)
        return _finish(report, manifest, out_dir, None)

    reconciled: ReconciliationResult = reconciled_for(manifest, director)
    ref = selection_ref(
        manifest,
        selection_outcome.version,
        selection_outcome.plan_sha256,
        selection_outcome.plan_artifact_id,
    )
    plan = committed_plan_for(manifest, selection, reconciled, ref)
    edit_dir = out_dir / "edit-plan"
    initialize_edit_plan_store(edit_dir, episode_id=manifest.fixture_id, base_version=EDIT_BASE)
    edit_outcome = _as_outcome(
        EditCommitAuthority(
            artifact_store=artifacts,
            registry=registry,
            state_store=state,
            job_id=JOB_ID,
            edit_plan_dir=edit_dir,
            context=EditValidationContext(
                episode=context.episode,
                edit_source=context.edit_source,
                capability_allowlist=context.capability_allowlist,
                selection_base=ref,
                locks=(),
            ),
            selection_plan=selection,
            holder="phase1-run",
        ).commit(plan, now=100, ttl_seconds=100),
        "edit_plan",
    )
    production = compile_production_ir(manifest, plan)
    plane = project_review_plan(plan, reconciled, manifest)
    store_dir = out_dir / "review-store"
    init_review_store(plane.plan, store_dir)
    ir_v1 = store_ir(store_dir / "ir-v1.json")
    tools: PinnedTools = load_pinned_tools(PREVIEW_TOOLS_LOCK)
    preview_dir = out_dir / "preview-v1"
    render_review_preview(plane.plan, ir_v1, media.mezzanine, preview_dir, tools=tools)
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
    bundle = assemble_initial_bundle(
        manifest=manifest,
        eligibility_status=eligibility.status,
        mezzanine_sha256=sha256_file(media.mezzanine),
        edit_source_world_sha256=media.world_sha256,
        manifest_sha256=sha256_file(manifest_copy),
        target=target,
    )
    save_bundle(bundle, out_dir / BUNDLE_NAME)
    _advance(state, "PREVIEW_READY", preview_sha)
    report = ChainRunReport(
        schema_version="phase1-run-report-v1",
        **base,
        edit_plan_version=edit_outcome.version,
        edit_plan_sha256=edit_outcome.plan_sha256,
        production_ir_sha256=production.ir_sha256(),
        review_plan_sha256=target.plan_sha256,
        review_ir_sha256=target.ir_sha256,
        preview_sha256=preview_sha,
        preview_trace_sha256=trace_sha,
        bundle_path=str(out_dir / BUNDLE_NAME),
    )
    return _finish(report, manifest, out_dir, plane)


__all__ = ["JOB_ID", "REPORT_NAME", "RunOutcome", "run_phase1"]
