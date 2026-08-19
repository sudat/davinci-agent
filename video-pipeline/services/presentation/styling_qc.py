"""Fail-closed QC over the styled presentation output (Todo 57).

The Todo-44 subtitle rules are recomputed over the styled table — never
trusted from the compile stage — and extended with the presentation rules:
the style table resolves inside the policy's declared style set, style
parameters sit within their declared ranges, texts are UTF-8 clean, titled
items never collide with the subtitle safe-area band or each other, and
every titled item carries non-empty fact evidence.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.contracts.styled_presentation import (
    StyledQcViolation,
    SubtitleStyleParams,
)

if TYPE_CHECKING:
    from services.compile.subtitle_policy import SubtitleQcPolicy
    from services.contracts.primitives import RecordFrameSpan
    from services.presentation.styling_models import (
        StyledCue,
        StyledPresentation,
    )

CONTROL_CHAR_BOUND: Final = 0x20
DELETE_CHAR: Final = 0x7F


class StyledQcError(Exception):
    """One or more styled-QC violations; nothing styled leaves on failure."""

    def __init__(self, violations: tuple[StyledQcViolation, ...]) -> None:
        super().__init__("; ".join(f"{v.rule_id}:{v.item_id}" for v in violations))
        self.violations = violations


def _utf8_clean(text: str) -> bool:
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return not any(
        ord(char) < CONTROL_CHAR_BOUND or ord(char) == DELETE_CHAR for char in text
    )


def _overlaps(left: RecordFrameSpan, right: RecordFrameSpan) -> bool:
    return left.start_frame < right.end_frame and right.start_frame < left.end_frame


def _cue_violations(cue: StyledCue, policy: SubtitleQcPolicy) -> list[StyledQcViolation]:
    violations: list[StyledQcViolation] = []
    if cue.record_span.length < policy.min_duration_frames:
        violations.append(
            StyledQcViolation(
                rule_id="subtitle_min_duration",
                item_id=cue.item_id,
                detail=f"{cue.record_span.length} frames < min {policy.min_duration_frames}",
            )
        )
    if len(cue.lines) > policy.max_lines:
        violations.append(
            StyledQcViolation(
                rule_id="subtitle_max_lines",
                item_id=cue.item_id,
                detail=f"{len(cue.lines)} lines > max {policy.max_lines}",
            )
        )
    overlong = [len(line) for line in cue.lines if len(line) > policy.max_chars_per_line]
    if overlong:
        violations.append(
            StyledQcViolation(
                rule_id="subtitle_max_chars",
                item_id=cue.item_id,
                detail=f"lines exceed {policy.max_chars_per_line} chars: {overlong}",
            )
        )
    capacity = len(cue.lines) <= policy.max_lines and all(
        len(line) <= policy.max_chars_per_line for line in cue.lines
    )
    if not cue.safe_area or not capacity:
        violations.append(
            StyledQcViolation(
                rule_id="subtitle_safe_area",
                item_id=cue.item_id,
                detail="cue does not fit the safe-area text capacity",
            )
        )
    if not _utf8_clean(cue.text) or any(not _utf8_clean(line) for line in cue.lines):
        violations.append(
            StyledQcViolation(
                rule_id="text_encoding",
                item_id=cue.item_id,
                detail="cue text is not clean UTF-8 (control characters present)",
            )
        )
    return violations


def _style_violations(
    styled: StyledPresentation, policy: SubtitleQcPolicy
) -> list[StyledQcViolation]:
    violations: list[StyledQcViolation] = []
    try:
        SubtitleStyleParams.model_validate(styled.style.model_dump(mode="json"))
    except ValidationError as error:
        violations.append(
            StyledQcViolation(
                rule_id="style_param_out_of_range",
                item_id=styled.artifact_id,
                detail=f"style table outside declared ranges: {error.errors()[0]['msg']}",
            )
        )
    for node in (*styled.cues, *styled.titled_items):
        if node.style_id not in policy.declared_style_refs:
            violations.append(
                StyledQcViolation(
                    rule_id="style_unregistered",
                    item_id=node.item_id,
                    detail=f"style_id {node.style_id} is not a declared style ref",
                )
            )
        if node.style_id != styled.style_id or node.style != styled.style:
            violations.append(
                StyledQcViolation(
                    rule_id="style_table_mismatch",
                    item_id=node.item_id,
                    detail="node does not carry the artifact style table",
                )
            )
        try:
            SubtitleStyleParams.model_validate(node.style.model_dump(mode="json"))
        except ValidationError:
            violations.append(
                StyledQcViolation(
                    rule_id="style_param_out_of_range",
                    item_id=node.item_id,
                    detail="node style parameters fall outside the declared ranges",
                )
            )
    return violations


def _titled_violations(styled: StyledPresentation) -> list[StyledQcViolation]:
    violations: list[StyledQcViolation] = []
    for item in styled.titled_items:
        if not _utf8_clean(item.text):
            violations.append(
                StyledQcViolation(
                    rule_id="text_encoding",
                    item_id=item.item_id,
                    detail="titled text is not clean UTF-8",
                )
            )
        if not item.evidence_text.strip():
            violations.append(
                StyledQcViolation(
                    rule_id="fact_without_evidence",
                    item_id=item.item_id,
                    detail="titled item carries no resolved evidence text",
                )
            )
        if item.anchor.startswith("bottom-") and any(
            _overlaps(item.record_span, cue.record_span) for cue in styled.cues
        ):
            violations.append(
                StyledQcViolation(
                    rule_id="titled_placement_collision",
                    item_id=item.item_id,
                    detail=f"{item.anchor} item overlaps the subtitle safe-area band",
                )
            )
    ordered = sorted(
        styled.titled_items,
        key=lambda item: (item.anchor, item.record_span.start_frame, item.item_id),
    )
    for previous, current in pairwise(ordered):
        if (
            previous.anchor == current.anchor
            and current.record_span.start_frame < previous.record_span.end_frame
        ):
            violations.append(
                StyledQcViolation(
                    rule_id="titled_item_overlap",
                    item_id=current.item_id,
                    detail=f"titled item overlaps {previous.item_id} at same anchor",
                )
            )
    return violations


def run_styled_qc(
    styled: StyledPresentation, policy: SubtitleQcPolicy
) -> tuple[StyledQcViolation, ...]:
    """Evaluate every styled-QC rule; pure — returns violations without raising."""

    violations: list[StyledQcViolation] = []
    for cue in styled.cues:
        violations.extend(_cue_violations(cue, policy))
    ordered = sorted(styled.cues, key=lambda cue: (cue.record_span.start_frame, cue.item_id))
    for previous, current in pairwise(ordered):
        if current.record_span.start_frame < previous.record_span.end_frame:
            violations.append(
                StyledQcViolation(
                    rule_id="subtitle_cue_overlap",
                    item_id=current.item_id,
                    detail=f"cue overlaps {previous.item_id} on the subtitle track",
                )
            )
    violations.extend(_style_violations(styled, policy))
    violations.extend(_titled_violations(styled))
    return tuple(violations)


def require_styled_qc(styled: StyledPresentation, policy: SubtitleQcPolicy) -> None:
    """Raise :class:`StyledQcError` unless every styled-QC rule passes."""

    violations = run_styled_qc(styled, policy)
    if violations:
        raise StyledQcError(violations)


__all__ = [
    "StyledQcError",
    "StyledQcViolation",
    "require_styled_qc",
    "run_styled_qc",
]
