"""Reading-speed enforcement over chunked subtitle cues (task 33; PRD §9.1.5-6).

``enforce_reading_speed`` walks cues in record order. Reading speed is
chars/second on the RECORD span. WORD UNITY IS HARD, cps IS SOFT (r4
priority swap, 2026-09-04): a cue text listed in ``_WORD_UNIT_SPLITS``
(carried from real-episode review — no morphological analyzer) is cut
ONLY at its reviewed word-safe offsets, and every resulting piece stays
whole however far it exceeds the limit. Otherwise an over-limit cue
splits at a rule break opportunity nearest the character midpoint —
STRONG boundaries (clause punctuation and the chunker's own line
offsets) win exclusively when present, because weak particle hits can
land inside compound words; a piece left without a strong boundary
stays whole and is flagged rather than cut mid-word — and each piece
re-checks against its own allocated span. Consecutive fragments
carrying the same ``transcript_ref`` (pieces of one reconciled segment) form
one run that shares its extent and airtime: pieces get at least the frames
``max(ceil(chars * rate / cps_max), min_duration_frames)`` needs, and when
the run cannot host that many cues at the QC display minimum, adjacent
pieces MERGE (lines carried verbatim, at most ``lines_per_cue`` of them)
until the count fits — chunking one segment therefore never emits cues
shorter than the policy minimum unless even maximal merging cannot fit the
run extent, in which case the starved run keeps one-frame ordering reserves
and stays explicit through violations. A run never borrows a different
segment's time: it ends at the next DIFFERENT-ref cue's start (or the
timeline end), so ordering and the no-overlap invariant always hold. Pieces
that still exceed the limit after allocation (no break opportunity and no
airtime left) are kept and flagged ``reading_speed_violation``; cues under
the minimum are kept and flagged ``reading_speed_under`` — never silent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from services.conform.rate_model import ceil_fraction
from services.creative_plan.subtitle_lines import WorkingCue, proportional_frame
from services.creative_plan.subtitle_models import (
    ReadingSpeedViolationV1,
    SubtitleStyleProfileV1,
)
from services.creative_plan.subtitle_text import allowed_break_offsets

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate


@dataclass(frozen=True, slots=True)
class SpeedOutcome:
    """Enforced cues plus the explicit violations that could not be fixed."""

    cues: tuple[WorkingCue, ...]
    violations: tuple[ReadingSpeedViolationV1, ...]


def _is_over(text: str, frames: int, rate: Fraction, cps_max: Fraction) -> bool:
    return Fraction(len(text)) * rate / Fraction(frames) > cps_max


# Clause punctuation: break opportunities that can never land inside a word
# (v44-real-01 st68 evidence — a bare か/な particle hit cut おか|しくな mid-word).
_STRONG_BREAK_CHARS: Final[frozenset[str]] = frozenset("、。！？…・")  # noqa: RUF001

# Word-unit split table (r4, 2026-09-04, Opus-approved priority swap): cue
# texts whose ONLY word-safe split offsets are pinned here after review.
# Word unity is the HARD constraint and cps the SOFT one: the strong tier
# below cannot guarantee word unity because the chunker's own line wrap
# (break_into_lines) may itself cut a compound word — st68's wrap landed on
# おかしくな|いんじゃない and the strong tier trusted that line offset
# (r3, rejected). Deterministic by design (no morphological analyzer,
# PRD §9.2): entries come only from real-episode review evidence, and
# pieces that still exceed the speed limit stay whole and flagged.
_WORD_UNIT_SPLITS: Final[dict[str, tuple[int, ...]]] = {
    # v44-real-01 st68 (C-live run 5082-5145, 63f): cut after あっても only
    # -> ま、あっても / おかしくないんじゃないのかな.
    "ま、あってもおかしくないんじゃないのかな": (6,),
}


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
    left_frames = max(1, proportional_frame(frames, len(left_text), len(text)))
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
    rule_offsets = allowed_break_offsets(text)
    word_units = _WORD_UNIT_SPLITS.get(text)
    if word_units is not None:
        # Hard word unity: the reviewed offsets are the only cuts allowed;
        # recursion finds no further candidates, so every piece stays whole
        # and any cps excess is flagged downstream instead of cut mid-word.
        candidates = frozenset(o for o in word_units if 0 < o < len(text))
    else:
        # Strong boundaries (clause punctuation + the chunker's own line
        # offsets) win exclusively when present; weak particle hits can
        # land inside compound words, so a piece left without a strong
        # boundary stays whole and is flagged rather than cut mid-word.
        # Weak-only cues (no clause punctuation, single line) keep the
        # particle boundaries as before.
        strong = line_offsets | {
            offset for offset in rule_offsets if text[offset - 1] in _STRONG_BREAK_CHARS
        }
        candidates = frozenset(strong or rule_offsets)
    parts = _split_over(text, candidates, frames=frames, rate=rate, cps_max=cps_max)
    return [(part,) for part in parts]


class _RunPiece(NamedTuple):
    """One flattened piece of a same-``transcript_ref`` run of cues."""

    piece_id: str
    lines: tuple[str, ...]
    chars_before: int
    chars_after: int


def _run_pieces(
    run: Sequence[WorkingCue], *, rate: Fraction, cps_max: Fraction
) -> list[_RunPiece]:
    """Speed-split every cue of a run into flat, text-ordered pieces."""
    pieces: list[_RunPiece] = []
    consumed = 0
    for cue in run:
        parts = _pieces_for_speed(cue, rate=rate, cps_max=cps_max)
        for position, part in enumerate(parts):
            chars = sum(len(line) for line in part)
            pieces.append(
                _RunPiece(
                    piece_id=cue.cue_id
                    if len(parts) == 1
                    else f"{cue.cue_id}-{position + 1}",
                    lines=part,
                    chars_before=consumed,
                    chars_after=consumed + chars,
                )
            )
            consumed += chars
    return pieces


def _pack_groups(
    pieces: Sequence[_RunPiece], *, capacity: int, lines_per_cue: int
) -> list[list[_RunPiece]]:
    """Merge adjacent pieces until the count fits *capacity* cues.

    A merged cue carries the merged pieces' lines verbatim (at most
    *lines_per_cue* of them), so text, order, and the per-line width limits
    are preserved. When even maximal merging cannot reach the capacity the
    pieces come back unmerged — the starved run stays explicit through
    reading-speed violations instead of silent short cues pretending to pass.
    """
    if len(pieces) <= capacity:
        return [[piece] for piece in pieces]
    groups: list[list[_RunPiece]] = []
    current: list[_RunPiece] = []
    lines = 0
    for piece in pieces:
        if current and lines + len(piece.lines) > lines_per_cue:
            groups.append(current)
            current, lines = [], 0
        current.append(piece)
        lines += len(piece.lines)
    if current:
        groups.append(current)
    return groups if len(groups) <= capacity else [[piece] for piece in pieces]


def _speed_violation(
    cue: WorkingCue,
    *,
    cps: Fraction,
    kind: Literal["reading_speed_violation", "reading_speed_under"],
    profile: SubtitleStyleProfileV1,
) -> ReadingSpeedViolationV1:
    """Build one over/under reading-speed report (never silent)."""
    over = kind == "reading_speed_violation"
    limit = profile.reading_speed_max_cps if over else profile.reading_speed_min_cps
    chars = sum(len(line) for line in cue.lines)
    frames = cue.record_end - cue.record_start
    detail = "no break opportunity and no airtime left" if over else "slow cue flagged for review"
    return ReadingSpeedViolationV1(
        cue_id=cue.cue_id,
        transcript_ref=cue.transcript_ref,
        kind=kind,
        note=(
            f"{chars} chars over {frames} frames "
            f"({float(cps):.2f} cps {'>' if over else '<'} {limit} cps); {detail}"
        ),
        measured_cps=float(cps),
        limit_cps=limit,
    )


def enforce_reading_speed(
    cues: list[WorkingCue],
    *,
    rate: RationalFrameRate,
    profile: SubtitleStyleProfileV1,
    timeline_end: int,
) -> SpeedOutcome:
    """Split/extend over-limit cues; floor every cue at the profile minimum.

    Fragments of one transcript segment (same ``transcript_ref``) are
    allocated as one run: pieces merge into fewer cues until the count fits
    the run extent at ``min_duration_frames`` each, and the run never borrows
    a different segment's time — it ends at the next different-ref cue's
    start (or the timeline end).
    """
    rate_fraction = rate.as_fraction
    cps_max = Fraction(profile.reading_speed_max_cps)
    cps_min = Fraction(profile.reading_speed_min_cps)
    min_frames = profile.min_duration_frames
    ordered = sorted(cues, key=lambda cue: (cue.record_start, cue.cue_id))
    enforced: list[WorkingCue] = []
    violations: list[ReadingSpeedViolationV1] = []
    start = 0
    while start < len(ordered):
        ref = ordered[start].transcript_ref
        stop = start
        while stop < len(ordered) and ordered[stop].transcript_ref == ref:
            stop += 1
        run = ordered[start : stop]
        following = ordered[stop].record_start if stop < len(ordered) else timeline_end
        hard_end = max(following, *(cue.record_end for cue in run))
        extent = hard_end - run[0].record_start
        run_frames = run[-1].record_end - run[0].record_start
        pieces = _run_pieces(run, rate=rate_fraction, cps_max=cps_max)
        total_chars = pieces[-1].chars_after
        capacity = max(1, extent // min_frames)
        groups = _pack_groups(pieces, capacity=capacity, lines_per_cue=profile.lines_per_cue)
        reserve = min_frames if len(groups) * min_frames <= extent else 1
        cursor = run[0].record_start
        for position, group in enumerate(groups):
            chars = group[-1].chars_after - group[0].chars_before
            needed = max(
                ceil_fraction(Fraction(chars) * rate_fraction / cps_max), min_frames
            )
            proportional_end = run[0].record_start + proportional_frame(
                run_frames, group[-1].chars_after, total_chars
            )
            cap = hard_end - (len(groups) - 1 - position) * reserve
            end = min(max(proportional_end, cursor + needed), cap)
            cps = Fraction(chars) * rate_fraction / Fraction(end - cursor)
            merged = WorkingCue(
                cue_id=group[0].piece_id,
                transcript_ref=ref,
                source_id=run[0].source_id,
                lines=tuple(line for piece in group for line in piece.lines),
                source_start=run[0].source_start
                + proportional_frame(run_frames, group[0].chars_before, total_chars),
                source_end=run[0].source_start
                + proportional_frame(run_frames, group[-1].chars_after, total_chars),
                record_start=cursor,
                record_end=end,
            )
            enforced.append(merged)
            if cps > cps_max:
                violations.append(
                    _speed_violation(
                        merged, cps=cps, kind="reading_speed_violation", profile=profile
                    )
                )
            elif cps < cps_min:
                violations.append(
                    _speed_violation(
                        merged, cps=cps, kind="reading_speed_under", profile=profile
                    )
                )
            cursor = end
        start = stop
    return SpeedOutcome(cues=tuple(enforced), violations=tuple(violations))


__all__ = ["SpeedOutcome", "enforce_reading_speed"]
