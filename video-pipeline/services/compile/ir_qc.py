"""Deterministic QC over the production Timeline IR (Todo 44).

Rules (each violation carries a typed rule id):

- ``subtitle_min_duration``: every cue's record span meets the policy minimum.
- ``subtitle_max_lines``: wrapped line count within the policy maximum.
- ``subtitle_max_chars``: every line within the policy per-line maximum.
- ``subtitle_safe_area``: the cue's frozen-layout safe-area verdict holds.
- ``subtitle_cue_overlap``: cues on one subtitle track never overlap.
- ``track_kind_mismatch``: item kinds match their logical track kind.
- ``resolve_field_forbidden``: the serialized document carries no Resolve
  field names (model-level rejection is the first line of defense; this is
  the second).
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel, reject_resolve_keys
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.compile.subtitle_policy import SubtitleQcPolicy
    from services.contracts.timeline_ir import (
        SubtitleCueItem,
        TimelineIrProduction,
        TimelineTrackProduction,
    )

QcRuleId = Literal[
    "subtitle_min_duration",
    "subtitle_max_lines",
    "subtitle_max_chars",
    "subtitle_safe_area",
    "subtitle_cue_overlap",
    "track_kind_mismatch",
    "resolve_field_forbidden",
]


class QcViolation(StrictModel):
    """One typed QC rule violation bound to the offending item."""

    rule_id: QcRuleId
    item_id: str
    detail: str


class IrQcError(Exception):
    """One or more QC rule violations over the produced IR."""

    def __init__(self, violations: tuple[QcViolation, ...]) -> None:
        super().__init__("; ".join(f"{v.rule_id}:{v.item_id}" for v in violations))
        self.violations = violations


def _track_kind_violations(track: TimelineTrackProduction) -> list[QcViolation]:
    violations: list[QcViolation] = []
    for item in track.items:
        if item.kind == "gap":
            continue
        expected = "subtitle" if item.kind == "subtitle_cue" else item.kind
        if expected != track.track.kind:
            violations.append(
                QcViolation(
                    rule_id="track_kind_mismatch",
                    item_id=item.item_id,
                    detail=f"item kind {item.kind} on track {track.track.kind}",
                )
            )
    return violations


def _cue_violations(cue: SubtitleCueItem, policy: SubtitleQcPolicy) -> list[QcViolation]:
    violations: list[QcViolation] = []
    if cue.record_span.length < policy.min_duration_frames:
        violations.append(
            QcViolation(
                rule_id="subtitle_min_duration",
                item_id=cue.item_id,
                detail=(
                    f"cue spans {cue.record_span.length} frames below the "
                    f"minimum {policy.min_duration_frames}"
                ),
            )
        )
    if len(cue.lines) > policy.max_lines:
        violations.append(
            QcViolation(
                rule_id="subtitle_max_lines",
                item_id=cue.item_id,
                detail=f"cue wraps into {len(cue.lines)} lines over {policy.max_lines}",
            )
        )
    overlong = [line for line in cue.lines if len(line) > policy.max_chars_per_line]
    if overlong:
        violations.append(
            QcViolation(
                rule_id="subtitle_max_chars",
                item_id=cue.item_id,
                detail=(
                    f"lines exceed {policy.max_chars_per_line} chars: "
                    f"{[len(line) for line in overlong]}"
                ),
            )
        )
    if not cue.safe_area:
        violations.append(
            QcViolation(
                rule_id="subtitle_safe_area",
                item_id=cue.item_id,
                detail="cue does not fit the safe-area text capacity",
            )
        )
    return violations


def _overlap_violations(track: TimelineTrackProduction) -> list[QcViolation]:
    cues = sorted(
        (item for item in track.items if item.kind == "subtitle_cue"),
        key=lambda cue: (cue.record_span.start_frame, cue.item_id),
    )
    violations: list[QcViolation] = []
    for previous, current in pairwise(cues):
        if current.record_span.start_frame < previous.record_span.end_frame:
            violations.append(
                QcViolation(
                    rule_id="subtitle_cue_overlap",
                    item_id=current.item_id,
                    detail=(
                        f"cue overlaps {previous.item_id} at record frame "
                        f"{current.record_span.start_frame}"
                    ),
                )
            )
    return violations


def _resolve_field_violations(ir: TimelineIrProduction) -> list[QcViolation]:
    document: object = json.loads(canonical_model_bytes(ir))
    try:
        reject_resolve_keys(document)
    except PydanticCustomError as error:
        return [
            QcViolation(
                rule_id="resolve_field_forbidden",
                item_id=ir.artifact_id,
                detail=str(error),
            )
        ]
    return []


def run_ir_qc(ir: TimelineIrProduction, policy: SubtitleQcPolicy) -> tuple[QcViolation, ...]:
    """Evaluate every QC rule; pure — returns the violations without raising."""

    violations: list[QcViolation] = []
    for track in ir.tracks:
        violations.extend(_track_kind_violations(track))
        if track.track.kind == "subtitle":
            for item in track.items:
                if item.kind == "subtitle_cue":
                    violations.extend(_cue_violations(item, policy))
            violations.extend(_overlap_violations(track))
    violations.extend(_resolve_field_violations(ir))
    return tuple(violations)


def require_ir_qc(ir: TimelineIrProduction, policy: SubtitleQcPolicy) -> None:
    """Raise :class:`IrQcError` unless the IR satisfies every QC rule."""

    violations = run_ir_qc(ir, policy)
    if violations:
        raise IrQcError(violations)


__all__ = ["IrQcError", "QcRuleId", "QcViolation", "require_ir_qc", "run_ir_qc"]
