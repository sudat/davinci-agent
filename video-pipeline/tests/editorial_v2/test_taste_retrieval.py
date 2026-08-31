"""Behavior lock: ``TasteCitation`` coerces JSON arrays to tuples.

``StrictModel`` runs strict, so an ``entry_refs`` list — the shape every
JSON payload carries (LLM-seam replays, ``model_dump(mode="json")``
round-trips) — is rejected unless the ``BeforeValidator`` coerces it to
a tuple first. Locks that coercion ahead of consolidating the duplicated
``_to_tuple`` helpers into ``services.contracts.primitives``.
"""

from __future__ import annotations

from services.editorial_v2.taste_retrieval import TasteCitation
from services.reference_learning.models import PreferenceDomain


def test_taste_citation_entry_refs_coerces_json_list_to_tuple() -> None:
    # given: a citation payload whose entry_refs arrives as a JSON array
    payload = {
        "domain": PreferenceDomain.b_roll,
        "statement": "increase B-roll density around product shots",
        "polarity": "like",
        "entry_refs": ["ref-anno-01", "ref-anno-02"],
    }

    # when: the strict model validates the payload
    citation = TasteCitation.model_validate(payload)

    # then: the array landed as a tuple (strict mode rejects the raw list)
    assert citation.entry_refs == ("ref-anno-01", "ref-anno-02")
    assert isinstance(citation.entry_refs, tuple)
