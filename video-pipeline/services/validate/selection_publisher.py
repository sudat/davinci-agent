"""Publication and state-adoption seams for Selection commits (Todo 41).

One helper owning the artifact-store side of a commit: publish the
proposal artifact and the immutable committed plan version with full
lineage inputs, and adopt a plan hash through the Todo-10 state CAS
with a mandatory read-back verification.
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
from services.validate.selection_validators import evidence_refs

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.job_runner.state_store import StateStore

COMMIT_AUTHORITY_PRODUCER: Final[Producer] = Producer(
    name="selection-commit-authority", version="todo41-v1"
)
PROPOSAL_ARTIFACT_TYPE: Final[str] = "selection-plan-proposal"
COMMITTED_ARTIFACT_TYPE: Final[str] = "selection-plan-committed"
COMMITTED_SCHEMA_VERSION: Final[str] = "selection-plan-committed-v1"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def proposal_content_sha256(proposal: SelectionPlanProposal) -> str:
    return _sha256(canonical_model_bytes(proposal))


class SelectionPublisher:
    """Publishes commit artifacts and adopts plan hashes via the state CAS."""

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

    def publish_proposal(self, proposal: SelectionPlanProposal) -> PublicationReceipt:
        proposal_bytes = canonical_model_bytes(proposal)
        return self._publish_artifact(
            artifact_id=proposal.proposal_id,
            artifact_type=PROPOSAL_ARTIFACT_TYPE,
            schema_version=proposal.schema_version,
            content_hash=_sha256(proposal_bytes),
            producer=Producer(
                name=proposal.producer.model_role_id,
                version=proposal.producer.contract_version,
            ),
            inputs=tuple(
                ArtifactRef(artifact_id=row_id, sha256=row_sha)
                for row_id, row_sha in evidence_refs(proposal)
            ),
            payload=proposal_bytes,
        )

    def publish_plan(
        self,
        artifact_id: str,
        plan_sha: str,
        proposal: SelectionPlanProposal,
        plan_bytes: bytes,
    ) -> PublicationReceipt:
        inputs = (
            ArtifactRef(
                artifact_id=proposal.proposal_id,
                sha256=_sha256(canonical_model_bytes(proposal)),
            ),
            *(
                ArtifactRef(artifact_id=row_id, sha256=row_sha)
                for row_id, row_sha in evidence_refs(proposal)
            ),
        )
        return self._publish_artifact(
            artifact_id=artifact_id,
            artifact_type=COMMITTED_ARTIFACT_TYPE,
            schema_version=COMMITTED_SCHEMA_VERSION,
            content_hash=plan_sha,
            producer=COMMIT_AUTHORITY_PRODUCER,
            inputs=inputs,
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
    "COMMITTED_ARTIFACT_TYPE",
    "COMMITTED_SCHEMA_VERSION",
    "COMMIT_AUTHORITY_PRODUCER",
    "PROPOSAL_ARTIFACT_TYPE",
    "SelectionPublisher",
    "proposal_content_sha256",
]
