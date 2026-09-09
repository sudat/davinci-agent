"""The REAL-episode chain to PREVIEW_READY (Todo 46).

Reuses the established machinery at every step — frozen ingest
(``register_one``), frozen Todo-22 normalization recipes, REAL Todo-33/34/35
analyzers through the Todo-38 orchestrator over a real artifact store under
the out dir, Todo-40 candidate assembly, Todo-41/43 commit authorities,
Todo-42 planner, Todo-44 compiler, Todo-27 preview — with the director
honestly labeled per mode (deterministic baseline vs live). Emits the same
review-bundle model the fixture chain produces plus the resolved production
policy snapshot the H1 ``propose`` consumes.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli.real_analyze import RealAnalyzerError, run_real_analyzers
from services.cli.real_commit import commit_selection
from services.cli.real_director import RealDirectorError, select_and_reconcile
from services.cli.real_episode import (
    EPISODE_MANIFEST_NAME,
    RealEpisodeError,
    ingest_real_episode,
    load_real_episode,
)
from services.cli.real_plan import RealPlanError
from services.cli.real_policy import load_episode_grant
from services.cli.real_pool import (
    RealPoolError,
    pool_for,
)
from services.cli.real_preview import render_preview_tail
from services.cli.real_report import (
    RealChainError,
    RealChainReport,
    RealRunOutcome,
    RunFacts,
    build_report,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.ingest.models import SourceManifest
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.normalize.errors import NormalizeError
from services.normalize.runner import NormalizeContext, normalize_one
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock
from services.validate.selection_models import (
    CommittedEpisodeRecord,
    EditSourceFacts,
)

if TYPE_CHECKING:
    from services.normalize.models import NormalizeRecord

JOB_ID: Final = "job-real-episode-run"
REPORT_NAME: Final = "run-report.json"
PHASE1_LOCK: Final = Path("config/toolchains/phase-1-technical-v2.json")
PHASE0B_LOCK: Final = Path("config/toolchains/phase-0b-v2.json")
PREVIEW_TOOLS_LOCK: Final = Path("config/toolchains/phase-0c-v2.json")
STAGE_ORDER: Final = ("ANALYZED", "PLAN_COMMITTED", "PREVIEW_READY")
MEZZANINE_NAME: Final = "edit-source.mov"


def _advance(state: StateStore, target: str, artifact: str) -> None:
    snapshot = current_job_state(state, JOB_ID)
    apply_transition(
        state,
        JOB_ID,
        expected_status=snapshot.status,
        expected_parent_hash=snapshot.adopted_artifact_hash,
        new_status=target,  # type: ignore[arg-type] (STAGE_ORDER members are statuses)
        new_artifact_hash=artifact,
    )


def _normalize(
    source_manifest: Path, ffmpeg: Path, ffprobe: Path, out_dir: Path
) -> tuple[NormalizeRecord, Path]:
    source = SourceManifest.model_validate_json(source_manifest.read_bytes())
    record = normalize_one(
        source,
        "cfr30",
        NormalizeContext(
            lock_path=PHASE0B_LOCK,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            output_dir=out_dir / "media",
            record_out=out_dir / "normalize-record.json",
        ),
        declared_video_pix_fmt="yuv420p",
    )
    mezzanine = out_dir / "media" / MEZZANINE_NAME
    shutil.copyfile(Path(record.output.path), mezzanine)
    return record, mezzanine


def run_real_chain(  # noqa: C901, PLR0915, PLR0913 (chain wiring: root/stop/out + env/policy/runtime)
    episode_root: Path,
    stop: str,
    out_dir: Path,
    *,
    env: dict[str, str] | None = None,
    policy_path: Path | None = None,
    editorial_runtime: Path | None = None,
) -> RealRunOutcome:
    environment = env if env is not None else dict(os.environ)
    if stop not in STAGE_ORDER:
        raise RealChainError("stop_unknown", f"--stop must be one of {STAGE_ORDER}, got {stop}")
    try:
        manifest = load_real_episode(episode_root)
        lock = load_lock(PHASE1_LOCK)
    except (RealEpisodeError, ValueError) as error:
        raise RealChainError("episode_invalid", str(error)) from error
    try:
        editorial_grant = load_episode_grant(episode_root)
    except ValueError as error:
        raise RealChainError(
            "editorial-grant-invalid", f"cannot parse the episode grant: {error}"
        ) from error
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise RealChainError("lock_wrong", f"{PHASE1_LOCK} is not the phase-1 toolchain lock")
    out_dir.mkdir(parents=True, exist_ok=True)
    episode_copy = out_dir / EPISODE_MANIFEST_NAME
    if not episode_copy.exists():
        episode_copy.write_bytes((episode_root / EPISODE_MANIFEST_NAME).read_bytes())
    state = StateStore.open(out_dir / "state.sqlite3")
    state.create_job(job_id=JOB_ID, episode_id=manifest.episode_id, current_stage="editorial")
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    ffprobe = Path(lock.ffmpeg.ffprobe.path)

    try:
        _source, eligibility, _video_sha = ingest_real_episode(
            manifest, episode_root, ffprobe, out_dir
        )
        normalize_record, mezzanine = _normalize(
            out_dir / "source-manifest.json", ffmpeg, ffprobe, out_dir
        )
    except (RealEpisodeError, NormalizeError, ValueError) as error:
        raise RealChainError("ingest_normalize_failed", str(error)) from error
    total_frames = normalize_record.drop_dup.expected.output_frames
    _advance(state, "INGESTED", sha256_file(mezzanine))
    _advance(state, "NORMALIZED", normalize_record.output.sha256)

    has_budget_env = "V44_MAX_DECODE_FRAMES" in environment
    has_budget_os = os.environ.get("V44_MAX_DECODE_FRAMES") is not None
    if not has_budget_env and not has_budget_os:
        from services.cli.decode_budget import scaled_decode_budget  # noqa: PLC0415

        _budget = scaled_decode_budget(total_frames)
        if _budget is not None:
            os.environ["V44_MAX_DECODE_FRAMES"] = str(_budget)
            environment["V44_MAX_DECODE_FRAMES"] = str(_budget)

    source_id = f"{manifest.episode_id}-edit-source"
    try:
        analysis = run_real_analyzers(lock, mezzanine, source_id, manifest.episode_id, out_dir)
        pool, speech = pool_for(
            analysis.transcript,
            analysis.dialogue,
            analysis.evidence,
            source_id=source_id,
            edit_source_sha=analysis.record.edit_source_sha256,
            total_frames=total_frames,
        )
    except (RealAnalyzerError, RealPoolError, ValueError) as error:
        raise RealChainError("analyze_failed", str(error)) from error
    _advance(state, "ANALYZED", analysis.record.edit_source_sha256)
    facts = RunFacts(
        episode_id=manifest.episode_id,
        eligibility=eligibility,
        edit_source_world_sha256=analysis.record.edit_source_sha256,
        editorial_grant=editorial_grant,
    )
    if stop == "ANALYZED":
        return _finish(build_report(facts, stop), out_dir, None)

    try:
        outcome, selection, reconciled, policy_file = select_and_reconcile(
            episode_id=manifest.episode_id,
            source_id=source_id,
            total_frames=total_frames,
            analysis=analysis,
            pool=pool,
            speech=speech,
            policy_path=policy_path,
            out_dir=out_dir,
            env=environment,
            runtime_path=editorial_runtime,
            editorial_grant=editorial_grant,
        )
        episode_record = CommittedEpisodeRecord(
            episode_id=manifest.episode_id,
            contract_id=eligibility.contract_id,
            status=eligibility.status,
            fixture_only=False,
        )
        edit_facts = EditSourceFacts(
            source_id=source_id,
            edit_source_sha=analysis.record.edit_source_sha256,
            total_frames=total_frames,
        )
        selection_outcome = commit_selection(
            state=state,
            artifacts=ArtifactStore(out_dir / "artifacts"),
            registry=ArtifactRegistry(out_dir / "registry"),
            out_dir=out_dir,
            selection=selection,
            episode=episode_record,
            facts=edit_facts,
            request_hash=outcome.request_hash,
        )
    except (RealDirectorError, RealPlanError, RealPoolError, ValueError) as error:
        raise RealChainError("director_or_commit_failed", str(error)) from error
    facts = RunFacts(
        episode_id=manifest.episode_id,
        eligibility=eligibility,
        edit_source_world_sha256=analysis.record.edit_source_sha256,
        director=outcome,
        editorial_grant=editorial_grant,
        selection_producer=(
            f"{selection.producer.model_role_id}:{selection.producer.contract_version}"
        ),
        selection=selection_outcome,
    )
    if stop == "PLAN_COMMITTED":
        return _finish(build_report(facts, stop), out_dir, None)

    return render_preview_tail(
        state=state,
        out_dir=out_dir,
        facts=facts,
        pool=pool,
        selection=selection,
        reconciled=reconciled,
        episode_record=episode_record,
        edit_facts=edit_facts,
        selection_outcome=selection_outcome,
        speech=speech,
        mezzanine=mezzanine,
        policy_file=policy_file,
    )


def _finish(report: RealChainReport, out_dir: Path, bundle: Path | None) -> RealRunOutcome:
    atomic_write(out_dir / REPORT_NAME, canonical_model_bytes(report))
    return RealRunOutcome(report=report, bundle_file=bundle)


__all__ = [
    "REPORT_NAME",
    "STAGE_ORDER",
    "RealChainError",
    "RealChainReport",
    "RealRunOutcome",
    "run_real_chain",
]
