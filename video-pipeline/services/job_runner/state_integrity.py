"""Fail-closed integrity checks and recovery queries for runtime state.

``verify_against_store`` recomputes truth from the artifact store bytes
and the lineage registry: every hash pointer in the database (stage-run
adopted/input hashes, approval targets, cache pointers) must exist in
the store, hash to its declared value, and be indexed; a database that
points elsewhere for the same target fails closed. ``recover_job`` is
query-only resume bookkeeping for the Stage Runner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.artifact_registry.store_view import object_path, object_stats
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import (
    JobRecovery,
    VerificationReport,
)

if TYPE_CHECKING:
    from services.artifact_registry.models import RegistryIndex
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.contracts.primitives import Identifier, Sha256
    from services.job_runner.state_store import StateStore


def recover_job(state_store: StateStore, job_id: Identifier) -> JobRecovery:
    """Return resume bookkeeping derived from committed stage runs."""

    snapshot = state_store.get_job_snapshot(job_id)
    committed = [
        run
        for run in snapshot.stage_runs
        if run.status == "succeeded" and run.adopted_artifact_hash is not None
    ]
    last = committed[-1] if committed else None
    return JobRecovery(
        job_id=snapshot.job.job_id,
        job_status=snapshot.job.status,
        current_stage=snapshot.job.current_stage,
        last_succeeded_stage=None if last is None else last.stage_name,
        resume_input_hashes=() if last is None else last.input_artifact_hashes,
        last_adopted_hash=None if last is None else last.adopted_artifact_hash,
    )


def verify_against_store(
    state_store: StateStore,
    artifact_store: ArtifactStore,
    registry: ArtifactRegistry,
) -> VerificationReport:
    """Cross-check every DB hash pointer against store/registry truth.

    Fails closed with ``missing-artifact`` when a referenced hash has no
    stored object or no registry entry, and with ``row-hash-disagreement``
    when store bytes hash to a different value than the DB pointer, an
    approval target disagrees with the registry entry for its artifact,
    or one idempotency key maps to different adopted hashes.
    """

    index = registry.load()
    runs = state_store.all_stage_runs()
    adopted_by_key: dict[tuple[str, str], str | None] = {}
    verified_hashes: set[str] = set()
    for run in runs:
        key = (run.job_id, run.idempotency_key)
        if key in adopted_by_key and adopted_by_key[key] != run.adopted_artifact_hash:
            raise StateStoreError(
                "row-hash-disagreement",
                f"idempotency key {run.idempotency_key} of job {run.job_id}"
                f" maps to adopted hashes {adopted_by_key[key]} and"
                f" {run.adopted_artifact_hash}",
            )
        adopted_by_key[key] = run.adopted_artifact_hash
        pointers = list(run.input_artifact_hashes)
        if run.adopted_artifact_hash is not None:
            pointers.append(run.adopted_artifact_hash)
        for pointer in pointers:
            _require_store_hash(
                artifact_store, index, pointer, context=f"stage run {run.stage_name}"
            )
            verified_hashes.add(pointer)
    approval_refs = state_store.get_approval_refs()
    for ref in approval_refs:
        _require_store_hash(
            artifact_store, index, ref.target_hash, context=f"approval {ref.purpose}"
        )
        entry = index.entries.get(ref.artifact_ref)
        if entry is None:
            raise StateStoreError(
                "missing-artifact",
                f"approval {ref.purpose} references unindexed artifact"
                f" {ref.artifact_ref}",
            )
        if entry.content_sha256 != ref.target_hash:
            raise StateStoreError(
                "row-hash-disagreement",
                f"approval {ref.purpose} targets {ref.target_hash} but artifact"
                f" {ref.artifact_ref} holds {entry.content_sha256}",
            )
    cache_pointers = state_store.get_cache_pointers()
    for pointer in cache_pointers:
        _require_store_hash(
            artifact_store,
            index,
            pointer.artifact_hash,
            context=f"cache {pointer.cache_key}",
        )
    return VerificationReport(
        checked_stage_runs=len(runs),
        checked_hashes=len(verified_hashes),
        checked_approval_refs=len(approval_refs),
        checked_cache_pointers=len(cache_pointers),
    )


def _require_store_hash(
    artifact_store: ArtifactStore,
    index: RegistryIndex,
    sha: Sha256,
    *,
    context: str,
) -> None:
    stats = object_stats(object_path(artifact_store.store_root, sha))
    if stats is None:
        raise StateStoreError(
            "missing-artifact",
            f"{context}: no object bytes in the artifact store for {sha}",
        )
    _size, digest = stats
    if digest != sha:
        raise StateStoreError(
            "row-hash-disagreement",
            f"{context}: store bytes hash to {digest} but the DB row says {sha}",
        )
    if sha not in index.by_sha256:
        raise StateStoreError(
            "missing-artifact",
            f"{context}: hash {sha} is not indexed by the artifact registry",
        )


__all__ = ["recover_job", "verify_against_store"]
