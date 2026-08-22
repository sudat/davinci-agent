"""Reference learning schemas — v43 task 24.

Five first-class artifacts (all StrictModel):

- ReferenceSourceV1
- PreferenceDomain (enum of PRD 8.5)
- ReferenceAnnotationV1 (with hard domain-scoping rule)
- PairwisePreferenceV1
- DerivedTasteProfileV1 (entries + explicit contradictions)
- ReferenceLibraryV1 (versioned collection)

Hard rule is enforced in the model validator, not only in tests:
any domain NOT in ``named_domains`` must carry polarity ``unspecified``
(or be absent); a non-unspecified polarity on an unnamed domain is a
validation error.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Producer, Sha256, StrictModel


class PreferenceDomain(StrEnum):
    """Preference domains that may be named by the operator (PRD 8.5)."""

    story_structure = "story_structure"
    pacing = "pacing"
    color = "color"
    subtitle = "subtitle"
    b_roll = "b_roll"
    framing_graphics = "framing_graphics"
    audio = "audio"


Polarity = Literal["like", "dislike", "neutral", "unspecified"]
Scope = Literal["channel", "series", "episode"]
HumanApproval = Literal["pending", "approved", "rejected"]
PairwiseChoice = Literal["a", "b"]
EvidenceSourceKind = Literal[
    "explicit_rule",
    "reference_annotation",
    "approved_edit",
    "negative_example",
    "pairwise",
]
SourceKind = Literal["local_file", "youtube_url", "owned_render", "timestamp_range"]


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _tuple_domains(value: object) -> object:
    if isinstance(value, list):
        out: list[PreferenceDomain] = []
        for item in value:
            if isinstance(item, str):
                out.append(PreferenceDomain(item))
            else:
                out.append(item)  # type: ignore[arg-type]
        return tuple(out)
    return value


def _coerce_domains_dict(value: object) -> object:
    if isinstance(value, dict):
        coerced: dict[PreferenceDomain, Polarity] = {}
        for k, v in value.items():  # type: ignore[union-attr]
            key = PreferenceDomain(k) if isinstance(k, str) else k
            coerced[key] = v  # type: ignore[index]
        return coerced
    return value


def _coerce_feature_dict(value: object) -> object:
    if isinstance(value, dict):
        coerced: dict[PreferenceDomain, dict[str, str]] = {}
        for k, v in value.items():  # type: ignore[union-attr]
            key = PreferenceDomain(k) if isinstance(k, str) else k
            coerced[key] = v  # type: ignore[index]
        return coerced
    return value


def _coerce_domain(value: object) -> object:
    if isinstance(value, str):
        return PreferenceDomain(value)
    return value


type DomainField = Annotated[PreferenceDomain, BeforeValidator(_coerce_domain)]


def _check_duplicate_named(named: tuple[PreferenceDomain, ...]) -> None:
    if len(set(named)) != len(named):
        raise PydanticCustomError(
            "duplicate_named_domain", "named_domains must be unique"
        )


def _check_polarity_without_domain(
    named_set: set[PreferenceDomain],
    domains: dict[PreferenceDomain, Polarity],
) -> None:
    if not named_set:
        for domain, polarity in domains.items():
            if polarity != "unspecified":
                raise PydanticCustomError(
                    "polarity_without_domain",
                    "polarity {pol} on {domain} but no domain was named",
                    {"pol": polarity, "domain": str(domain)},
                )


def _check_unnamed_polarity(
    named_set: set[PreferenceDomain],
    domains: dict[PreferenceDomain, Polarity],
) -> None:
    for domain, polarity in domains.items():
        if domain not in named_set and polarity != "unspecified":
            raise PydanticCustomError(
                "domain_not_named",
                "domain {domain} has polarity {pol} but was not named",
                {"domain": str(domain), "pol": polarity},
            )


def _check_named_entries(
    named_set: set[PreferenceDomain],
    domains: dict[PreferenceDomain, Polarity],
) -> None:
    for named in named_set:
        polarity = domains.get(named)
        if polarity is None:
            raise PydanticCustomError(
                "named_domain_missing",
                "named domain {domain} has no polarity entry",
                {"domain": str(named)},
            )
        if polarity == "unspecified":
            raise PydanticCustomError(
                "named_domain_unspecified",
                "named domain {domain} must not be unspecified",
                {"domain": str(named)},
            )


def _check_feature_scoping(
    named_set: set[PreferenceDomain],
    features: dict[PreferenceDomain, dict[str, str]],
) -> None:
    for fdom in features:
        if fdom not in named_set:
            raise PydanticCustomError(
                "feature_domain_not_named",
                "feature domain {domain} was not named",
                {"domain": str(fdom)},
            )

# ---------------------------------------------------------------------------
# Shared small models
# ---------------------------------------------------------------------------


class ReferenceTimeRangeV1(StrictModel):
    """Optional timestamp range within a reference source."""

    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward_range(self) -> ReferenceTimeRangeV1:
        if self.end_ms <= self.start_ms:
            raise PydanticCustomError(
                "time_range_empty",
                "time range must be a non-empty half-open interval",
            )
        return self


# ---------------------------------------------------------------------------
# ReferenceSourceV1
# ---------------------------------------------------------------------------


class ReferenceSourceV1(StrictModel):
    """One reference source (PRD 8.4)."""

    schema_version: Literal["reference-source-v1"] = "reference-source-v1"
    source_id: Identifier
    kind: SourceKind
    location: Annotated[str, Field(min_length=1, strict=True)]
    created_at: Annotated[str, Field(min_length=1, strict=True)]
    provenance: Producer
    sha256: Sha256 | None = None
    parent_source_id: Identifier | None = None
    time_range: ReferenceTimeRangeV1 | None = None

    @model_validator(mode="after")
    def require_timestamp_consistency(self) -> ReferenceSourceV1:
        is_range = self.kind == "timestamp_range"
        if is_range:
            if self.parent_source_id is None or self.time_range is None:
                raise PydanticCustomError(
                    "timestamp_range_requires_parent",
                    "timestamp_range kind requires parent_source_id and time_range",
                )
        elif self.parent_source_id is not None or self.time_range is not None:
            raise PydanticCustomError(
                "timestamp_range_exclusive",
                "only timestamp_range kind may carry parent/time_range",
            )
        return self


# ---------------------------------------------------------------------------
# ReferenceAnnotationV1 — hard domain-scoping rule
# ---------------------------------------------------------------------------


class ReferenceAnnotationV1(StrictModel):
    """Operator annotation over a reference source (PRD 8.4).

    The hard scoping rule is validated in ``enforce_domain_scoping``.
    """

    schema_version: Literal["reference-annotation-v1"] = "reference-annotation-v1"
    annotation_id: Identifier
    source_id: Identifier
    time_range: ReferenceTimeRangeV1 | None = None
    named_domains: Annotated[
        tuple[PreferenceDomain, ...], BeforeValidator(_tuple_domains)
    ] = ()
    domains: Annotated[
        dict[PreferenceDomain, Polarity], BeforeValidator(_coerce_domains_dict)
    ] = Field(default_factory=dict)
    rationale: Annotated[str, Field(min_length=1, strict=True)]
    extracted_features: Annotated[
        dict[PreferenceDomain, dict[str, str]],
        BeforeValidator(_coerce_feature_dict),
    ] = Field(default_factory=dict)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    scope: Scope
    provenance: Producer
    human_approval: HumanApproval = "pending"

    @model_validator(mode="after")
    def enforce_domain_scoping(self) -> ReferenceAnnotationV1:
        _check_duplicate_named(self.named_domains)
        named_set = set(self.named_domains)
        _check_polarity_without_domain(named_set, self.domains)
        _check_unnamed_polarity(named_set, self.domains)
        _check_named_entries(named_set, self.domains)
        _check_feature_scoping(named_set, self.extracted_features)
        return self


# ---------------------------------------------------------------------------
# PairwisePreferenceV1
# ---------------------------------------------------------------------------


class PairwisePreferenceV1(StrictModel):
    """Domain-specific A/B preference (PRD 8.7)."""

    schema_version: Literal["pairwise-preference-v1"] = "pairwise-preference-v1"
    pairwise_id: Identifier
    domain: DomainField
    choice: PairwiseChoice
    reference_a_id: Identifier
    reference_b_id: Identifier
    reason: Annotated[str, Field(min_length=1, strict=True)]
    provenance: Producer
    created_at: Annotated[str, Field(min_length=1, strict=True)]

    @model_validator(mode="after")
    def require_distinct_refs(self) -> PairwisePreferenceV1:
        if self.reference_a_id == self.reference_b_id:
            raise PydanticCustomError(
                "pairwise_same_ref",
                "pairwise refs must be distinct",
            )
        return self


# ---------------------------------------------------------------------------
# DerivedTasteProfileV1
# ---------------------------------------------------------------------------


class DerivedTasteEntryV1(StrictModel):
    """One aggregated taste statement with required citations."""

    domain: DomainField
    statement: Annotated[str, Field(min_length=1, strict=True)]
    polarity: Polarity
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    evidence_refs: Annotated[tuple[Identifier, ...], BeforeValidator(_tuple)] = Field(
        min_length=1
    )
    source_kind: EvidenceSourceKind


class ContradictionRecordV1(StrictModel):
    """Explicit contradiction — never silently averaged (PRD 8.6)."""

    domain: DomainField
    description: Annotated[str, Field(min_length=1, strict=True)]
    conflicting_refs: Annotated[tuple[Identifier, ...], BeforeValidator(_tuple)] = Field(
        min_length=2
    )
    note: str | None = None


class DerivedTasteProfileV1(StrictModel):
    """Aggregated, explainable taste profile (PRD 8.6)."""

    schema_version: Literal["derived-taste-profile-v1"] = "derived-taste-profile-v1"
    profile_id: Identifier
    entries: Annotated[tuple[DerivedTasteEntryV1, ...], BeforeValidator(_tuple)] = ()
    unresolved_contradictions: Annotated[
        tuple[ContradictionRecordV1, ...], BeforeValidator(_tuple)
    ] = ()
    provenance: Producer
    created_at: Annotated[str, Field(min_length=1, strict=True)]


# ---------------------------------------------------------------------------
# ReferenceLibraryV1
# ---------------------------------------------------------------------------


class ReferenceLibraryV1(StrictModel):
    """Versioned collection of sources + annotation ids (PRD 8.8)."""

    schema_version: Literal["reference-library-v1"] = "reference-library-v1"
    library_id: Identifier
    version: int = Field(ge=1, strict=True)
    sources: Annotated[tuple[ReferenceSourceV1, ...], BeforeValidator(_tuple)] = ()
    annotation_ids: Annotated[tuple[Identifier, ...], BeforeValidator(_tuple)] = ()
    provenance: Producer
    created_at: Annotated[str, Field(min_length=1, strict=True)]

    @model_validator(mode="after")
    def require_unique_sources(self) -> ReferenceLibraryV1:
        ids = [s.source_id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_source",
                "library sources must have unique source_id",
            )
        if len(self.annotation_ids) != len(set(self.annotation_ids)):
            raise PydanticCustomError(
                "duplicate_annotation",
                "annotation_ids must be unique",
            )
        return self


__all__ = [
    "ContradictionRecordV1",
    "DerivedTasteEntryV1",
    "DerivedTasteProfileV1",
    "EvidenceSourceKind",
    "HumanApproval",
    "PairwiseChoice",
    "PairwisePreferenceV1",
    "Polarity",
    "PreferenceDomain",
    "ReferenceAnnotationV1",
    "ReferenceLibraryV1",
    "ReferenceSourceV1",
    "ReferenceTimeRangeV1",
    "Scope",
    "SourceKind",
]
