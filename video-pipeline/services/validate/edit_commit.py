"""The serialized Edit Commit authority (Todo 43).

Validators run first and in order; any refusal publishes nothing and changes
no state — it is only recorded in the sealed event log. A proposal whose
base SELECTION plan is no longer the pinned committed version is a
stale-selection conflict before any publish. A fully valid proposal acquires
the Todo-10 plan-commit lane, publishes the proposal artifact and the
immutable committed edit plan version to the content-addressed artifact
store, advances the edit versions index, and records the adoption with ONE
state CAS whose read-back is verified — the SAME atomicity, idempotency,
stale-base, and crash-recovery semantics as Todo 41 (``plan_type=edit``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import current_job_state
from services.job_runner.lanes import plan_commit_resource
from services.job_runner.state_errors import StateStoreError
from services.validate.edit_commit_models import (
    EditCommitResult,
    EditPlanValidationError,
    EditValidationRefusal,
)
from services.validate.edit_commit_publisher import EditPublisher
from services.validate.edit_commit_schema import parse_edit_plan_document
from services.validate.edit_commit_seams import (
    event_version,
    publish_edit_version,
    raw_digest,
    record_edit_refusal,
    replay_edit_adopt,
    replay_event,
)
from services.validate.edit_commit_store import (
    current_edit_base_label,
    latest_edit_version,
    load_edit_index,
    load_events,
)
from services.validate.edit_commit_validators import (
    validate_edit_capabilities,
    validate_edit_locks,
    validate_edit_semantic,
)

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.contracts.primitives import Identifier
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.job_runner.state_store import StateStore
    from services.validate.edit_commit_models import EditValidationContext

COMMIT_STATUSES: Final[frozenset[str]] = frozenset({"PLAN_PROPOSED", "PLAN_COMMITTED"})


class EditCommitAuthority:
    """Serialized writer turning validated edit plan proposals into versions."""

    def __init__(  # noqa: PLR0913 (authority wiring: store, registry, state, context)
        self,
        *,
        artifact_store: ArtifactStore,
        registry: ArtifactRegistry,
        state_store: StateStore,
        job_id: Identifier,
        edit_plan_dir: Path,
        context: EditValidationContext,
        selection_plan: SelectionPlanProposal,
        holder: Identifier,
    ) -> None:
        self._edit_plan_dir = edit_plan_dir
        self._job_id = job_id
        self._context = context
        self._selection_plan = selection_plan
        self._state_store = state_store
        self._holder = holder
        self._publisher = EditPublisher(
            artifact_store=artifact_store,
            registry=registry,
            state_store=state_store,
            job_id=job_id,
            holder=holder,
        )

    def commit(
        self, proposal_document: object, *, now: int, ttl_seconds: int
    ) -> EditCommitResult:
        resource = plan_commit_resource(self._job_id)
        self._state_store.acquire_lease(
            resource=resource, holder=self._publisher_holder(), now=now, ttl_seconds=ttl_seconds
        )
        try:
            return self._commit_locked(proposal_document, now=now, ttl_seconds=ttl_seconds)
        finally:
            self._state_store.release_lease(
                resource=resource, holder=self._publisher_holder(), now=now
            )

    def _publisher_holder(self) -> Identifier:
        return self._holder

    def _commit_locked(
        self, proposal_document: object, *, now: int, ttl_seconds: int
    ) -> EditCommitResult:
        index = load_edit_index(self._edit_plan_dir)
        events = load_events(self._edit_plan_dir)
        snapshot = current_job_state(self._state_store, self._job_id)
        if snapshot.status not in COMMIT_STATUSES:
            raise StateStoreError(
                "edit-commit-status",
                f"job {self._job_id} is {snapshot.status}; edit commits run"
                " only at PLAN_PROPOSED/PLAN_COMMITTED",
            )
        try:
            plan = parse_edit_plan_document(proposal_document)
        except EditPlanValidationError as error:
            return record_edit_refusal(
                self._edit_plan_dir,
                self._context.episode.episode_id,
                events,
                raw_digest(proposal_document),
                None,
                error.refusal,
            )
        digest = hashlib.sha256(canonical_model_bytes(plan)).hexdigest()
        replay = replay_event(events, digest)
        forced: int | None = None
        if replay is not None:
            forced = event_version(replay)
            entry = index.versions.get(str(forced))
            if entry is not None and entry.proposal_sha256 == digest:
                return replay_edit_adopt(
                    self._publisher,
                    plan,
                    replay,
                    entry,
                    index,
                    snapshot=snapshot,
                    now=now,
                    ttl_seconds=ttl_seconds,
                )
        base_label = current_edit_base_label(index)
        if plan.plan_base_version != base_label:
            return record_edit_refusal(
                self._edit_plan_dir,
                self._context.episode.episode_id,
                events,
                digest,
                plan.plan_base_version,
                EditValidationRefusal(
                    validator=None,
                    code="stale_base",
                    detail=(
                        f"proposal base {plan.plan_base_version} is not the latest"
                        f" committed base {base_label}"
                    ),
                ),
            )
        if plan.base_selection_plan != self._context.selection_base:
            return record_edit_refusal(
                self._edit_plan_dir,
                self._context.episode.episode_id,
                events,
                digest,
                plan.plan_base_version,
                EditValidationRefusal(
                    validator=None,
                    code="stale_selection",
                    detail=(
                        f"proposal builds on selection {plan.base_selection_plan.plan_version}"
                        f" ({plan.base_selection_plan.plan_sha256[:8]}) which is not the"
                        " pinned committed selection base "
                        f"{self._context.selection_base.plan_version}"
                        f" ({self._context.selection_base.plan_sha256[:8]})"
                    ),
                ),
            )
        for refusal in (
            validate_edit_semantic(plan, self._context, self._selection_plan),
            validate_edit_locks(plan, self._selection_plan, self._context.locks),
            validate_edit_capabilities(plan, self._context.capability_allowlist),
        ):
            if refusal is not None:
                return record_edit_refusal(
                    self._edit_plan_dir,
                    self._context.episode.episode_id,
                    events,
                    digest,
                    plan.plan_base_version,
                    refusal,
                )
        version = forced if forced is not None else latest_edit_version(index) + 1
        return publish_edit_version(
            self._publisher,
            self._edit_plan_dir,
            plan,
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
    "COMMIT_STATUSES",
    "EditCommitAuthority",
]
