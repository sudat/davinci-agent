"""Shared Todo-41 rig: fixture proposals from frozen inputs via Todo-40 builders.

Every proposal here is assembled by ``build_selection_proposal`` over a
frozen Phase-1 fixture manifest and the frozen golden director document
— no invented candidates, no clocks, no network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.editorial.proposal_builder import build_selection_proposal
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.validate.selection_commit import SelectionCommitAuthority
from services.validate.selection_models import (
    EditSourceFacts,
    ValidationContext,
    episode_record_from_manifest,
)
from services.validate.selection_plan_store import initialize_plan_store
from tests.editorial.support import golden_proposal, load_manifest
from tests.editorial.test_selection_proposal import _document, index_for, pool_for

if TYPE_CHECKING:
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.validate.selection_commit import SelectionCommitResult
    from services.validate.selection_models import CommittedEpisodeRecord, SelectionLock

JOB = "job-select-1"
BASE = "plan-base-v0"
ALL_FIXTURE_IDS = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)


def proposal_for(fixture_id: str, *, base: str = BASE) -> SelectionPlanProposal:
    manifest = load_manifest(fixture_id)
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool_for(manifest),
        director_document=_document(fixture_id),
        evidence_index=index_for(manifest),
        plan_base_version=base,
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def reduced_proposal_for(fixture_id: str, *, base: str = BASE) -> SelectionPlanProposal:
    """A DISTINCT proposal on the same base: only the first segment selected."""

    manifest = load_manifest(fixture_id)
    document: dict[str, object] = dict(golden_proposal(fixture_id).model_dump(mode="json"))
    selection = document["selection"]
    assert isinstance(selection, list)
    document["selection"] = selection[:1]
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool_for(manifest),
        director_document=document,
        evidence_index=index_for(manifest),
        plan_base_version=base,
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def context_for(
    fixture_id: str,
    *,
    allowlist: tuple[str, ...] = PHASE_0A_CAPABILITIES,
    locks: tuple[SelectionLock, ...] = (),
    episode: CommittedEpisodeRecord | None = None,
) -> ValidationContext:
    manifest = load_manifest(fixture_id)
    pool = pool_for(manifest)
    return ValidationContext(
        episode=episode if episode is not None else episode_record_from_manifest(manifest),
        edit_source=EditSourceFacts(
            source_id=pool.source_id,
            edit_source_sha=pool.edit_source_sha,
            total_frames=pool.total_frames,
        ),
        capability_allowlist=tuple(allowlist),
        locks=locks,
    )


def store_object_count(store_root: Path) -> int:
    objects = store_root / "objects"
    if not objects.exists():
        return 0
    return sum(
        1 for path in objects.rglob("*") if path.is_file() and not path.name.startswith(".")
    )


@dataclass
class CommitRig:
    state: StateStore
    artifacts: ArtifactStore
    registry: ArtifactRegistry
    authority: SelectionCommitAuthority
    plan_dir: Path

    def commit(
        self, document: object, *, now: int = 100, ttl_seconds: int = 100
    ) -> SelectionCommitResult:
        return self.authority.commit(document, now=now, ttl_seconds=ttl_seconds)


def advance_to_plan_proposed(state: StateStore) -> None:
    snapshot = current_job_state(state, JOB)
    for target in ("INGESTED", "NORMALIZED", "ANALYZED", "PLAN_PROPOSED"):
        snapshot = apply_transition(
            state,
            JOB,
            expected_status=snapshot.status,
            expected_parent_hash=snapshot.adopted_artifact_hash,
            new_status=target,
            new_artifact_hash=hashlib.sha256(f"step-{target}".encode()).hexdigest(),
        )


def rig_for(
    tmp_path: Path,
    fixture_id: str = "p1-ref-02-pauses-fillers",
    *,
    context: ValidationContext | None = None,
) -> CommitRig:
    manifest = load_manifest(fixture_id)
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id=manifest.fixture_id, current_stage="editorial")
    advance_to_plan_proposed(state)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    registry = ArtifactRegistry(tmp_path / "registry")
    plan_dir = tmp_path / "plan-store"
    initialize_plan_store(plan_dir, episode_id=manifest.fixture_id, base_version=BASE)
    authority = SelectionCommitAuthority(
        artifact_store=artifacts,
        registry=registry,
        state_store=state,
        job_id=JOB,
        plan_dir=plan_dir,
        context=context if context is not None else context_for(fixture_id),
        holder="commit-a",
    )
    return CommitRig(
        state=state, artifacts=artifacts, registry=registry, authority=authority, plan_dir=plan_dir
    )
