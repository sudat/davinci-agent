"""Evidence v2 — multimodal candidate corroboration over MediaQueryApiV2 (task 23).

v1 corroborated with ``transcript_search``/``silence_overlap`` only (frozen
``services/editorial/evidence.py``); v2 extends to the seven PRD 7.4 methods
(``evidence_v2_probes``), each backed by a distinct api_v2 query:
transcript -> ``transcript_range``, deep_shot_vision -> ``moment_reviews``
fused rows (T7: hash/span/confidence citations — never shot descriptions),
exact_frames -> ``shots`` covering row, audio_measurement ->
``audio_energy_ranges`` (pause requires low energy), similarity ->
``similar_shots``, scene_metadata -> ``scene_summary``, source_quality ->
``visual_quality_ranges``.

Legacy mapping: ``transcript_search`` -> ``transcript``, ``silence_overlap``
-> ``audio_measurement`` (LEGACY_METHOD_ALIASES). NO-INVENTED-IDS preserved:
every bundle ref is an id produced by an api_v2 returned row (or verified via
``shot_detail``); a cited id no query produced raises EvidenceIncompleteV2
naming it. Budget exhaustion raises EvidenceBudgetExceededV2 — never a silent
partial bundle; partial output ONLY under an explicit ``partial_allowance``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.editorial_v2.evidence_v2_probes import (
    PROBES,
    CorroborationMethod,
    EvidenceBudgetExceededV2,
    EvidenceIncompleteV2,
    MomentReviewCitationV2,
    ProbeDeps,
    verify_cited_refs,
)
from services.media_query import v2_models as vm

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.editorial_v2.moment_models import MomentCandidateV2
    from services.media_query.query_v2 import MediaQueryApiV2

LEGACY_METHOD_ALIASES: Final[dict[str, CorroborationMethod]] = {
    "transcript_search": "transcript",
    "silence_overlap": "audio_measurement",
}

_VISUAL: Final[tuple[CorroborationMethod, ...]] = ("deep_shot_vision", "exact_frames")
_METHOD_CHAINS: Final[dict[str, tuple[CorroborationMethod, ...]]] = {
    "speech": ("transcript", "deep_shot_vision"),
    "pause": ("audio_measurement",),
    "alternate_take": ("similarity", "deep_shot_vision"),
    **dict.fromkeys(
        ("b_roll", "reaction", "action", "insert", "product_demo", "screen_demo"), _VISUAL
    ),
    "establishing": ("scene_metadata", "deep_shot_vision"),
    "ambient": ("audio_measurement", "scene_metadata"),
    "transition": ("exact_frames", "deep_shot_vision"),
    **dict.fromkeys(("graphic", "still"), ("exact_frames", "source_quality")),
}


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


class CandidateEvidenceV2(StrictModel):
    """Per-candidate corroboration record; every ref is a real index id.

    ``moment_reviews`` (T7, additive) cites the overlapping fused deep
    reviews by hash, exact half-open span, and overall confidence.
    """

    candidate_id: Identifier
    methods: Annotated[tuple[CorroborationMethod, ...], BeforeValidator(_to_tuple)]
    evidence_refs: Annotated[tuple[Identifier, ...], BeforeValidator(_to_tuple)]
    lineage: Annotated[tuple[Sha256, ...], BeforeValidator(_to_tuple)]
    moment_reviews: Annotated[tuple[MomentReviewCitationV2, ...], BeforeValidator(_to_tuple)] = (
        Field(default_factory=tuple)
    )


class BudgetUsageV2(StrictModel):
    api_calls: int = Field(ge=0, strict=True)
    rows_returned: int = Field(ge=0, strict=True)
    row_budget: int = Field(gt=0, strict=True)


class EvidenceBundleV2(StrictModel):
    entries: Annotated[tuple[CandidateEvidenceV2, ...], BeforeValidator(_to_tuple)]
    lineage: Annotated[tuple[Sha256, ...], BeforeValidator(_to_tuple)]
    budget: BudgetUsageV2
    partial: bool
    missing: Annotated[tuple[Identifier, ...], BeforeValidator(_to_tuple)]


def _corroborate(deps: ProbeDeps, candidate: MomentCandidateV2) -> CandidateEvidenceV2 | None:
    span = vm.FrameSpan(
        start_frame=candidate.source_span.start_frame, end_frame=candidate.source_span.end_frame
    )
    chain = _METHOD_CHAINS[candidate.candidate_type]
    seen: set[str] = set()
    shas: set[str] = set()
    methods: list[CorroborationMethod] = []
    citations: list[MomentReviewCitationV2] = []
    for method in chain:
        hit = PROBES[method](deps, candidate, span)
        if hit.ok:
            methods.append(method)
            seen.update(hit.ids)
            shas.update(hit.shas)
            citations.extend(hit.citations)
    shas.update(verify_cited_refs(deps, candidate, seen))
    if methods:
        return CandidateEvidenceV2(
            candidate_id=candidate.candidate_id,
            methods=tuple(methods),
            evidence_refs=tuple(sorted(seen)),
            lineage=tuple(sorted(shas)),
            moment_reviews=tuple(citations),
        )
    if deps.allowance is None or candidate.candidate_id not in deps.allowance:
        raise EvidenceIncompleteV2(
            f"candidate {candidate.candidate_id} ({candidate.candidate_type}) could not "
            f"be corroborated by any configured method {chain}"
        )
    return None


def assemble_evidence_v2(
    api: MediaQueryApiV2,
    candidates: Sequence[MomentCandidateV2],
    *,
    source_id: str | None = None,
    partial_allowance: frozenset[str] | None = None,
) -> EvidenceBundleV2:
    """Corroborate each MomentCandidateV2 against the v2 media-query index.

    ``source_id`` enables transcript corroboration (the v2 api cannot discover
    source ids itself); without it the transcript method is unavailable and
    chains fall through to their next method.
    """

    deps = ProbeDeps(api, source_id, partial_allowance)
    entries: list[CandidateEvidenceV2] = []
    missing: list[str] = []
    lineage: set[str] = set()
    for candidate in candidates:
        entry = _corroborate(deps, candidate)
        if entry is None:
            missing.append(candidate.candidate_id)
            continue
        entries.append(entry)
        lineage.update(entry.lineage)
    return EvidenceBundleV2(
        entries=tuple(entries),
        lineage=tuple(sorted(lineage)),
        budget=BudgetUsageV2(
            api_calls=deps.usage.calls, rows_returned=deps.usage.rows, row_budget=vm.V2_ROW_BUDGET
        ),
        partial=bool(missing),
        missing=tuple(missing),
    )


__all__ = [
    "LEGACY_METHOD_ALIASES",
    "BudgetUsageV2",
    "CandidateEvidenceV2",
    "CorroborationMethod",
    "EvidenceBudgetExceededV2",
    "EvidenceBundleV2",
    "EvidenceIncompleteV2",
    "MomentReviewCitationV2",
    "assemble_evidence_v2",
]
