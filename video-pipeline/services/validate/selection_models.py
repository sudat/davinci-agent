"""Strict shared models for Selection Plan validation and commit (Todo 41)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import (
    ArtifactId,
    Identifier,
    Sha256,
    SourceId,
    StrictModel,
)
from services.editorial.candidate_models import (  # noqa: TC001 (pydantic runtime fields)
    CandidateSpan,
    SelectionPlanProposal,
)
from services.fixtures.manifest_phase1 import (  # noqa: TC001 (frozen fixture contract)
    Phase1TechnicalFixtureManifest,
)
from services.ingest.eligibility import EligibilityDeclared as IngestEligibilityDeclared
from services.ingest.eligibility import (
    EpisodeEligibilityBundle,
    classify_episode,
)
from services.job_runner.cas import CasSnapshot  # noqa: TC001 (pydantic runtime fields)

type ValidatorName = Literal[
    "schema", "semantic", "lock", "capability", "episode_contract"
]
type SelectionLockField = Literal[
    "selection", "source_span", "order", "text", "presentation", "audio"
]
type EpisodeStatus = Literal["supported", "assisted", "unsupported"]


class CommittedEpisodeRecord(StrictModel):
    """Committed episode record under the Supported Episode Contract (Todo 32)."""

    episode_id: Identifier
    contract_id: str = Field(min_length=1, strict=True)
    status: EpisodeStatus
    fixture_only: bool
    privacy_flags: tuple[str, ...] = ()
    rights_flags: tuple[str, ...] = ()


def episode_record_from_manifest(
    manifest: Phase1TechnicalFixtureManifest,
) -> CommittedEpisodeRecord:
    """Recompute the committed episode record from the frozen fixture manifest."""

    declared = manifest.eligibility.declared
    result = classify_episode(
        EpisodeEligibilityBundle(
            episode_id=manifest.fixture_id,
            declared=IngestEligibilityDeclared(
                language=declared.language,
                principal_video_count=declared.principal_video_count,
                audio_present=declared.audio_present,
                vfr=declared.vfr,
                cfr_normalizable=declared.cfr_normalizable,
                total_duration_sec=declared.total_duration_sec,
                speaker_count=declared.speaker_count,
                privacy_flags=declared.privacy_flags,
                rights_flags=declared.rights_flags,
            ),
        )
    )
    return CommittedEpisodeRecord(
        episode_id=manifest.fixture_id,
        contract_id=result.contract_id,
        status=result.status,
        fixture_only=manifest.fixture_only,
        privacy_flags=tuple(
            gate.flag for gate in result.human_gates if gate.kind == "privacy"
        ),
        rights_flags=tuple(
            gate.flag for gate in result.human_gates if gate.kind == "rights"
        ),
    )


class SelectionLock(StrictModel):
    """Field-level lock over one committed base span (PRD 14.3)."""

    field: SelectionLockField
    source_id: SourceId
    edit_source_sha: Sha256
    span: CandidateSpan
    base_plan_version: Identifier
    grantor: Identifier
    basis: str = Field(min_length=1, strict=True)
    granted_at_seq: int = Field(ge=0, strict=True)


class EditSourceFacts(StrictModel):
    """Edit-source extent and identity the proposal spans must live inside."""

    source_id: SourceId
    edit_source_sha: Sha256
    total_frames: int = Field(gt=0, strict=True)


class ValidationContext(StrictModel):
    """Frozen inputs every validator recomputes against — never proposal claims."""

    episode: CommittedEpisodeRecord
    edit_source: EditSourceFacts
    capability_allowlist: tuple[str, ...] = Field(min_length=1)
    locks: tuple[SelectionLock, ...] = ()
    mandatory_candidate_ids: tuple[Sha256, ...] = ()


class ValidationRefusal(StrictModel):
    """Structured validator refusal; lock conflicts stay deferrable."""

    validator: ValidatorName | None
    code: str = Field(min_length=1, strict=True)
    detail: str
    deferred: bool = False


class SelectionValidationError(Exception):
    """Schema-stage parse failure carrying its structured refusal."""

    def __init__(self, refusal: ValidationRefusal) -> None:
        super().__init__(f"{refusal.validator}: {refusal.code}: {refusal.detail}")
        self.refusal = refusal


class CommittedSelectionPlan(StrictModel):
    """The immutable committed plan body published to the artifact store."""

    schema_version: Literal["selection-plan-committed-v1"] = "selection-plan-committed-v1"
    plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    episode_id: Identifier
    base_version: Identifier
    proposal: SelectionPlanProposal


class SelectionCommitOutcome(StrictModel):
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


class SelectionCommitRefusal(StrictModel):
    """Structured refusal: nothing published, no state change."""

    committed: Literal[False] = False
    validator: ValidatorName | None
    code: str = Field(min_length=1, strict=True)
    detail: str
    deferred: bool = False
    event_id: Sha256 | None = None


type SelectionCommitResult = SelectionCommitOutcome | SelectionCommitRefusal


__all__ = [
    "CommittedEpisodeRecord",
    "CommittedSelectionPlan",
    "EditSourceFacts",
    "EpisodeStatus",
    "SelectionCommitOutcome",
    "SelectionCommitRefusal",
    "SelectionCommitResult",
    "SelectionLock",
    "SelectionLockField",
    "SelectionValidationError",
    "ValidationContext",
    "ValidationRefusal",
    "ValidatorName",
    "episode_record_from_manifest",
]
