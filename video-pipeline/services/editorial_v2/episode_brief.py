"""Episode Brief V1 — compact human-approved episode intent artifact.

PRD 8.3 required fields:
    audience_hypothesis, viewer_promise, episode_objective,
    must_include (ideas/moments, min_length=1),
    must_not_misrepresent (min_length=1),
    target_duration_minutes {min, max} bounds-validated,
    cta (optional), pacing_target, editing_intensity,
    required_assets, publication_constraints.
Plus approval state machine (draft → proposed → approved) and downstream gate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel, to_tuple

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class MustIncludeEntry(StrictModel):
    idea_or_moment: Annotated[str, Field(min_length=1, strict=True)]
    note: Annotated[str, Field(min_length=1, strict=True)] | None = None


class TargetDurationMinutes(StrictModel):
    min: Annotated[int, Field(gt=0, strict=True)]
    max: Annotated[int, Field(gt=0, strict=True)]

    @model_validator(mode="after")
    def require_bounds(self) -> TargetDurationMinutes:
        if self.max < self.min:
            raise PydanticCustomError(
                "duration_bounds_inverted",
                "target_duration_minutes.max must be >= min",
            )
        return self


class EpisodeBriefApprovalRef(StrictModel):
    """Approval reference compatible with services/approvals record shape.

    Minimal purpose-bound reference: an operator checkpoint that targeted
    the brief's content hash. Mirrors the minimal ``ApprovalRef`` consumed
    by ``services.job_runner.cas`` (purpose / target_hash / artifact_ref)
    and carries the ``record_id`` / ``decision`` fields of
    ``services.approvals.models.OperationRecord`` so offline audit can
    correlate it with the append-only operation log without modifying that
    service.

    Fields:
        record_id: correlates to ``OperationRecord.record_id`` (opr-XXXXXXXX).
        purpose: always "editorial" — the approvals service binds purpose
                 to target type.
        target_type: logical target type for this artifact.
        target_hash: sha256 of the canonical brief bytes (with this ref
                     zeroed, matching the artifact_content_hash convention).
        decision: always "approve".
        actor_id: operator who approved (None for synthetic/fixture use).
    """

    record_id: Identifier
    purpose: Literal["editorial"] = "editorial"
    target_type: Literal["episode-brief"] = "episode-brief"
    target_hash: Sha256
    decision: Literal["approve"] = "approve"
    actor_id: Identifier | None = None


BriefStatus = Literal["draft", "proposed", "approved"]


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class EpisodeBriefTransitionError(ValueError):
    """Typed transition refusal (e.g. draft→approved)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class EpisodeBriefNotApprovedError(ValueError):
    """Downstream gate: brief is not approved."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Main artifact
# ---------------------------------------------------------------------------


class EpisodeBriefV1(StrictModel):
    schema_version: Literal["episode-brief-v1"] = "episode-brief-v1"
    episode_id: Identifier
    status: BriefStatus = "draft"

    # PRD 8.3 required fields
    audience_hypothesis: Annotated[str, Field(min_length=1, strict=True)]
    viewer_promise: Annotated[str, Field(min_length=1, strict=True)]
    episode_objective: Annotated[str, Field(min_length=1, strict=True)]
    must_include: Annotated[
        tuple[MustIncludeEntry, ...], BeforeValidator(to_tuple)
    ] = Field(min_length=1)
    must_not_misrepresent: Annotated[
        tuple[Annotated[str, Field(min_length=1, strict=True)], ...],
        BeforeValidator(to_tuple),
    ] = Field(min_length=1)
    target_duration_minutes: TargetDurationMinutes
    cta: Annotated[str, Field(min_length=1, strict=True)] | None = None
    pacing_target: Annotated[str, Field(min_length=1, strict=True)]
    editing_intensity: Annotated[str, Field(min_length=1, strict=True)]
    required_assets: Annotated[
        tuple[Annotated[str, Field(min_length=1, strict=True)], ...],
        BeforeValidator(to_tuple),
    ] = Field(default_factory=tuple)
    publication_constraints: Annotated[
        tuple[Annotated[str, Field(min_length=1, strict=True)], ...],
        BeforeValidator(to_tuple),
    ] = Field(default_factory=tuple)

    # Approval reference — only present when status == "approved"
    approval_ref: EpisodeBriefApprovalRef | None = None

    @model_validator(mode="after")
    def enforce_approval_invariant(self) -> EpisodeBriefV1:
        if self.status == "approved" and self.approval_ref is None:
            raise PydanticCustomError(
                "approval_ref_missing",
                "approved briefs must carry an approval_ref",
            )
        if self.status != "approved" and self.approval_ref is not None:
            raise PydanticCustomError(
                "approval_ref_unexpected",
                "only approved briefs may carry an approval_ref",
            )
        # Per-element whitespace check — tuple element Field(min_length=1)
        # already rejects empty, but whitespace-only needs explicit check.
        for entry in self.must_not_misrepresent:
            if not entry.strip():
                raise PydanticCustomError(
                    "must_not_misrepresent_empty",
                    "must_not_misrepresent entries must be non-empty",
                )
        for entry in self.required_assets:
            if not entry.strip():
                raise PydanticCustomError(
                    "required_assets_empty",
                    "required_assets entries must be non-empty",
                )
        for entry in self.publication_constraints:
            if not entry.strip():
                raise PydanticCustomError(
                    "publication_constraints_empty",
                    "publication_constraints entries must be non-empty",
                )
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _brief_content_hash(brief: EpisodeBriefV1) -> str:
    """Canonical sha256 of brief bytes with approval_ref zeroed.

    The approved brief's own approval_ref.target_hash covers this hash,
    so hashing must exclude the ref to avoid self-reference.
    """

    # Zero the approval_ref before hashing (genesis-like convention)
    payload = brief.model_copy(update={"approval_ref": None})
    # Exclude approval_ref from bytes when it is None — but we want stable
    # bytes: model_dump with approval_ref None is stable.
    canonical = json.dumps(
        payload.model_dump(mode="json", by_alias=True, exclude_none=False),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    # Reject floats (should not occur but mirror serialization convention)
    # Already ensured by strict ints; just hash.
    return hashlib.sha256(canonical).hexdigest()


def propose(brief: EpisodeBriefV1) -> EpisodeBriefV1:
    """Transition draft → proposed.

    Only draft → proposed is legal. Any other source status raises a typed
    error. The returned brief is a new frozen instance with status proposed.
    """

    if brief.status != "draft":
        raise EpisodeBriefTransitionError(
            "illegal-transition",
            f"propose() requires status draft, got {brief.status}",
        )
    return brief.model_copy(update={"status": "proposed"})


def approve(
    brief: EpisodeBriefV1,
    *,
    record_id: Identifier | None = None,
    actor_id: Identifier | None = None,
) -> EpisodeBriefV1:
    """Transition proposed → approved with an approvals-compatible ref.

    Only proposed → approved is legal; draft → approved (or already
    approved → approved) raises ``EpisodeBriefTransitionError`` with code
    ``illegal-transition``. On success the returned brief carries
    ``status == approved`` and an ``EpisodeBriefApprovalRef`` whose
    ``target_hash`` is the canonical sha256 of the brief's content.

    Args:
        brief: The proposed brief to approve.
        record_id: Optional explicit record id (default synthetic
                   ``opr-XXXXXXXX`` derived from hash prefix, compatible
                   with ``services.approvals.models.record_id_for_seq`` shape).
        actor_id: Optional operator identifier for the approval.
    """

    if brief.status != "proposed":
        raise EpisodeBriefTransitionError(
            "illegal-transition",
            f"approve() requires status proposed, got {brief.status}",
        )
    content_hash = _brief_content_hash(brief)
    # Synthetic record_id if not supplied — deterministic from hash prefix
    synthetic_id: Identifier = record_id if record_id is not None else f"opr-{content_hash[:8]}"
    ref = EpisodeBriefApprovalRef(
        record_id=synthetic_id,
        target_hash=content_hash,  # type: ignore[arg-type]  # validated as Sha256 via pattern
        actor_id=actor_id,
    )
    return brief.model_copy(update={"status": "approved", "approval_ref": ref})


def require_approved(brief: EpisodeBriefV1) -> EpisodeBriefV1:
    """Downstream gate: raise typed error unless brief is approved."""

    if brief.status != "approved" or brief.approval_ref is None:
        raise EpisodeBriefNotApprovedError(
            "not-approved",
            f"downstream stage requires approved brief, got status {brief.status}",
        )
    return brief


__all__ = [
    "BriefStatus",
    "EpisodeBriefApprovalRef",
    "EpisodeBriefNotApprovedError",
    "EpisodeBriefTransitionError",
    "EpisodeBriefV1",
    "MustIncludeEntry",
    "TargetDurationMinutes",
    "approve",
    "propose",
    "require_approved",
]
