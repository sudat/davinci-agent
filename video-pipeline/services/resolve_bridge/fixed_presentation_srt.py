"""Strict SRT parsing, recipe-derived cue bounds, and canonical SRT rendering."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.resolve_bridge.fixed_presentation_models import SubtitleCue

if TYPE_CHECKING:
    from services.fixtures.manifest import SubtitleRecipe


class SubtitleTextError(Exception):
    """The subtitle source is not valid UTF-8 SRT or disagrees with the recipe."""


def _ms(stamp: str) -> int:
    hours, minutes, seconds = stamp.split(":")
    seconds, _, millis = seconds.partition(",")
    total = int(hours) * 3600_000 + int(minutes) * 60_000 + int(seconds) * 1000
    return total + int(millis.ljust(3, "0")[:3])


def parse_srt(raw: bytes) -> tuple[SubtitleCue, ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SubtitleTextError(f"srt is not valid UTF-8: {error}") from error
    cues: list[SubtitleCue] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            raise SubtitleTextError(f"srt block without timing line: {lines[:1]}")
        start, end = (part.strip() for part in timing.split("-->", 1))
        body = "\n".join(lines[lines.index(timing) + 1 :])
        cues.append(SubtitleCue(start_ms=_ms(start), end_ms=_ms(end), text=body))
    if not cues:
        raise SubtitleTextError("srt contains no cues")
    return tuple(cues)


def cue_from_recipe(recipe: SubtitleRecipe, rate_num: int, rate_den: int) -> SubtitleCue:
    span = recipe.record_span
    start = Fraction(span.start_frame * rate_den * 1000, rate_num)
    end = Fraction(span.end_frame * rate_den * 1000, rate_num)
    if start.denominator != 1 or end.denominator != 1:
        raise SubtitleTextError("subtitle record span is not an exact millisecond bound")
    return SubtitleCue(start_ms=int(start), end_ms=int(end), text=recipe.text)


def _stamp(ms: int) -> str:
    hours, rem = divmod(ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(cue: SubtitleCue) -> bytes:
    return f"1\n{_stamp(cue.start_ms)} --> {_stamp(cue.end_ms)}\n{cue.text}\n".encode()


def load_fixed_cue(
    srt_path: Path, recipe: SubtitleRecipe, rate_num: int, rate_den: int
) -> SubtitleCue:
    cues = parse_srt(srt_path.read_bytes())
    if len(cues) != 1:
        raise SubtitleTextError(f"fixed-subtitle-v1 requires exactly one cue, found {len(cues)}")
    cue = cues[0]
    expected = cue_from_recipe(recipe, rate_num, rate_den)
    if cue != expected:
        raise SubtitleTextError(
            f"srt cue {cue.span_seconds} {cue.text!r} != table "
            f"{expected.span_seconds} {expected.text!r}"
        )
    return cue
