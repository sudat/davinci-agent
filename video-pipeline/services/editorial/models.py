"""Request/record models for the Editorial Director boundary (Todo 39).

The Director consumes declared, frozen inputs (candidates + rule spec from
the Phase-1 fixture manifest family) plus bounded evidence from the Todo-37
MediaQueryApi, and ALWAYS emits proposals inside the Todo-32 frozen contract.
Nothing in this module can express a commit, an approval, a Resolve
instruction, or a credential value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.editorial_model import (  # noqa: TC001 (pydantic runtime fields)
    EditorialRequestEnvelope,
    EditorialSelectionProposal,
)
from services.contracts.primitives import Identifier, StrictModel
from services.fixtures.manifest_phase1 import (  # noqa: TC001 (pydantic runtime fields)
    EditorialRules,
    EditSourceSpec,
)


class DeclaredCandidate(StrictModel):
    """One declared candidate from the frozen manifest transcript (evidence)."""

    segment_id: Identifier
    kind: Literal["speech", "pause", "filler", "false_start"]
    text: str
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)
    content_score: int = Field(ge=0, le=10, strict=True)
    clarity_score: int = Field(ge=0, le=10, strict=True)
    pause_ms: int = Field(ge=0, strict=True)
    retake_group: Identifier | None = None

    @model_validator(mode="after")
    def require_forward_span(self) -> DeclaredCandidate:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "span_empty", "declared candidate spans are non-empty half-open ranges"
            )
        return self


class DirectorRequest(StrictModel):
    """Declared inputs for one editorial-director run (frozen Phase-1 shape)."""

    episode_id: Identifier
    edit_source: EditSourceSpec
    rules: EditorialRules
    candidates: tuple[DeclaredCandidate, ...] = Field(min_length=1)


class EditorialPolicyEnvelope(StrictModel):
    """Explicit data-policy envelope recorded with every run (Todo 12 + pin).

    ``binding_scope`` distinguishes the frozen spike adapter (fixture binding;
    only the five Phase-1 synthetic episodes may leave the local lane) from
    the production path (the operator's resolved production policy is the
    granting authority; deny-by-default through the Todo-12 gate).
    """

    fixture_binding: Literal["granted", "denied"]
    binding_reason: str = Field(min_length=1)
    control_plane_decision: Literal["allow", "deny"]
    control_plane_reason: str = Field(min_length=1)
    binding_scope: Literal["fixture-binding", "production-policy"] = "fixture-binding"

    @property
    def allowed(self) -> bool:
        if self.binding_scope == "production-policy":
            return self.control_plane_decision == "allow"
        return self.fixture_binding == "granted" and self.control_plane_decision == "allow"


class EditorialMetadata(StrictModel):
    """Requested/observed model and request metadata (no credential values)."""

    pin_version: str
    requested_model: str
    observed_model: str | None = None
    transport_kind: Literal["replay", "live-stub", "live-http"]
    prompt_bundle_hash: str
    evidence_lineage: tuple[str, ...] = ()


EditorialErrorCode = Literal[
    "local_only_denial",
    "policy_source_error",
    "evidence_incomplete",
    "transport_failure",
    "truncated_response",
    "malformed_json",
    "arbitrary_tool_claim",
    "commit_field_forbidden",
    "proposal_schema_violation",
    "hallucinated_reference",
]


class EditorialErrorRecord(StrictModel):
    code: EditorialErrorCode
    detail: str = Field(min_length=1)
    transport_code: str | None = None


class DirectorRunResult(StrictModel):
    """Proposal-only outcome; every status is fully structured, never partial."""

    envelope: EditorialRequestEnvelope
    policy: EditorialPolicyEnvelope
    metadata: EditorialMetadata
    proposal: EditorialSelectionProposal | None = None
    refusal_reason: str | None = None
    error: EditorialErrorRecord | None = None

    @model_validator(mode="after")
    def require_status_consistency(self) -> DirectorRunResult:
        status = self.envelope.status
        if status == "proposal" and (
            self.proposal is None or self.refusal_reason is not None or self.error is not None
        ):
            raise PydanticCustomError(
                "status_mismatch", "proposal status requires exactly a proposal"
            )
        if status == "refusal" and (
            self.refusal_reason is None or self.proposal is not None or self.error is not None
        ):
            raise PydanticCustomError(
                "status_mismatch", "refusal status requires exactly a refusal reason"
            )
        if status == "error" and (
            self.error is None or self.proposal is not None or self.refusal_reason is not None
        ):
            raise PydanticCustomError("status_mismatch", "error status requires exactly an error")
        return self


__all__ = [
    "DeclaredCandidate",
    "DirectorRequest",
    "DirectorRunResult",
    "EditorialErrorCode",
    "EditorialErrorRecord",
    "EditorialMetadata",
    "EditorialPolicyEnvelope",
]
