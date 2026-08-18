"""Subtitle cue generation under FROZEN Japanese formatting rules (Todo 44).

Frozen rule table (``ja-cue-format-v1``) — changes require a new rule id:

- ``normalize``: surrounding whitespace stripped; a trailing clause mark
  ``、`` is never displayed (repeatedly dropped from the cue/line end); a
  sentence-ending ``。`` (and the question/exclamation marks) is KEPT.
- ``meaning units``: text splits after each sentence end mark and each
  clause mark (``、``); units rejoin into lines verbatim.
- ``wrap``: greedy packing of whole meaning units up to the policy's
  ``max_chars_per_line``; a line never ends with ``、`` (dropped at the line
  end only); an unbreakable unit longer than the limit still forms its own
  line (and flags the cue unsafe).
- ``safe area``: the cue fits iff every line is within ``max_chars_per_line``
  AND the line count is within ``max_lines`` (both from the QC policy).
- ``min duration`` (declared action ``drop-below-min``): a split piece whose
  record span is shorter than the policy minimum is DROPPED deterministically.
- ``split text`` (declared action ``repeat-source-text``): pieces of a cue
  split at an edit boundary each carry the full formatted source text.

Style refs come only from the policy's declared set (Phase 1 uses the fixed
default ref; Phase 3 introduces real styles). The 0C subtitle-table
conventions (text + integer record span on a subtitle track) are the base
this generator extends.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import SubtitleCueItem

if TYPE_CHECKING:
    from services.compile.record_placement import CuePiece
    from services.compile.subtitle_policy import SubtitleQcPolicy

CUE_FORMAT_RULE_ID: Final = "ja-cue-format-v1"
SENTENCE_END_MARKS: Final = ("。", "？", "！")  # noqa: RUF001 (frozen JA rule table)
CLAUSE_MARK: Final = "、"
MIN_DURATION_RULE: Final = "drop-below-min"
SPLIT_TEXT_RULE: Final = "repeat-source-text"
CUE_ID_PREFIX: Final = "cue"


@dataclass(frozen=True, slots=True)
class FormattedCue:
    """Normalized text, wrapped lines, and the safe-area verdict."""

    text: str
    lines: tuple[str, ...]
    safe_area: bool


def normalize_cue_text(text: str) -> str:
    """Whitespace stripped; trailing ``、`` dropped; sentence end kept."""

    normalized = text.strip()
    while normalized.endswith(CLAUSE_MARK):
        normalized = normalized[:-1].strip()
    return normalized


def meaning_units(text: str) -> tuple[str, ...]:
    """Split after every sentence end and clause mark; keep marks attached."""

    units: list[str] = []
    buffer: list[str] = []
    for character in text:
        buffer.append(character)
        if character in SENTENCE_END_MARKS or character == CLAUSE_MARK:
            units.append("".join(buffer))
            buffer.clear()
    if buffer:
        units.append("".join(buffer))
    return tuple(units)


def _strip_line_end_clause(line: str) -> str:
    while line.endswith(CLAUSE_MARK):
        line = line[:-1]
    return line


def wrap_lines(text: str, max_chars_per_line: int) -> tuple[str, ...]:
    """Greedy whole-unit packing; a line never ends with the clause mark."""

    lines: list[str] = []
    current = ""
    for unit in meaning_units(text):
        candidate = current + unit
        if not current or len(candidate) <= max_chars_per_line:
            current = candidate
        else:
            lines.append(_strip_line_end_clause(current))
            current = unit
    if current:
        lines.append(_strip_line_end_clause(current))
    return tuple(line for line in lines if line)


def format_cue(text: str, policy: SubtitleQcPolicy) -> FormattedCue | None:
    """Apply the frozen formatting table; ``None`` means the cue is dropped."""

    normalized = normalize_cue_text(text)
    if not normalized:
        return None
    lines = wrap_lines(normalized, policy.max_chars_per_line)
    if not lines:
        return None
    safe = len(lines) <= policy.max_lines and all(
        len(line) <= policy.max_chars_per_line for line in lines
    )
    return FormattedCue(text=normalized, lines=lines, safe_area=safe)


def cue_item_id(
    piece: CuePiece, formatted: FormattedCue, style_ref: Identifier
) -> str:
    """Deterministic cue identity recomputed from normalized fields."""

    digest = hashlib.sha256(
        "|".join(
            (
                CUE_FORMAT_RULE_ID,
                piece.segment_id,
                formatted.text,
                str(piece.source_start),
                str(piece.source_end),
                str(piece.record_start),
                str(piece.record_end),
                style_ref,
            )
        ).encode()
    ).hexdigest()
    return f"{CUE_ID_PREFIX}.{digest[:24]}"


def build_cue_item(
    piece: CuePiece,
    policy: SubtitleQcPolicy,
    rate: RationalFrameRate,
    source_id: str,
) -> tuple[SubtitleCueItem | None, str | None]:
    """Build one cue item, or (None, drop-reason) when a frozen rule drops it."""

    formatted = format_cue(piece.text, policy)
    if formatted is None:
        return None, f"{piece.segment_id}:{piece.record_start}-empty-text"
    length = piece.record_end - piece.record_start
    if length < policy.min_duration_frames:
        return None, (
            f"{piece.segment_id}:{piece.record_start}-{piece.record_end}"
            f"-below-min-{policy.min_duration_frames}"
        )
    return (
        SubtitleCueItem(
            item_id=cue_item_id(piece, formatted, policy.default_style_ref),
            source=SourceRef(
                source_id=source_id,
                span=SourceFrameSpan(
                    start_frame=piece.source_start,
                    end_frame=piece.source_end,
                    rate=rate,
                ),
            ),
            record_span=RecordFrameSpan(
                start_frame=piece.record_start, end_frame=piece.record_end
            ),
            text=formatted.text,
            lines=formatted.lines,
            style_ref=policy.default_style_ref,
            safe_area=formatted.safe_area,
            min_duration_frames=policy.min_duration_frames,
        ),
        None,
    )


__all__ = [
    "CLAUSE_MARK",
    "CUE_FORMAT_RULE_ID",
    "MIN_DURATION_RULE",
    "SENTENCE_END_MARKS",
    "SPLIT_TEXT_RULE",
    "FormattedCue",
    "build_cue_item",
    "format_cue",
    "meaning_units",
    "normalize_cue_text",
    "wrap_lines",
]
