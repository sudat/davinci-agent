"""Strict shared models for Edit Plan validation and commit (Todo 43).

Mirrors the Todo-41 selection models: a refusal family (lock conflicts stay
deferrable), a frozen validation context (episode record, edit-source facts,
capability allowlist, field locks, and the SELECTION base pin the proposal
must build on), and the commit outcome/refusal pair. The committed wrapper
``CommittedEditPlan`` is the immutable artifact body published to the store.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import ArtifactId, Identifier, Sha256, StrictModel
from services.job_runner.cas import CasSnapshot  # noqa: TC001 (pydantic runtime fields)
from services.plan.edit_plan_models import (
    EditPlan,  # noqa: TC001 (pydantic runtime field)
    SelectionPlanRef,  # noqa: TC001 (runtime field)
)
from services.validate.selection_models import (  # noqa: TC001 (pydantic runtime fields)
    CommittedEpisodeRecord,
    EditSourceFacts,
    SelectionLock,
)

type EditValidatorName = Literal["schema", "semantic", "lock", "capability"]

EDIT_PROPOSAL_SCHEMA_VERSION: str = "edit-plan-v1"
EDIT_COMMITTED_SCHEMA_VERSION: str = "edit-plan-committed-v1"


class CommittedEditPlan(StrictModel):
    """The immutable committed edit plan body published to the artifact store."""

    schema_version: Literal["edit-plan-committed-v1"] = "edit-plan-committed-v1"
    plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    episode_id: Identifier
    base_version: Identifier
    proposal: EditPlan


class EditValidationRefusal(StrictModel):
    """Structured validator refusal; lock conflicts stay deferrable."""

    validator: EditValidatorName | None
    code: str = Field(min_length=1, strict=True)
    detail: str
    deferred: bool = False


class EditPlanValidationError(Exception):
    """Schema-stage parse failure carrying its structured refusal."""

    def __init__(self, refusal: EditValidationRefusal) -> None:
        super().__init__(f"{refusal.validator}: {refusal.code}: {refusal.detail}")
        self.refusal = refusal


class EditValidationContext(StrictModel):
    """Frozen inputs every edit validator recomputes against."""

    episode: CommittedEpisodeRecord
    edit_source: EditSourceFacts
    capability_allowlist: tuple[str, ...] = Field(min_length=1)
    selection_base: SelectionPlanRef
    locks: tuple[SelectionLock, ...] = ()


class EditCommitOutcome(StrictModel):
    """Successful commit: the published version plus the read-back adoption."""

    committed: Literal[True] = True
    version: int = Field(ge=1, strict=True)
    plan_sha256: Sha256
    plan_artifact_id: ArtifactId
    proposal_artifact_id: ArtifactId
    event_id: Sha256
    idempotent: bool
    state_changed: bool
    adopted: CasSnapshot


class EditCommitRefusal(StrictModel):
    """Structured refusal: nothing published, no state change."""

    committed: Literal[False] = False
    validator: EditValidatorName | None
    code: str = Field(min_length=1, strict=True)
    detail: str
    deferred: bool = False
    event_id: Sha256 | None = None


type EditCommitResult = EditCommitOutcome | EditCommitRefusal


__all__ = [
    "EDIT_COMMITTED_SCHEMA_VERSION",
    "EDIT_PROPOSAL_SCHEMA_VERSION",
    "CommittedEditPlan",
    "EditCommitOutcome",
    "EditCommitRefusal",
    "EditCommitResult",
    "EditPlanValidationError",
    "EditValidationContext",
    "EditValidationRefusal",
    "EditValidatorName",
]
