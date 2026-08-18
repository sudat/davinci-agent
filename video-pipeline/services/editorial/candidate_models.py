"""Strict Candidate and Selection Plan Proposal models (Todo 40).

Candidates are the reconciled unit of editorial intent: a half-open integer
span on one edit source, one frozen intent, a deterministic identity computed
in :mod:`services.editorial.candidate_ids` (NEVER accepted from outside), a
parent chain for adjusted spans, deterministic redundancy grouping for
overlapping keeps, and at least one evidence artifact ref. Floats cannot be
expressed anywhere. The Selection Plan Proposal is a proposal-only envelope —
commit authority belongs to Todo 41.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactRef,
    Frame,
    Identifier,
    PositiveInteger,
    Sha256,
    SourceId,
    StrictModel,
)
from services.editorial.candidate_ids import compute_candidate_id
from services.fixtures.manifest_phase1 import (  # noqa: TC001 (pydantic runtime fields)
    DurationBudgetRules,
    OrderingRules,
)

SegmentKind = Literal["speech", "pause", "filler", "false_start"]
CandidateIntent = Literal["remove", "keep", "adjust", "subtitle"]
FROZEN_CANDIDATE_INTENTS: Final[frozenset[CandidateIntent]] = frozenset(
    ("remove", "keep", "adjust", "subtitle")
)


def _frame_ms(frame: int, rate_num: int, rate_den: int) -> int:
    return frame * 1000 * rate_den // rate_num


class CandidateSpan(StrictModel):
    """Half-open integer frame span with a deterministic millisecond mirror."""

    start_frame: Frame
    end_frame: Frame
    rate_num: PositiveInteger
    rate_den: PositiveInteger
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward_and_mirrored(self) -> CandidateSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "spans are non-empty half-open ranges")
        if self.start_ms != _frame_ms(
            self.start_frame, self.rate_num, self.rate_den
        ) or self.end_ms != _frame_ms(self.end_frame, self.rate_num, self.rate_den):
            raise PydanticCustomError("span_mirror", "ms mirrors must equal frame*1000*den/num")
        return self

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame

    def overlaps(self, other: CandidateSpan) -> bool:
        return self.start_frame < other.end_frame and other.start_frame < self.end_frame


class CandidateSourceRef(StrictModel):
    source_id: SourceId
    edit_source_sha: Sha256


class Handles(StrictModel):
    """Head/tail handles from the declared structure (ints; Phase 1: none)."""

    head_frames: int = Field(default=0, ge=0, strict=True)
    tail_frames: int = Field(default=0, ge=0, strict=True)


class CandidateProvenance(StrictModel):
    analyzer_version: str = Field(min_length=1, strict=True)
    rule_ids: tuple[str, ...] = Field(min_length=1)


class Candidate(StrictModel):
    """One evidence-backed editorial candidate with a deterministic identity."""

    candidate_id: Sha256
    source_ref: CandidateSourceRef
    span: CandidateSpan
    intent: CandidateIntent
    analyzer_version: str = Field(min_length=1, strict=True)
    parent_candidate_id: Sha256 | None = None
    duration_frames: int = Field(gt=0, strict=True)
    handles: Handles = Field(default_factory=Handles)
    redundancy_group: str | None = None
    evidence: tuple[ArtifactRef, ...] = Field(min_length=1)
    provenance: CandidateProvenance

    @model_validator(mode="after")
    def require_derived_identity(self) -> Candidate:
        if self.analyzer_version != self.provenance.analyzer_version:
            raise PydanticCustomError(
                "provenance_mismatch", "analyzer_version must equal provenance version"
            )
        if self.duration_frames != self.span.length:
            raise PydanticCustomError(
                "duration_mismatch", "duration_frames must equal the span length"
            )
        expected = compute_candidate_id(
            edit_source_sha=self.source_ref.edit_source_sha,
            source_id=self.source_ref.source_id,
            start_frame=self.span.start_frame,
            end_frame=self.span.end_frame,
            rate_num=self.span.rate_num,
            rate_den=self.span.rate_den,
            intent=self.intent,
            analyzer_version=self.analyzer_version,
        )
        if self.candidate_id != expected:
            raise PydanticCustomError(
                "identity_binding", "candidate_id must equal the deterministic identity hash"
            )
        return self


def derive_candidate(  # noqa: PLR0913 (candidate fields are the record)
    *,
    source_ref: CandidateSourceRef,
    span: CandidateSpan,
    intent: CandidateIntent,
    analyzer_version: str,
    evidence: tuple[ArtifactRef, ...],
    provenance: CandidateProvenance,
    parent_candidate_id: Sha256 | None = None,
    handles: Handles | None = None,
    redundancy_group: str | None = None,
) -> Candidate:
    """Compute the ID from the normalized fields, then construct and re-check."""

    return Candidate(
        candidate_id=compute_candidate_id(
            edit_source_sha=source_ref.edit_source_sha,
            source_id=source_ref.source_id,
            start_frame=span.start_frame,
            end_frame=span.end_frame,
            rate_num=span.rate_num,
            rate_den=span.rate_den,
            intent=intent,
            analyzer_version=analyzer_version,
        ),
        source_ref=source_ref,
        span=span,
        intent=intent,
        analyzer_version=analyzer_version,
        parent_candidate_id=parent_candidate_id,
        duration_frames=span.length,
        handles=handles if handles is not None else Handles(),
        redundancy_group=redundancy_group,
        evidence=evidence,
        provenance=provenance,
    )


class AnalyzerSegmentRecord(StrictModel):
    """Analyzer-side input record; adjusted spans reference their base segment."""

    segment_id: Identifier
    kind: SegmentKind
    span: CandidateSpan
    evidence: tuple[ArtifactRef, ...] = Field(min_length=1)
    provenance: CandidateProvenance
    adjusted_from: Identifier | None = None


class CandidatePool(StrictModel):
    """Evidence-backed analyzer inputs bound to exactly one edit source."""

    source_id: SourceId
    edit_source_sha: Sha256
    total_frames: int = Field(gt=0, strict=True)
    segments: tuple[AnalyzerSegmentRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_resolvable_adjustments(self) -> CandidatePool:
        bases = [record for record in self.segments if record.adjusted_from is None]
        base_ids = {record.segment_id for record in bases}
        if len(base_ids) != len(bases):
            raise PydanticCustomError("duplicate_base", "base segment ids must be unique")
        kinds = {record.segment_id: record.kind for record in bases}
        for record in self.segments:
            if record.adjusted_from is None:
                continue
            if record.adjusted_from not in base_ids:
                raise PydanticCustomError(
                    "adjusted_parent_missing", "adjusted records need a base in the pool"
                )
            if record.kind != kinds[record.adjusted_from]:
                raise PydanticCustomError(
                    "adjusted_kind", "adjusted records keep the base segment kind"
                )
        return self


class EvidenceIndex(StrictModel):
    """Resolved evidence artifacts plus the edit-source binding they attest."""

    rows: tuple[ArtifactRef, ...] = Field(min_length=1)
    edit_source_sha: Sha256

    @model_validator(mode="after")
    def require_unique_artifact_ids(self) -> EvidenceIndex:
        ids = [row.artifact_id for row in self.rows]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_artifact", "evidence rows must carry unique artifact ids"
            )
        return self

    def sha_for(self, artifact_id: str) -> Sha256 | None:
        for row in self.rows:
            if row.artifact_id == artifact_id:
                return row.sha256
        return None


class ProposalProducer(StrictModel):
    """Producing model role and the prompt contract version it ran under."""

    model_role_id: Identifier
    contract_version: str = Field(min_length=1, strict=True)


class SelectionPlanProposal(StrictModel):
    """Proposal-only selection plan envelope; commit authority is Todo 41."""

    schema_version: Literal["selection-plan-proposal-v1"] = "selection-plan-proposal-v1"
    proposal_id: Identifier
    episode_id: Identifier
    candidates: tuple[Candidate, ...] = Field(min_length=1)
    must_include: tuple[Sha256, ...] = ()
    budget: DurationBudgetRules
    ordering: OrderingRules
    plan_base_version: Identifier
    producer: ProposalProducer
    fixture_only: bool

    @model_validator(mode="after")
    def require_must_include_keeps(self) -> SelectionPlanProposal:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_identity", "candidate identities are unique")
        if len(set(self.must_include)) != len(self.must_include):
            raise PydanticCustomError("duplicate_must", "must_include ids must be unique")
        keeps = {
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.intent == "keep"
        }
        if set(self.must_include) - keeps:
            raise PydanticCustomError(
                "must_include_not_kept", "every must_include id must be a keep candidate"
            )
        return self


__all__ = [
    "FROZEN_CANDIDATE_INTENTS",
    "AnalyzerSegmentRecord",
    "Candidate",
    "CandidateIntent",
    "CandidatePool",
    "CandidateProvenance",
    "CandidateSourceRef",
    "CandidateSpan",
    "EvidenceIndex",
    "Handles",
    "ProposalProducer",
    "SegmentKind",
    "SelectionPlanProposal",
    "derive_candidate",
]
