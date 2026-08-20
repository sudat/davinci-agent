"""End-to-end Decision→Source→Review→Build trace completeness (PRD 31.6).

Every committed decision must reference declared source spans and be
realized by a build item; review-corrected decisions must exist; build
items must reference declared decisions of the same episode. Any break
is typed and makes the report invalid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.metrics.report_models import TraceBreak, TraceSection
from services.metrics.streams import TraceBuildItem, TraceDecision, is_correction_event

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle


def _span_breaks(
    decision: TraceDecision, sources: set[tuple[str, str]]
) -> list[TraceBreak]:
    if not decision.source_span_ids:
        return [
            TraceBreak(
                code="decision_without_source",
                detail=f"decision {decision.decision_id} of {decision.episode_id} "
                "references no source span",
            )
        ]
    return [
        TraceBreak(
            code="unknown_source_span",
            detail=f"decision {decision.decision_id} references unknown "
            f"source span {span}",
        )
        for span in decision.source_span_ids
        if (decision.episode_id, span) not in sources
    ]


def _decision_breaks(
    decisions: tuple[TraceDecision, ...], sources: set[tuple[str, str]]
) -> tuple[dict[str, str], list[TraceBreak]]:
    breaks: list[TraceBreak] = []
    by_decision_id: dict[str, str] = {}
    for decision in decisions:
        if decision.decision_id in by_decision_id:
            breaks.append(
                TraceBreak(
                    code="duplicate_decision",
                    detail=f"decision {decision.decision_id} declared twice",
                )
            )
        by_decision_id[decision.decision_id] = decision.episode_id
        breaks.extend(_span_breaks(decision, sources))
    return by_decision_id, breaks


def _build_item_breaks(
    items: tuple[TraceBuildItem, ...], by_decision_id: dict[str, str]
) -> tuple[set[tuple[str, str]], list[TraceBreak]]:
    breaks: list[TraceBreak] = []
    built: set[tuple[str, str]] = set()
    for item in items:
        owner = by_decision_id.get(item.decision_id)
        if owner is None:
            breaks.append(
                TraceBreak(
                    code="build_item_without_decision",
                    detail=f"build item {item.item_id} references unknown decision "
                    f"{item.decision_id}",
                )
            )
        elif owner != item.episode_id:
            breaks.append(
                TraceBreak(
                    code="decision_episode_mismatch",
                    detail=f"build item {item.item_id} of {item.episode_id} builds "
                    f"decision {item.decision_id} of {owner}",
                )
            )
        else:
            built.add((item.episode_id, item.decision_id))
    return built, breaks


def _review_breaks(
    bundle: EventBundle, by_decision_id: dict[str, str]
) -> tuple[set[str], list[TraceBreak]]:
    breaks: list[TraceBreak] = []
    linked: set[str] = set()
    for wrapped in bundle.review_events:
        if not is_correction_event(wrapped) or wrapped.event.decision_id is None:
            continue
        decision_id = wrapped.event.decision_id
        if by_decision_id.get(decision_id) != wrapped.episode_id:
            breaks.append(
                TraceBreak(
                    code="review_decision_unknown",
                    detail=f"review event of {wrapped.episode_id} references unknown "
                    f"decision {decision_id}",
                )
            )
        else:
            linked.add(decision_id)
    return linked, breaks


def trace_section(bundle: EventBundle) -> TraceSection:
    decisions = bundle.trace_decisions
    items = bundle.trace_build_items
    if not decisions and not items and not bundle.trace_sources:
        return TraceSection(
            status="not_evaluated",
            decision_count=0,
            build_item_count=0,
            review_linked_decisions=0,
            breaks=(),
        )
    sources = {(item.episode_id, item.source_id) for item in bundle.trace_sources}
    by_decision_id, breaks = _decision_breaks(decisions, sources)
    built, item_breaks = _build_item_breaks(items, by_decision_id)
    linked, review_breaks = _review_breaks(bundle, by_decision_id)
    breaks += item_breaks + review_breaks
    breaks.extend(
        TraceBreak(
            code="decision_without_build_item",
            detail=f"decision {decision.decision_id} of {decision.episode_id} "
            "has no build item",
        )
        for decision in decisions
        if (decision.episode_id, decision.decision_id) not in built
    )
    ordered = tuple(sorted(breaks, key=lambda br: (br.code, br.detail)))
    return TraceSection(
        status="broken" if ordered else "complete",
        decision_count=len(decisions),
        build_item_count=len(items),
        review_linked_decisions=len(linked),
        breaks=ordered,
    )


__all__ = ["trace_section"]
