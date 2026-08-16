"""Pure recomputation helpers for the Phase-0C gate evaluator.

Golden projections (plan items / record tables), the v1→v2 transformation
diff, the Todo-27 trace coverage expectation, and the per-criterion check
state. Everything here is deterministic projection over contract models; file
IO and orchestration live in the evaluator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.gates.phase0c import PHASE_0C_CRITERIA

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C

CODE_INITIAL_DECISION: Final = "initial-plan-v1"


class GoldenPlanItem(StrictModel):
    item_id: str
    kind: Literal["video", "audio", "subtitle"]
    track_index: int
    av_link_id: str | None = None
    subtitle_text: str | None = None
    span_start: int
    span_end: int


class GoldenRecordRow(StrictModel):
    item_id: str
    kind: Literal["video", "audio", "subtitle"]
    track_index: int
    record_start: int
    record_end: int
    source_start: int
    source_end: int
    subtitle_text: str | None = None


class GoldenCase0C(StrictModel):
    classification: Literal["clear", "ambiguous", "conflict"]
    decision: Literal["apply", "defer"]
    resulting_plan_version: Literal["v1", "v2"]
    target_candidate_item_ids: tuple[str, ...]
    plan_items: tuple[GoldenPlanItem, ...] = Field(min_length=1)
    record_table: tuple[GoldenRecordRow, ...] = Field(min_length=1)


class CheckState0c:
    """Accumulates criterion booleans, mismatch rows, and evidence hashes."""

    def __init__(self, criteria: tuple[str, ...] = PHASE_0C_CRITERIA) -> None:
        self.criteria: dict[str, bool] = dict.fromkeys(criteria, True)
        self.rows: list[tuple[str, str]] = []
        self.evidence_sha: dict[str, list[str]] = {criterion: [] for criterion in criteria}

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.rows.append((code, detail))

    def note(self, criterion: str, sha: str) -> None:
        if sha and sha not in self.evidence_sha[criterion]:
            self.evidence_sha[criterion].append(sha)


def plan_items_view(plan: EditPlan0C) -> tuple[GoldenPlanItem, ...]:
    return tuple(
        GoldenPlanItem(
            item_id=item.item_id,
            kind=item.kind,
            track_index=item.track_index,
            av_link_id=item.av_link_id,
            subtitle_text=item.subtitle_text,
            span_start=item.span.start_frame,
            span_end=item.span.end_frame,
        )
        for item in plan.plan.items
    )


def record_table_view(ir: TimelineIr0C) -> tuple[GoldenRecordRow, ...]:
    return tuple(
        GoldenRecordRow(
            item_id=item.item_id,
            kind=item.kind,
            track_index=track.track.index,
            record_start=item.record_span.start_frame,
            record_end=item.record_span.end_frame,
            source_start=item.source.span.start_frame,
            source_end=item.source.span.end_frame,
            subtitle_text=item.subtitle_text,
        )
        for track in ir.tracks
        for item in track.items
    )


class TransformationView(StrictModel):
    removed_item_ids: tuple[str, ...] = ()
    span_changes: tuple[tuple[str, int, int], ...] = ()
    text_changes: tuple[tuple[str, str], ...] = ()


def transformation_diff(before: EditPlan0C, after: EditPlan0C) -> TransformationView:
    old = {item.item_id: item for item in before.plan.items}
    new = {item.item_id: item for item in after.plan.items}
    removed = tuple(item_id for item_id in old if item_id not in new)
    span_changes = tuple(
        (item_id, new[item_id].span.start_frame, new[item_id].span.end_frame)
        for item_id in old
        if item_id in new
        and (old[item_id].span.start_frame, old[item_id].span.end_frame)
        != (new[item_id].span.start_frame, new[item_id].span.end_frame)
    )
    text_changes = tuple(
        (item_id, new[item_id].subtitle_text or "")
        for item_id in old
        if item_id in new and old[item_id].subtitle_text != new[item_id].subtitle_text
    )
    return TransformationView(
        removed_item_ids=removed,
        span_changes=span_changes,
        text_changes=text_changes,
    )


def expected_coverage(
    previous_video_spans: dict[str, tuple[int, int]],
    head_ir: TimelineIr0C,
    decision_id: str,
) -> tuple[tuple[int, int, str], ...]:
    """Recompute the Todo-27 post-apply coverage from raw IR state.

    A head video item whose record span is unchanged from preview-0 inherits
    the initial decision id; every shifted/new range maps to the applied
    decision id.
    """

    entries: list[tuple[int, int, str]] = []
    for track in head_ir.tracks:
        if track.track.kind != "video":
            continue
        for item in track.items:
            span = (item.record_span.start_frame, item.record_span.end_frame)
            inherited = previous_video_spans.get(item.item_id) == span
            entries.append((*span, CODE_INITIAL_DECISION if inherited else decision_id))
    return tuple(entries)


__all__ = [
    "CODE_INITIAL_DECISION",
    "CheckState0c",
    "GoldenCase0C",
    "GoldenPlanItem",
    "GoldenRecordRow",
    "TransformationView",
    "expected_coverage",
    "plan_items_view",
    "record_table_view",
    "transformation_diff",
]
