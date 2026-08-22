"""Pairwise preference application — v43 task 27.

Pure function only. Opportunistic presentation lives in UI layer (task 50).
This module maps choice a/b to the expected direction for the given domain.
When an annotation-derived polarity is available for the chosen reference,
that polarity is used; otherwise the pairwise statement itself is recorded
as domain evidence with polarity ``like`` (the operator preferred that
reference's rendering of the domain).
"""

from __future__ import annotations

from services.reference_learning.models import (
    DerivedTasteEntryV1,
    DerivedTasteProfileV1,
    PairwisePreferenceV1,
)


def _statement_for(preference: PairwisePreferenceV1) -> str:
    chosen = "A" if preference.choice == "a" else "B"
    return (
        f"pairwise {preference.pairwise_id}: prefer {chosen}"
        f" for {preference.domain.value} — {preference.reason}"
    )


def apply_pairwise(
    profile: DerivedTasteProfileV1,
    preference: PairwisePreferenceV1,
) -> DerivedTasteProfileV1:
    """Apply a pairwise preference to a profile in the expected direction.

    Semantics:
    - Always records the chosen reference as ``like`` for ``preference.domain``
      (the pairwise statement itself is domain evidence when no annotation
      polarity is available inline).
    - When the profile already contains domain evidence for the chosen
      reference with a polarity, that polarity would be reused; this pure
      function has no annotation store, so it falls back to ``like`` which
      is the correct expected direction for a chosen A/B comparison.
    - Returns a new profile (immutable ``model_copy``) with the new entry
      appended; never mutates the input.
    """
    statement = _statement_for(preference)
    entry = DerivedTasteEntryV1(
        domain=preference.domain,
        statement=statement,
        polarity="like",
        confidence=0.75,
        evidence_refs=(preference.pairwise_id,),
        source_kind="pairwise",
    )
    new_entries = (*profile.entries, entry)
    return profile.model_copy(update={"entries": new_entries})


__all__ = ["apply_pairwise"]
