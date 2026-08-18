"""Publication and state-adoption seams for Edit Plan commits (Todo 43).

One helper owning the artifact-store side of an edit commit: publish the
proposal artifact and the immutable committed plan version with the base
selection plan as lineage input, and adopt the plan hash through the Todo-10
state CAS with a mandatory read-back verification — the SAME adoption
semantics as the Todo-41 selection publisher (one StateLane CAS; the edit
family is distinguished by artifact ids/types, not by a state-format fork).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from services.artifact_store.models import PublicationIntent, PublicationReceipt
from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactRef,
    Identifier,
    Producer,
)
from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import CasSnapshot, current_job_state
from services.job_runner.lanes import StateLane
from services.job_runner.state_errors import StateStoreError

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.job_runner.state_store import StateStore
    from services.plan.edit_plan_models import EditPlan

EDIT_COMMIT_AUTHORITY_PRODUCER: Final[Producer] = Producer(
    name="edit-commit-authority", version="todo43-v1"
)
EDIT_PROPOSAL_ARTIFACT_TYPE: Final[str] = "edit-plan-proposal"
EDIT_COMMITTED_ARTIFACT_TYPE: Final[str] = "edit-plan-committed"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def edit_proposal_content_sha256(plan: EditPlan) -> str:
    return _sha256(canonical_model_bytes(plan))


class EditPublisher:
    """Publishes edit commit artifacts and adopts plan hashes via the state CAS."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        registry: ArtifactRegistry,
        state_store: StateStore,
        job_id: Identifier,
        holder: Identifier,
    ) -> None:
        self._artifact_store = artifact_store
        self._registry = registry
        self._state_store = state_store
        self._job_id = job_id
        self._holder = holder

    def _selection_input(self, plan: EditPlan) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=plan.base_selection_plan.plan_artifact_id,
            sha256=plan.base_selection_plan.plan_sha256,
        )

    def publish_proposal(self, plan: EditPlan) -> PublicationReceipt:
        payload = canonical_model_bytes(plan)
        return self._publish_artifact(
            artifact_id=plan.proposal_id,
            artifact_type=EDIT_PROPOSAL_ARTIFACT_TYPE,
            schema_version=plan.schema_version,
            content_hash=_sha256(payload),
            producer=Producer(
                name=plan.producer.model_role_id, version=plan.producer.contract_version
            ),
            inputs=(self._selection_input(plan),),
            payload=payload,
        )

    def publish_plan(
        self,
        artifact_id: str,
        plan_sha: str,
        plan: EditPlan,
        plan_bytes: bytes,
    ) -> PublicationReceipt:
        return self._publish_artifact(
            artifact_id=artifact_id,
            artifact_type=EDIT_COMMITTED_ARTIFACT_TYPE,
            schema_version="edit-plan-committed-v1",
            content_hash=plan_sha,
            producer=EDIT_COMMIT_AUTHORITY_PRODUCER,
            inputs=(
                ArtifactRef(
                    artifact_id=plan.proposal_id,
                    sha256=_sha256(canonical_model_bytes(plan)),
                ),
                self._selection_input(plan),
            ),
            payload=plan_bytes,
        )

    def _publish_artifact(  # noqa: PLR0913 (publication envelope fields are the record)
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        content_hash: str,
        producer: Producer,
        inputs: tuple[ArtifactRef, ...],
        payload: bytes,
    ) -> PublicationReceipt:
        envelope: ArtifactEnvelope[str] = ArtifactEnvelope(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            content_hash=content_hash,
            producer=producer,
            inputs=inputs,
        )
        receipt = self._artifact_store.publish(PublicationIntent(envelope=envelope), payload)
        self._registry.register(self._artifact_store, receipt)
        return receipt

    def adopt(
        self, snapshot: CasSnapshot, plan_sha: str, *, now: int, ttl_seconds: int
    ) -> tuple[CasSnapshot, bool]:
        if snapshot.status == "PLAN_COMMITTED" and snapshot.adopted_artifact_hash == plan_sha:
            changed = False
        else:
            state_lane = StateLane(self._state_store, self._job_id, holder=self._holder)
            state_lane.acquire(now=now, ttl_seconds=ttl_seconds)
            try:
                state_lane.apply(
                    expected_status=snapshot.status,
                    expected_parent_hash=snapshot.adopted_artifact_hash,
                    new_status="PLAN_COMMITTED",
                    new_artifact_hash=plan_sha,
                    now=now,
                )
            finally:
                state_lane.release(now=now)
            changed = True
        read_back = current_job_state(self._state_store, self._job_id)
        if read_back.status != "PLAN_COMMITTED" or read_back.adopted_artifact_hash != plan_sha:
            raise StateStoreError(
                "adoption-readback",
                f"job {self._job_id} read back {read_back.status}/"
                f"{read_back.adopted_artifact_hash} after adopting {plan_sha}",
            )
        return read_back, changed


__all__ = [
    "EDIT_COMMITTED_ARTIFACT_TYPE",
    "EDIT_COMMIT_AUTHORITY_PRODUCER",
    "EDIT_PROPOSAL_ARTIFACT_TYPE",
    "EditPublisher",
    "edit_proposal_content_sha256",
]
