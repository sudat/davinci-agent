"""Deterministic Phase-0C Edit Plan compiler subset.

Integer-frame arithmetic only; no Resolve-specific fields; every anchor must
resolve. The compiler consumes an EditPlan0C plus a ReviewCommand0C, derives
the deterministic Decision, and emits the next EditPlan0C and a Timeline IR.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from services.compile.classify import ClassifyError, decide
from services.conform.guards import guard_int64
from services.contracts.edit_plan_0c import (
    Decision0C,
    EditPlan0C,
    EditPlanItem0C,
    ItemIdSelector0C,
    ReviewCommand0C,
)
from services.contracts.primitives import (
    ArtifactRef,
    Producer,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
    TrackRef0C,
)
from services.foundation_io import canonical_model_bytes

PRODUCER = Producer(name="phase0c-compiler", version="1")


class CompileError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class CompileResult0C:
    plan: EditPlan0C
    ir: TimelineIr0C
    decision: Decision0C

    def plan_sha256(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self.plan)).hexdigest()

    def ir_sha256(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self.ir)).hexdigest()


def _require_anchor(plan: EditPlan0C, item_id: str) -> EditPlanItem0C:
    for item in plan.plan.items:
        if item.item_id == item_id:
            return item
    raise CompileError(f"unresolved anchor: item {item_id} is not in plan")


def _bumped_version(version: str) -> str:
    return "v2" if version == "v1" else version


def _apply_remove(plan: EditPlan0C, target: EditPlanItem0C) -> tuple[EditPlanItem0C, ...]:
    link = target.av_link_id
    kept = tuple(
        item
        for item in plan.plan.items
        if item.item_id != target.item_id
        and not (link is not None and item.av_link_id == link)
    )
    if not kept:
        raise CompileError("remove would empty the plan")
    return kept


def _apply_span(
    plan: EditPlan0C,
    command: ReviewCommand0C,
    target: EditPlanItem0C,
) -> tuple[EditPlanItem0C, ...]:
    if command.new_span is None:
        raise CompileError("adjust_source_span requires new_span")
    if command.new_span.end_frame > plan.plan.edit_source.total_frames:
        raise CompileError("adjusted span exceeds the edit source length")
    updated: list[EditPlanItem0C] = []
    for item in plan.plan.items:
        same_group = (
            target.av_link_id is not None
            and item.av_link_id == target.av_link_id
            and item.span == target.span
        )
        if item.item_id == target.item_id or same_group:
            updated.append(item.model_copy(update={"span": command.new_span}))
        else:
            updated.append(item)
    return tuple(updated)


def _apply_text(
    plan: EditPlan0C,
    command: ReviewCommand0C,
    target: EditPlanItem0C,
) -> tuple[EditPlanItem0C, ...]:
    if command.new_text is None:
        raise CompileError("correct_subtitle requires new_text")
    return tuple(
        item.model_copy(update={"subtitle_text": command.new_text})
        if item.item_id == target.item_id
        else item
        for item in plan.plan.items
    )


def _body_digest(plan: EditPlan0C) -> str:
    digest = hashlib.sha256()
    digest.update(plan.artifact_id.encode())
    digest.update(plan.plan.plan_version.encode())
    for item in plan.plan.items:
        digest.update(canonical_model_bytes(item))
    return digest.hexdigest()


def _rehash(plan: EditPlan0C) -> EditPlan0C:
    return plan.model_copy(update={"content_hash": _body_digest(plan)})


def apply_command(plan: EditPlan0C, command: ReviewCommand0C) -> EditPlan0C:
    decision = decide(plan, command)
    if decision.action != "apply":
        raise CompileError("only clear commands may be applied")
    if not isinstance(command.target, ItemIdSelector0C):
        raise CompileError("apply requires an unambiguous single-item anchor")
    target = _require_anchor(plan, command.target.item_id)
    if command.operation == "remove_segment":
        items = _apply_remove(plan, target)
    elif command.operation == "adjust_source_span":
        items = _apply_span(plan, command, target)
    else:
        items = _apply_text(plan, command, target)
    body = plan.plan.model_copy(
        update={"items": items, "plan_version": _bumped_version(plan.plan.plan_version)}
    )
    return _rehash(plan.model_copy(update={"plan": body}))


def build_ir(plan: EditPlan0C, artifact_id: str, inputs: tuple[ArtifactRef, ...]) -> TimelineIr0C:
    tracks: list[TimelineTrack0C] = []
    for track_index in sorted({item.track_index for item in plan.plan.items}):
        kind: Literal["video", "audio", "subtitle"] = next(
            item.kind for item in plan.plan.items if item.track_index == track_index
        )
        items: list[TimelineItem0C] = []
        cursor = 0
        for item in plan.plan.items:
            if item.track_index != track_index:
                continue
            record_end = guard_int64(cursor + item.span.length, f"record end for {item.item_id}")
            items.append(
                TimelineItem0C(
                    item_id=item.item_id,
                    kind=item.kind,
                    source=SourceRef(
                        source_id=item.source_id,
                        span=SourceFrameSpan(
                            start_frame=item.span.start_frame,
                            end_frame=item.span.end_frame,
                            rate=plan.frame_rate,
                        ),
                    ),
                    record_span=RecordFrameSpan(start_frame=cursor, end_frame=record_end),
                    av_link_id=item.av_link_id,
                    subtitle_text=item.subtitle_text,
                )
            )
            cursor = record_end
        tracks.append(
            TimelineTrack0C(track=TrackRef0C(kind=kind, index=track_index), items=tuple(items))
        )
    return TimelineIr0C(
        artifact_id=artifact_id,
        artifact_type="timeline_ir_0c",
        schema_version="timeline-ir-0c-v1",
        content_hash=_body_digest(plan),
        producer=PRODUCER,
        inputs=inputs,
        rate=plan.frame_rate,
        tracks=tuple(tracks),
    )


def compile_plan(
    plan: EditPlan0C,
    command: ReviewCommand0C,
    ir_artifact_id: str,
) -> CompileResult0C:
    try:
        decision = decide(plan, command)
    except ClassifyError as error:
        raise CompileError(str(error)) from error
    result_plan = apply_command(plan, command) if decision.action == "apply" else plan
    input_refs = (
        ArtifactRef(
            artifact_id=plan.artifact_id,
            sha256=hashlib.sha256(canonical_model_bytes(plan)).hexdigest(),
        ),
    )
    ir = build_ir(result_plan, ir_artifact_id, input_refs)
    return CompileResult0C(plan=result_plan, ir=ir, decision=decision)


__all__ = ["CompileError", "CompileResult0C", "apply_command", "build_ir", "compile_plan"]
