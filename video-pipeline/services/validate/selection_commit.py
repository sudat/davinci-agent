"""The serialized Selection Commit authority (Todo 41).

Validators run first and in order; any refusal publishes nothing and
changes no state — it is only recorded in the sealed event log. A fully
valid proposal acquires the Todo-10 plan-commit lane, publishes the
proposal artifact and the immutable committed plan version to the
content-addressed artifact store, advances the versions index, and
records the adoption with ONE state CAS whose read-back is verified.
Every step is idempotent across the crash seams: a re-commit of the
same proposal completes an orphaned publish/index and re-adopts, and
never mints a second version for the same content and base. A proposal
built on a base that is no longer latest is a stale-base conflict.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import current_job_state
from services.job_runner.lanes import plan_commit_resource
from services.job_runner.state_errors import StateStoreError
from services.validate.selection_models import (
    SelectionCommitResult,
    SelectionValidationError,
    ValidationContext,
    ValidationRefusal,
)
from services.validate.selection_plan_store import (
    current_base_label,
    latest_version,
    load_events,
    load_index,
)
from services.validate.selection_publisher import (
    COMMITTED_ARTIFACT_TYPE,
    PROPOSAL_ARTIFACT_TYPE,
    SelectionPublisher,
)
from services.validate.selection_schema import parse_selection_document
from services.validate.selection_seams import (
    event_version,
    publish_version,
    raw_digest,
    record_refusal,
    replay_adopt,
    replay_event,
)
from services.validate.selection_validators import (
    validate_capabilities,
    validate_episode_contract,
    validate_locks,
    validate_semantic,
)

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.contracts.primitives import Identifier
    from services.job_runner.state_store import StateStore

COMMIT_STATUSES: Final[frozenset[str]] = frozenset({"PLAN_PROPOSED", "PLAN_COMMITTED"})


class SelectionCommitAuthority:
    """Serialized writer turning validated proposals into committed versions."""

    def __init__(  # noqa: PLR0913 (authority wiring: store, registry, state, context)
        self,
        *,
        artifact_store: ArtifactStore,
        registry: ArtifactRegistry,
        state_store: StateStore,
        job_id: Identifier,
        plan_dir: Path,
        context: ValidationContext,
        holder: Identifier,
    ) -> None:
        self._plan_dir = plan_dir
        self._job_id = job_id
        self._context = context
        self._holder = holder
        self._state_store = state_store
        self._publisher = SelectionPublisher(
            artifact_store=artifact_store,
            registry=registry,
            state_store=state_store,
            job_id=job_id,
            holder=holder,
        )

    def commit(
        self, proposal_document: object, *, now: int, ttl_seconds: int
    ) -> SelectionCommitResult:
        resource = plan_commit_resource(self._job_id)
        self._state_store.acquire_lease(
            resource=resource, holder=self._holder, now=now, ttl_seconds=ttl_seconds
        )
        try:
            return self._commit_locked(proposal_document, now=now, ttl_seconds=ttl_seconds)
        finally:
            self._state_store.release_lease(resource=resource, holder=self._holder, now=now)

    def _commit_locked(
        self, proposal_document: object, *, now: int, ttl_seconds: int
    ) -> SelectionCommitResult:
        index = load_index(self._plan_dir)
        events = load_events(self._plan_dir)
        snapshot = current_job_state(self._state_store, self._job_id)
        if snapshot.status not in COMMIT_STATUSES:
            raise StateStoreError(
                "selection-commit-status",
                f"job {self._job_id} is {snapshot.status}; selection commits run"
                " only at PLAN_PROPOSED/PLAN_COMMITTED",
            )
        try:
            proposal = parse_selection_document(proposal_document)
        except SelectionValidationError as error:
            return record_refusal(
                self._plan_dir,
                self._context.episode.episode_id,
                events,
                raw_digest(proposal_document),
                None,
                error.refusal,
            )
        digest = hashlib.sha256(canonical_model_bytes(proposal)).hexdigest()
        replay = replay_event(events, digest)
        forced: int | None = None
        if replay is not None:
            forced = event_version(replay)
            entry = index.versions.get(str(forced))
            if entry is not None and entry.proposal_sha256 == digest:
                return replay_adopt(
                    self._publisher,
                    proposal,
                    replay,
                    entry,
                    index,
                    snapshot=snapshot,
                    now=now,
                    ttl_seconds=ttl_seconds,
                )
        base_label = current_base_label(index)
        if proposal.plan_base_version != base_label:
            return record_refusal(
                self._plan_dir,
                self._context.episode.episode_id,
                events,
                digest,
                proposal.plan_base_version,
                ValidationRefusal(
                    validator=None,
                    code="stale_base",
                    detail=(
                        f"proposal base {proposal.plan_base_version} is not the latest"
                        f" committed base {base_label}"
                    ),
                ),
            )
        for refusal in (
            validate_semantic(proposal, self._context),
            validate_locks(proposal, self._context.locks),
            validate_capabilities(proposal, self._context.capability_allowlist),
            validate_episode_contract(proposal, self._context.episode),
        ):
            if refusal is not None:
                return record_refusal(
                    self._plan_dir,
                    self._context.episode.episode_id,
                    events,
                    digest,
                    proposal.plan_base_version,
                    refusal,
                )
        version = forced if forced is not None else latest_version(index) + 1
        return publish_version(
            self._publisher,
            self._plan_dir,
            proposal,
            index=index,
            version=version,
            event=replay,
            events=events,
            digest=digest,
            base_label=base_label,
            snapshot=snapshot,
            now=now,
            ttl_seconds=ttl_seconds,
        )


__all__ = [
    "COMMITTED_ARTIFACT_TYPE",
    "COMMIT_STATUSES",
    "PROPOSAL_ARTIFACT_TYPE",
    "SelectionCommitAuthority",
]
