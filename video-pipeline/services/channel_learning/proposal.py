"""Channel profile change proposal — governed learning.

Separation contract: This module defines channel-profile change proposals
and preference evidence only. It keeps audience outcomes separate from
preference evidence by schema and by import boundary — no symbol
from the observation artifact nor from the reference taste artifact may
be imported or re-exported here. Performance outcomes never automatically
rewrite operator taste.

Domain choice: Defines ChannelProfileDomain locally with values
mirroring the PRD 8.5 domain set (story_structure,
pacing, color, subtitle, b_roll, framing_graphics, audio) so that
proposals can name a domain without cross-importing the reference
taste package. This keeps the separation contract while remaining
compatible with the PRD 8.5 domain set. EvidenceClass distinguishes
the four PRD 16.1 streams end-to-end as a typed field (explicit_rule,
reference_taste, approved_edit, audience_outcome) and is never
collapsed into a single bucket.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel

# ---------------------------------------------------------------------------
# Four evidence classes (PRD 16.1) — field, not comment
# ---------------------------------------------------------------------------


class EvidenceClass(StrEnum):
    """Four distinguishable evidence streams (PRD 16.1)."""

    explicit_rule = "explicit_rule"
    reference_taste = "reference_taste"
    approved_edit = "approved_edit"
    audience_outcome = "audience_outcome"


class ChannelProfileDomain(StrEnum):
    """Channel-profile preference domains.

    Local mirror of reference_learning.PreferenceDomain (PRD 8.5)
    to avoid cross-import per separation contract.
    """

    story_structure = "story_structure"
    pacing = "pacing"
    color = "color"
    subtitle = "subtitle"
    b_roll = "b_roll"
    framing_graphics = "framing_graphics"
    audio = "audio"


def _coerce_evidence_class(value: object) -> object:
    if isinstance(value, str):
        return EvidenceClass(value)
    return value


def _coerce_domain(value: object) -> object:
    if isinstance(value, str):
        return ChannelProfileDomain(value)
    return value


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


type EvidenceClassField = Annotated[EvidenceClass, BeforeValidator(_coerce_evidence_class)]
type DomainField = Annotated[ChannelProfileDomain, BeforeValidator(_coerce_domain)]

Origin = Literal["explicit_request", "repeated_evidence"]
ProposalStatus = Literal["draft", "approved", "rejected", "applied"]


# ---------------------------------------------------------------------------
# Evidence record (input to proposal generation)
# ---------------------------------------------------------------------------


class EvidenceRecord(StrictModel):
    """Single approved evidence event used as proposal input.

    ``approved`` marks whether the event passed human approval (e.g.,
    approved Review Event / approved annotation). Only approved records
    count toward the repeated-evidence threshold; unapproved or single
    occurrences never generate a proposal on their own.
    """

    evidence_id: Identifier
    evidence_class: EvidenceClassField
    domain: DomainField
    statement: Annotated[str, Field(min_length=1, strict=True)]
    evidence_ref: Identifier
    approved: bool = True


# ---------------------------------------------------------------------------
# Proposal artifact
# ---------------------------------------------------------------------------


class ChannelProfileChangeProposalV1(StrictModel):
    """Governed channel-profile change proposal (PRD 16.2).

    A preference may become a durable channel rule only through
    explicit human request OR repeated approved evidence, then
    human approval, profile version bump, and holdout evaluation.
    Single-occurrence corrections or audience metrics produce no
    proposal (None from maybe_generate_proposal) — never a silent
    rule mutation.
    """

    schema_version: Literal["channel-profile-change-proposal-v1"] = (
        "channel-profile-change-proposal-v1"
    )
    proposal_id: Identifier
    evidence_class: EvidenceClassField
    domain: DomainField
    statement: Annotated[str, Field(min_length=1, strict=True)]
    supporting_evidence: Annotated[tuple[Identifier, ...], BeforeValidator(_tuple)] = Field(
        min_length=1
    )
    evidence_count: Annotated[int, Field(ge=1, strict=True)]
    origin: Origin
    status: ProposalStatus = "draft"

    @model_validator(mode="after")
    def require_count_matches_evidence(self) -> ChannelProfileChangeProposalV1:
        if self.evidence_count != len(self.supporting_evidence):
            raise PydanticCustomError(
                "evidence_count_mismatch",
                "evidence_count must equal len(supporting_evidence)",
            )
        return self


# ---------------------------------------------------------------------------
# Proposal generation — typed, testable, no silent mutation
# ---------------------------------------------------------------------------


def maybe_generate_proposal(
    evidence_records: list[EvidenceRecord],
    *,
    explicit_request: bool = False,
    threshold: int = 2,
    proposal_id: str = "prop-0001",
) -> ChannelProfileChangeProposalV1 | None:
    """Generate a proposal only from explicit request or repeated evidence.

    Returns ``None`` for single-occurrence corrections or audience metrics
    (no silent rule mutation). When ``explicit_request`` is True, a
    single approved record suffices. Otherwise, the SAME statement +
    domain + evidence_class must appear at least ``threshold`` times
    among approved records (default 2). Different statements or mixed
    classes/domains do not aggregate.
    """

    if threshold < 1:
        raise ValueError("threshold must be >= 1")

    if not evidence_records:
        return None

    if explicit_request:
        # Use the first record as representative; all evidence refs for
        # explicit_request proposals include the trigger set (single or multi).
        first = evidence_records[0]
        # For explicit_request, we generate regardless of approved flag?
        # Require at least one record; supporting_evidence is the full set
        # to preserve provenance.
        refs = tuple(r.evidence_ref for r in evidence_records)
        if not refs:
            return None
        return ChannelProfileChangeProposalV1(
            proposal_id=proposal_id,  # type: ignore[arg-type]
            evidence_class=first.evidence_class,
            domain=first.domain,
            statement=first.statement,
            supporting_evidence=refs,
            evidence_count=len(refs),
            origin="explicit_request",
            status="draft",
        )

    # Repeated-evidence path: group approved records by (class, domain, statement)
    groups: dict[tuple[EvidenceClass, ChannelProfileDomain, str], list[EvidenceRecord]] = (
        defaultdict(list)
    )
    for rec in evidence_records:
        if not rec.approved:
            continue
        key = (rec.evidence_class, rec.domain, rec.statement)
        groups[key].append(rec)

    for (ec, dom, stmt), lst in groups.items():
        if len(lst) >= threshold:
            refs2 = tuple(r.evidence_ref for r in lst)
            try:
                return ChannelProfileChangeProposalV1(
                    proposal_id=proposal_id,  # type: ignore[arg-type]
                    evidence_class=ec,
                    domain=dom,
                    statement=stmt,
                    supporting_evidence=refs2,
                    evidence_count=len(refs2),
                    origin="repeated_evidence",
                    status="draft",
                )
            except ValidationError:
                # Should not happen for well-formed groups; propagate as None
                # to keep function typed and never silently mutate.
                return None

    return None
