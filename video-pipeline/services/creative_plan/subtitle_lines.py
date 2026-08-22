"""Subtitle line chunking + reading-speed enforcement (task 33; PRD §9.1.5-6).

``chunk_cue`` breaks one reconciled cue's normalized text with the
``subtitle_text`` rule tables and groups lines into cues of at most
``lines_per_cue`` lines; multi-chunk spans split proportionally by character
count using the frozen ``round_half_away`` rule (same rate math as conform).

``enforce_reading_speed`` walks cues in record order. Reading speed is
chars/second on the RECORD span. An over-limit cue splits at a rule break
opportunity nearest the character midpoint (line boundaries first) and each
piece re-checks against its own allocated span: pieces get at least the
frames ``ceil(chars * rate / cps_max)`` needs — earlier pieces may push later
starts forward, and the trailing piece may extend into the airtime before the
next cue's start (or the timeline end); at least one frame is reserved for
every later piece so ordering and the no-overlap invariant always hold.
Pieces that still exceed the limit after allocation (no break opportunity and
no airtime left) are kept and flagged ``reading_speed_violation``; cues under
the minimum are kept and flagged ``reading_speed_under`` — never silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from services.conform.rate_model import ceil_fraction, round_half_away
from services.creative_plan.subtitle_models import (
    ReadingSpeedViolationV1,
    SubtitleReconciledCueV1,
    SubtitleStyleProfileV1,
)
from services.creative_plan.subtitle_text import allowed_break_offsets, break_into_lines

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate


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


@dataclass(frozen=True, slots=True)
class SpeedOutcome:
    """Enforced cues plus the explicit violations that could not be fixed."""

    cues: tuple[WorkingCue, ...]
    violations: tuple[ReadingSpeedViolationV1, ...]


def _boundary(total_frames: int, chars_at: int, total_chars: int) -> int:
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
            lead = _boundary(total_frames, start_offset, total_chars)
            tail = _boundary(total_frames, consumed, total_chars)
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


def _is_over(text: str, frames: int, rate: Fraction, cps_max: Fraction) -> bool:
    return Fraction(len(text)) * rate / Fraction(frames) > cps_max


def _split_over(
    text: str,
    candidates: frozenset[int],
    *,
    frames: int,
    rate: Fraction,
    cps_max: Fraction,
) -> list[str]:
    if not _is_over(text, frames, rate, cps_max) or not candidates:
        return [text]
    cut = min(candidates, key=lambda o: (abs(Fraction(o) - Fraction(len(text), 2)), o))
    left_text, right_text = text[:cut], text[cut:]
    left_frames = max(1, _boundary(frames, len(left_text), len(text)))
    left = _split_over(
        left_text,
        frozenset(o for o in candidates if o < cut),
        frames=left_frames,
        rate=rate,
        cps_max=cps_max,
    )
    right = _split_over(
        right_text,
        frozenset(o - cut for o in candidates if o > cut),
        frames=max(1, frames - left_frames),
        rate=rate,
        cps_max=cps_max,
    )
    return left + right


def _pieces_for_speed(
    cue: WorkingCue, *, rate: Fraction, cps_max: Fraction
) -> list[tuple[str, ...]]:
    frames = cue.record_end - cue.record_start
    text = "".join(cue.lines)
    if not _is_over(text, frames, rate, cps_max):
        return [cue.lines]
    line_offsets: set[int] = set()
    consumed = 0
    for line in cue.lines[:-1]:
        consumed += len(line)
        line_offsets.add(consumed)
    candidates = frozenset(line_offsets | set(allowed_break_offsets(text)))
    parts = _split_over(text, candidates, frames=frames, rate=rate, cps_max=cps_max)
    return [(part,) for part in parts]


def enforce_reading_speed(
    cues: list[WorkingCue],
    *,
    rate: RationalFrameRate,
    profile: SubtitleStyleProfileV1,
    timeline_end: int,
) -> SpeedOutcome:
    """Split/extend over-limit cues; flag what cannot be fixed."""
    rate_fraction = rate.as_fraction
    cps_max = Fraction(profile.reading_speed_max_cps)
    cps_min = Fraction(profile.reading_speed_min_cps)
    ordered = sorted(cues, key=lambda cue: (cue.record_start, cue.cue_id))
    enforced: list[WorkingCue] = []
    violations: list[ReadingSpeedViolationV1] = []
    for index, cue in enumerate(ordered):
        following = ordered[index + 1].record_start if index + 1 < len(ordered) else timeline_end
        hard_end = max(following, cue.record_end)
        pieces = _pieces_for_speed(cue, rate=rate_fraction, cps_max=cps_max)
        total_chars = sum(len(line) for piece in pieces for line in piece)
        total_frames = cue.record_end - cue.record_start
        cursor = cue.record_start
        consumed = 0
        for position, piece in enumerate(pieces):
            chars = sum(len(line) for line in piece)
            consumed += chars
            needed = ceil_fraction(Fraction(chars) * rate_fraction / cps_max)
            proportional_end = cue.record_start + _boundary(total_frames, consumed, total_chars)
            cap = hard_end - (len(pieces) - 1 - position)
            end = min(max(proportional_end, cursor + needed), cap)
            cps = Fraction(chars) * rate_fraction / Fraction(end - cursor)
            cue_id = cue.cue_id if len(pieces) == 1 else f"{cue.cue_id}-{position + 1}"
            enforced.append(
                WorkingCue(
                    cue_id=cue_id,
                    transcript_ref=cue.transcript_ref,
                    source_id=cue.source_id,
                    lines=piece,
                    source_start=cue.source_start
                    + _boundary(total_frames, consumed - chars, total_chars),
                    source_end=cue.source_start + _boundary(total_frames, consumed, total_chars),
                    record_start=cursor,
                    record_end=end,
                )
            )
            if cps > cps_max:
                violations.append(
                    ReadingSpeedViolationV1(
                        cue_id=cue_id,
                        transcript_ref=cue.transcript_ref,
                        kind="reading_speed_violation",
                        note=(
                            f"{chars} chars over {end - cursor} frames "
                            f"({float(cps):.2f} cps > {profile.reading_speed_max_cps} cps); "
                            "no break opportunity and no airtime left"
                        ),
                        measured_cps=float(cps),
                        limit_cps=profile.reading_speed_max_cps,
                    )
                )
            elif cps < cps_min:
                violations.append(
                    ReadingSpeedViolationV1(
                        cue_id=cue_id,
                        transcript_ref=cue.transcript_ref,
                        kind="reading_speed_under",
                        note=(
                            f"{chars} chars over {end - cursor} frames "
                            f"({float(cps):.2f} cps < {profile.reading_speed_min_cps} cps); "
                            "slow cue flagged for review"
                        ),
                        measured_cps=float(cps),
                        limit_cps=profile.reading_speed_min_cps,
                    )
                )
            cursor = end
    return SpeedOutcome(cues=tuple(enforced), violations=tuple(violations))


__all__ = ["SpeedOutcome", "WorkingCue", "chunk_cue", "enforce_reading_speed"]
