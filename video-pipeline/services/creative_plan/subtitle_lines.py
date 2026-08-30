"""Subtitle cue chunking (task 33; PRD §9.1.5).

``chunk_cue`` breaks one reconciled cue's normalized text with the
``subtitle_text`` rule tables and groups lines into cues of at most
``lines_per_cue`` lines; multi-chunk spans split proportionally by character
count using the frozen ``round_half_away`` rule (same rate math as conform).
Reading-speed enforcement over these chunks lives in ``subtitle_speed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from services.conform.rate_model import round_half_away
from services.creative_plan.subtitle_text import break_into_lines

if TYPE_CHECKING:
    from services.creative_plan.subtitle_models import (
        SubtitleReconciledCueV1,
        SubtitleStyleProfileV1,
    )


@dataclass(frozen=True, slots=True)
class WorkingCue:
    """Mutable-pipeline working cue (rebuilt, never mutated in place)."""

    cue_id: str
    transcript_ref: str
    source_id: str
    lines: tuple[str, ...]
    source_start: int
    source_end: int
    record_start: int
    record_end: int


def proportional_frame(total_frames: int, chars_at: int, total_chars: int) -> int:
    """Frame offset of *chars_at* in text split proportionally by char count."""
    return round_half_away(Fraction(total_frames * chars_at) / Fraction(total_chars))


def chunk_cue(
    cue: SubtitleReconciledCueV1,
    *,
    profile: SubtitleStyleProfileV1,
    atomic_spans: tuple[tuple[int, int], ...],
) -> list[WorkingCue]:
    """Break one reconciled cue into <= lines_per_cue-line cues."""
    lines = break_into_lines(cue.text, profile.chars_per_line, atomic_spans)
    total_chars = sum(len(line) for line in lines)
    total_frames = cue.record_end - cue.record_start
    groups = [
        lines[start : start + profile.lines_per_cue]
        for start in range(0, len(lines), profile.lines_per_cue)
    ]
    chunks: list[WorkingCue] = []
    consumed = 0
    for index, group in enumerate(groups):
        start_offset = consumed
        consumed += sum(len(line) for line in group)
        if len(groups) == 1:
            lead, tail = 0, total_frames
            cue_id = cue.cue_id
        else:
            lead = proportional_frame(total_frames, start_offset, total_chars)
            tail = proportional_frame(total_frames, consumed, total_chars)
            cue_id = f"{cue.cue_id}-{index + 1}"
        chunks.append(
            WorkingCue(
                cue_id=cue_id,
                transcript_ref=cue.transcript_ref,
                source_id=cue.source_id,
                lines=group,
                source_start=cue.start_frame + lead,
                source_end=cue.start_frame + tail,
                record_start=cue.record_start + lead,
                record_end=cue.record_start + tail,
            )
        )
    return chunks


__all__ = ["WorkingCue", "chunk_cue", "proportional_frame"]
