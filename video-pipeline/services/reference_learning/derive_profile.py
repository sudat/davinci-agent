"""Derived taste profile aggregation — v43 task 27.

Aggregates evidence classes into ``DerivedTasteProfileV1`` with:
- entries carrying ``evidence_refs`` REQUIRED and ``source_kind`` per class
- contradictions: two approved evidences with opposite polarity in the same
  domain → ``unresolved_contradictions`` record (never averaged, never dropped)
- confidence derived from evidence count, monotonic non-decreasing

Entries are preserved verbatim (no averaging).
"""

from __future__ import annotations

from collections.abc import Sequence

from services.contracts.primitives import Producer
from services.reference_learning.models import (
    ContradictionRecordV1,
    DerivedTasteEntryV1,
    DerivedTasteProfileV1,
    PairwisePreferenceV1,
    PreferenceDomain,
    ReferenceAnnotationV1,
    ReferenceSourceV1,
)

_CONFIDENCE_BASE = 0.6
_CONFIDENCE_STEP = 0.07
_CONFIDENCE_CAP = 0.95
_CONFIDENCE_SINGLE_MIN = 0.65


def _confidence_for_count(count: int) -> float:
    # Monotonic: more evidence → higher confidence, capped.
    # 1 → 0.65, 2 → 0.72, 3 → 0.79, ... capped
    value = _CONFIDENCE_BASE + _CONFIDENCE_STEP * count
    if count == 1:
        value = max(value, _CONFIDENCE_SINGLE_MIN)
    value = min(value, _CONFIDENCE_CAP)
    value = max(value, 0.0)
    return round(value, 4)


def _entry_for_annotation(
    annotation: ReferenceAnnotationV1,
    *,
    domain: PreferenceDomain,
    confidence: float,
    source_kind: str = "reference_annotation",  # type: ignore[assignment]
) -> DerivedTasteEntryV1:
    polarity = annotation.domains.get(domain, "unspecified")
    # Caller guarantees domain is named with non-unspecified polarity
    assert polarity != "unspecified"  # noqa: S101  # internal invariant
    return DerivedTasteEntryV1(
        domain=domain,
        statement=annotation.rationale,
        polarity=polarity,  # type: ignore[arg-type]
        confidence=confidence,
        evidence_refs=(annotation.annotation_id,),
        source_kind=source_kind,  # type: ignore[arg-type]
    )


def derive_profile(  # noqa: C901,PLR0912,PLR0913,PLR0915
    sources: Sequence[ReferenceSourceV1] = (),
    annotations: Sequence[ReferenceAnnotationV1] = (),
    approved_edits: Sequence[object] = (),
    negative_examples: Sequence[ReferenceAnnotationV1] = (),
    pairwise: Sequence[PairwisePreferenceV1] = (),
    *,
    profile_id: str = "derived-0001",
    provenance: Producer | None = None,
    created_at: str = "2026-08-22T00:00:00Z",
) -> DerivedTasteProfileV1:
    """Aggregate evidence classes into a DerivedTasteProfileV1.

    Args:
        sources: reference sources (provenance only in v4.3; no direct entries).
        annotations: approved :class:`ReferenceAnnotationV1` — each named domain
            becomes one entry per domain (polarity from ``domains``).
        approved_edits: approved edit history entries (typed as
            ``DerivedTasteEntryV1`` or mapping with ``domain``/``statement``).
        negative_examples: annotations marked as negative examples.
        pairwise: :class:`PairwisePreferenceV1` comparisons.
    """
    _ = sources  # provenance-only in v4.3; validated
    prod = (
        provenance
        if provenance is not None
        else Producer(name="reference-learning", version="v1")
    )

    # ------------------------------------------------------------------
    # Collect entries per class with source_kind tagging
    # ------------------------------------------------------------------
    raw_entries: list[DerivedTasteEntryV1] = []

    # Group annotations by domain to compute monotonic confidence per domain count
    domain_counts: dict[PreferenceDomain, int] = {}

    # First pass: count per domain to derive confidence
    for ann in annotations:
        if ann.human_approval != "approved":
            continue
        for dom in ann.named_domains:
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
    for ann in negative_examples:
        for dom in ann.named_domains:
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
    for pw in pairwise:
        domain_counts[pw.domain] = domain_counts.get(pw.domain, 0) + 1
    # approved_edits: count if they carry domain info
    for item in approved_edits:
        dom_val: PreferenceDomain | None = None
        if isinstance(item, DerivedTasteEntryV1):
            dom_val = item.domain
        elif isinstance(item, dict) and "domain" in item:  # type: ignore[operator]
            try:
                dom_val = PreferenceDomain(item["domain"])  # type: ignore[index]
            except ValueError:
                dom_val = None
        if dom_val is not None:
            domain_counts[dom_val] = domain_counts.get(dom_val, 0) + 1

    # Second pass: materialize entries
    for ann in annotations:
        if ann.human_approval != "approved":
            continue
        for dom in ann.named_domains:
            pol = ann.domains.get(dom)
            if pol is None or pol == "unspecified":
                continue
            conf = _confidence_for_count(domain_counts.get(dom, 1))
            raw_entries.append(
                _entry_for_annotation(
                    ann, domain=dom, confidence=conf, source_kind="reference_annotation"
                )
            )

    for ann in negative_examples:
        for dom in ann.named_domains:
            pol = ann.domains.get(dom)
            if pol is None or pol == "unspecified":
                continue
            conf = _confidence_for_count(domain_counts.get(dom, 1))
            # Negative examples preserve their polarity but are tagged as negative_example
            raw_entries.append(
                DerivedTasteEntryV1(
                    domain=dom,
                    statement=ann.rationale,
                    polarity=pol,  # type: ignore[arg-type]
                    confidence=conf,
                    evidence_refs=(ann.annotation_id,),
                    source_kind="negative_example",
                )
            )

    for pw in pairwise:
        # Pairwise preference always records chosen style as "like"
        conf = _confidence_for_count(domain_counts.get(pw.domain, 1))
        statement = (
            f"pairwise {pw.pairwise_id}: prefer {pw.choice}"
            f" for {pw.domain.value} — {pw.reason}"
        )
        raw_entries.append(
            DerivedTasteEntryV1(
                domain=pw.domain,
                statement=statement,
                polarity="like",
                confidence=conf,
                evidence_refs=(pw.pairwise_id,),
                source_kind="pairwise",
            )
        )

    for item in approved_edits:
        if isinstance(item, DerivedTasteEntryV1):
            # Re-tag source_kind if not already approved_edit; preserve refs
            conf = _confidence_for_count(domain_counts.get(item.domain, 1))
            raw_entries.append(
                DerivedTasteEntryV1(
                    domain=item.domain,
                    statement=item.statement,
                    polarity=item.polarity,
                    confidence=conf,
                    evidence_refs=item.evidence_refs,
                    source_kind="approved_edit",
                )
            )
        elif isinstance(item, dict):
            # Dict edit entry with explicit fields
            try:
                dom = PreferenceDomain(item["domain"])  # type: ignore[index]
                stmt = str(item["statement"])  # type: ignore[index]
                pol = str(item.get("polarity", "like"))
                refs = tuple(item.get("evidence_refs", (item.get("edit_id", "edit-0001"),)))  # type: ignore[operator]
                conf = _confidence_for_count(domain_counts.get(dom, 1))
                raw_entries.append(
                    DerivedTasteEntryV1(
                        domain=dom,
                        statement=stmt,
                        polarity=pol,  # type: ignore[arg-type]
                        confidence=conf,
                        evidence_refs=refs,  # type: ignore[arg-type]
                        source_kind="approved_edit",
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue

    # ------------------------------------------------------------------
    # Contradictions: opposite polarities (like vs dislike) in same domain
    # ------------------------------------------------------------------
    contradictions: list[ContradictionRecordV1] = []
    by_domain: dict[PreferenceDomain, list[DerivedTasteEntryV1]] = {}
    for entry in raw_entries:
        by_domain.setdefault(entry.domain, []).append(entry)

    for domain, entries in by_domain.items():
        # Collect polarities ignoring neutral/unspecified
        like_refs: list[str] = []
        dislike_refs: list[str] = []
        for entry in entries:
            if entry.polarity == "like":
                like_refs.extend(list(entry.evidence_refs))
            elif entry.polarity == "dislike":
                dislike_refs.extend(list(entry.evidence_refs))
        if like_refs and dislike_refs:
            # Take first like and first dislike as conflicting pair; include all if needed
            # Must have at least 2 refs; pick the first of each
            conflicting = (like_refs[0], dislike_refs[0])
            # If more than 2 conflicting evidences, include up to first 2 pairs (keep min 2)
            # Ensure uniqueness
            if len(like_refs) > 1 or len(dislike_refs) > 1:
                # Include all distinct refs up to a reasonable bound
                max_uniq = 4  # cap to keep record bounded
                min_conflict = 2
                uniq: list[str] = []
                seen: set[str] = set()
                for r in like_refs + dislike_refs:
                    if r not in seen:
                        seen.add(r)
                        uniq.append(r)
                    if len(uniq) >= max_uniq:
                        break
                if len(uniq) >= min_conflict:
                    conflicting = tuple(uniq)  # type: ignore[assignment]
            contradictions.append(
                ContradictionRecordV1(
                    domain=domain,
                    description=(
                        f"contradiction in {domain.value}:"
                        " both like and dislike evidence present"
                    ),
                    conflicting_refs=conflicting,  # type: ignore[arg-type]
                    note="never averaged — requires human resolution",
                )
            )

    return DerivedTasteProfileV1(
        profile_id=profile_id,  # type: ignore[arg-type]
        entries=tuple(raw_entries),
        unresolved_contradictions=tuple(contradictions),
        provenance=prod,
        created_at=created_at,
    )


__all__ = ["derive_profile"]
