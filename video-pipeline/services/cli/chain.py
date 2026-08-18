"""Upstream half of the deterministic Phase-1 fixture chain (Todo 45).

Manifest loading + declared eligibility + the Todo-38 analyzer orchestration
over the synthesized edit source + the Todo-39 Editorial Director replay over
the real media-query index + the Todo-44 production compile helpers. The
commit/projection half lives in ``services.cli.run_stage``; the CLI surface is
``services.cli.phase1``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.cli.analyzers import run_analyzers
from services.cli.media import SynthesizedMedia, synthesize_edit_source
from services.contracts.primitives import Sha256, StrictModel
from services.editorial.director import EditorialDirector
from services.editorial.evidence import assemble_evidence
from services.editorial.models import DeclaredCandidate, DirectorRequest
from services.editorial.pin import load_pin, load_replay_set
from services.editorial.prompt import PROMPT_CONTRACT_VERSION, request_hash
from services.editorial.transport import ReplayTransport, replay_response
from services.fixtures.manifest_phase1 import PHASE_1_FIXTURE_IDS, Phase1TechnicalFixtureManifest
from services.ingest.eligibility import (
    EligibilityDeclared,
    EligibilityResult,
    EpisodeEligibilityBundle,
    classify_episode,
)
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.media_query.api import MediaQueryApi
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

if TYPE_CHECKING:
    from services.analyze.orchestrator_models import (
        OrchestrationRecord,
    )
    from services.contracts.editorial_model import EditorialSelectionProposal
    from services.job_runner.state_models import JobStatus

STAGE_ORDER: Final = ("ANALYZED", "PLAN_COMMITTED", "PREVIEW_READY")
PHASE1_LOCK: Final = Path("config/toolchains/phase-1-technical-v1.json")
PREVIEW_TOOLS_LOCK: Final = Path("config/toolchains/phase-0c-v1.json")
MANIFEST_NAME: Final = "manifest.json"


class ChainError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ChainRunReport(StrictModel):
    schema_version: Literal["phase1-run-report-v1"]
    episode_id: str
    fixture_only: bool
    stop_stage: str
    eligibility_status: str
    edit_source_world_sha256: Sha256
    director_request_hash: Sha256 | None = None
    selection_version: int | None = None
    selection_plan_sha256: Sha256 | None = None
    edit_plan_version: int | None = None
    edit_plan_sha256: Sha256 | None = None
    production_ir_sha256: Sha256 | None = None
    review_plan_sha256: Sha256 | None = None
    review_ir_sha256: Sha256 | None = None
    preview_sha256: Sha256 | None = None
    preview_trace_sha256: Sha256 | None = None
    bundle_path: str | None = None


def load_episode_manifest(episode_root: Path) -> Phase1TechnicalFixtureManifest:
    path = episode_root / MANIFEST_NAME
    try:
        manifest = Phase1TechnicalFixtureManifest.model_validate_json(path.read_bytes())
    except OSError as error:
        raise ChainError(
            "manifest_missing", f"episode root has no {MANIFEST_NAME}: {error}"
        ) from error
    except ValueError as error:
        raise ChainError("manifest_invalid", str(error)) from error
    if manifest.fixture_id not in PHASE_1_FIXTURE_IDS:
        raise ChainError(
            "fixture_unknown", f"{manifest.fixture_id} is not a frozen Phase-1 fixture"
        )
    return manifest


def classify_eligibility(manifest: Phase1TechnicalFixtureManifest) -> EligibilityResult:
    declared = manifest.eligibility.declared
    return classify_episode(
        EpisodeEligibilityBundle(
            episode_id=manifest.fixture_id,
            declared=EligibilityDeclared(
                language=declared.language,
                principal_video_count=declared.principal_video_count,
                audio_present=declared.audio_present,
                vfr=declared.vfr,
                cfr_normalizable=declared.cfr_normalizable,
                total_duration_sec=declared.total_duration_sec,
                speaker_count=declared.speaker_count,
                privacy_flags=tuple(declared.privacy_flags),
                rights_flags=tuple(declared.rights_flags),
            ),
        )
    )


def director_request(manifest: Phase1TechnicalFixtureManifest) -> DirectorRequest:
    return DirectorRequest(
        episode_id=manifest.fixture_id,
        edit_source=manifest.edit_source,
        rules=manifest.editorial_rules,
        candidates=tuple(
            DeclaredCandidate(
                segment_id=segment.segment_id,
                kind=segment.kind,
                text=segment.text,
                start_frame=segment.span.start_frame,
                end_frame=segment.span.end_frame,
                content_score=segment.observed.content_score,
                clarity_score=segment.observed.clarity_score,
                pause_ms=segment.observed.pause_ms,
                retake_group=segment.observed.retake_group,
            )
            for segment in manifest.transcript.segments
        ),
    )


def director_replay(
    manifest: Phase1TechnicalFixtureManifest, index_path: Path
) -> tuple[EditorialSelectionProposal, str]:
    """Replay the frozen director proposal over the real media-query index."""

    pin = load_pin()
    replay_set = load_replay_set(pin)
    request = director_request(manifest)
    with MediaQueryApi.open(index_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        transport = ReplayTransport({key: replay_response(replay_set, manifest.fixture_id)})
        result = EditorialDirector(transport=transport).run(request, api=api)
    if result.error is not None or result.proposal is None:
        detail = result.error.detail if result.error is not None else "no proposal"
        raise ChainError("director_failed", f"{result.envelope.status}: {detail}")
    return result.proposal, key


__all__ = [
    "MANIFEST_NAME",
    "PHASE1_LOCK",
    "PREPARE_JOB_ID",
    "PREVIEW_TOOLS_LOCK",
    "STAGE_ORDER",
    "ChainError",
    "ChainRunReport",
    "classify_eligibility",
    "director_replay",
    "director_request",
    "load_episode_manifest",
    "prepare_episode",
    "run_analyzers",
]


PREPARE_JOB_ID = "job-phase1-run"


@dataclass(frozen=True, slots=True)
class PreparedEpisode:
    manifest: Phase1TechnicalFixtureManifest
    eligibility: EligibilityResult
    manifest_copy: Path
    state: StateStore
    media: SynthesizedMedia
    record: OrchestrationRecord


def _advance(state: StateStore, job_id: str, target: JobStatus, artifact: str) -> None:
    snapshot = current_job_state(state, job_id)
    apply_transition(
        state,
        job_id,
        expected_status=snapshot.status,
        expected_parent_hash=snapshot.adopted_artifact_hash,
        new_status=target,
        new_artifact_hash=hashlib.sha256(artifact.encode()).hexdigest(),
    )


def prepare_episode(episode_root: Path, out_dir: Path) -> PreparedEpisode:
    """Eligibility + media synthesis + declared analyzers + state to ANALYZED."""

    manifest = load_episode_manifest(episode_root)
    eligibility = classify_eligibility(manifest)
    if eligibility.status != "supported":
        raise ChainError(
            "episode_not_supported",
            f"{manifest.fixture_id} classifies {eligibility.status}: "
            f"{[reason.code for reason in eligibility.reasons]}",
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_copy = out_dir / "manifest.json"
    if not manifest_copy.exists():
        manifest_copy.write_bytes((episode_root / "manifest.json").read_bytes())
    lock = load_lock(PHASE1_LOCK)
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise ChainError("lock_wrong", f"{PHASE1_LOCK} is not the phase-1 toolchain lock")
    state = StateStore.open(out_dir / "state.sqlite3")
    state.create_job(
        job_id=PREPARE_JOB_ID, episode_id=manifest.fixture_id, current_stage="editorial"
    )
    media = synthesize_edit_source(manifest, lock, out_dir / "media")
    record = run_analyzers(manifest, lock, media, out_dir)
    _advance(state, PREPARE_JOB_ID, "INGESTED", "ingest")
    _advance(state, PREPARE_JOB_ID, "NORMALIZED", "normalize")
    _advance(state, PREPARE_JOB_ID, "ANALYZED", record.edit_source_sha256)
    return PreparedEpisode(
        manifest=manifest,
        eligibility=eligibility,
        manifest_copy=manifest_copy,
        state=state,
        media=media,
        record=record,
    )



