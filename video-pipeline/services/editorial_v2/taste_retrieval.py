"""Taste retrieval adapter for Director v2 (task 28).

Thin, cite-preserving bridge from task 27's ``retrieve_relevant_evidence``
to director context (mainly Pass C): returns per-domain ``TasteCitation``
entries whose ``entry_refs`` are the underlying ``DerivedTasteEntryV1``
evidence refs. The citation contract is inherited — a profile entry that
lost its refs raises ``MissingCitationError`` inside retrieval, so every
taste-influenced decision downstream can cite real entries.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, StrictModel, to_tuple
from services.reference_learning.models import (  # noqa: TC001 (runtime models)
    DerivedTasteProfileV1,
    Polarity,
    PreferenceDomain,
)
from services.reference_learning.retrieval import retrieve_relevant_evidence


class TasteCitation(StrictModel):
    """One citable taste statement in director context."""

    domain: PreferenceDomain
    statement: Annotated[str, Field(min_length=1, strict=True)]
    polarity: Polarity
    entry_refs: Annotated[tuple[Identifier, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )


def cited_taste_entries(
    profile: DerivedTasteProfileV1,
    domain: PreferenceDomain,
    decision_context: dict[str, object] | None = None,
) -> tuple[TasteCitation, ...]:
    """Return the domain-scoped taste entries as citable decisions input.

    Delegates to task 27 retrieval (domain-scoped selection + explicit rules,
    citations enforced); never invents or averages entries.
    """

    return tuple(
        TasteCitation(
            domain=entry.domain,
            statement=entry.statement,
            polarity=entry.polarity,
            entry_refs=entry.evidence_refs,
        )
        for entry in retrieve_relevant_evidence(profile, domain, decision_context)
    )


__all__ = ["TasteCitation", "cited_taste_entries"]
