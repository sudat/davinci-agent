"""Evidence retrieval with citation contract — v43 task 27.

Retrieval returns only entries for the named domain plus explicit rules.
Every returned entry must carry ``evidence_refs`` (citation); the helper
``require_citations`` enforces this contract with a typed ``MissingCitationError``.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.reference_learning.models import (
    DerivedTasteEntryV1,  # noqa: TC001
    DerivedTasteProfileV1,  # noqa: TC001
    PreferenceDomain,  # noqa: TC001
)


class MissingCitationError(ValueError):
    """Raised when an entry required to cite evidence has empty evidence_refs."""

    def __init__(self, message: str = "entry missing required evidence_refs citation") -> None:
        super().__init__(message)


def require_citations(entries: Sequence[DerivedTasteEntryV1]) -> None:
    """Enforce cite-required contract: every entry must have evidence_refs.

    Raises:
        MissingCitationError: if any entry has empty ``evidence_refs``.
    """
    for entry in entries:
        refs = entry.evidence_refs
        # refs is a tuple; bypassed validation via model_construct may yield empty
        if refs is None or len(refs) == 0:  # type: ignore[arg-type]
            raise MissingCitationError(
                f"entry for domain {entry.domain.value} missing citation:"
                f" statement={entry.statement!r}"
            )


def retrieve_relevant_evidence(
    profile: DerivedTasteProfileV1,
    domain: PreferenceDomain,
    decision_context: dict[str, object] | None = None,
) -> list[DerivedTasteEntryV1]:
    """Return only entries for the named domain plus explicit rules.

    Args:
        profile: the aggregated taste profile.
        domain: the decision domain to retrieve for.
        decision_context: optional context about the current decision
            (e.g. story block, operation); currently used only for
            provenance filtering in future, but part of the contract.

    Returns:
        List of entries where ``entry.domain == domain`` or
        ``entry.source_kind == "explicit_rule"``. Each entry carries
        its ``evidence_refs`` citation; ``MissingCitationError`` is
        raised if any selected entry lost its refs.
    """
    _ = decision_context  # reserved for future block/op filtering
    selected = [
        entry
        for entry in profile.entries
        if entry.domain == domain or entry.source_kind == "explicit_rule"
    ]
    require_citations(selected)
    return selected


__all__ = ["MissingCitationError", "require_citations", "retrieve_relevant_evidence"]
