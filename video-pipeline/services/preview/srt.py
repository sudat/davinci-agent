"""Local SRT parsing/rendering for the preview adapter (Todo 17 precedent, reimplemented).

``services.preview`` must never import ``services.resolve_bridge`` — not even
for this small pure utility — so the strict parser and canonical renderer live
here. Cue bounds are integer milliseconds derived from record-frame spans.
"""

from __future__ import annotations

from fractions import Fraction

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    StrictModel,
)
from services.contracts.timeline_ir import TimelineItem0C  # noqa: TC001 (pydantic runtime)
from services.preview.models import PreviewBindingError, PreviewTraceError


class SubtitleCue(StrictModel):
    start_ms: int
    end_ms: int
    text: str

    @model_validator(mode="after")
    def require_forward_cue(self) -> SubtitleCue:
        if self.end_ms <= self.start_ms:
            raise PydanticCustomError("cue_empty", "subtitle cue bounds must be forward")
        return self


def cue_from_record_span(span: RecordFrameSpan, rate: RationalFrameRate, text: str) -> SubtitleCue:
    start = Fraction(span.start_frame * rate.den * 1000, rate.num)
    end = Fraction(span.end_frame * rate.den * 1000, rate.num)
    if start.denominator != 1 or end.denominator != 1:
        raise PreviewTraceError(
            f"subtitle record span [{span.start_frame},{span.end_frame}) "
            f"is not an exact millisecond bound at {rate.num}/{rate.den}"
        )
    return SubtitleCue(start_ms=int(start), end_ms=int(end), text=text)


def _stamp(ms: int) -> str:
    hours, rem = divmod(ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _ms(stamp: str) -> int:
    hours, minutes, seconds = stamp.split(":")
    seconds, _, millis = seconds.partition(",")
    total = int(hours) * 3600_000 + int(minutes) * 60_000 + int(seconds) * 1000
    return total + int(millis.ljust(3, "0")[:3])


def parse_srt(raw: bytes) -> tuple[SubtitleCue, ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PreviewBindingError(f"srt is not valid UTF-8: {error}") from error
    cues: list[SubtitleCue] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            raise PreviewBindingError(f"srt block without timing line: {lines[:1]}")
        start, end = (part.strip() for part in timing.split("-->", 1))
        body = "\n".join(lines[lines.index(timing) + 1 :])
        cues.append(SubtitleCue(start_ms=_ms(start), end_ms=_ms(end), text=body))
    if not cues:
        raise PreviewBindingError("srt contains no cues")
    return tuple(cues)


def render_srt(cues: tuple[SubtitleCue, ...]) -> bytes:
    blocks = [
        f"{index + 1}\n{_stamp(cue.start_ms)} --> {_stamp(cue.end_ms)}\n{cue.text}\n"
        for index, cue in enumerate(cues)
    ]
    return "\n".join(blocks).encode()


def expected_subtitle_cues(
    items: tuple[TimelineItem0C, ...], rate: RationalFrameRate
) -> tuple[SubtitleCue, ...]:
    cues = [
        cue_from_record_span(item.record_span, rate, item.subtitle_text or "") for item in items
    ]
    return tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.text)))


__all__ = [
    "SubtitleCue",
    "cue_from_record_span",
    "expected_subtitle_cues",
    "parse_srt",
    "render_srt",
]
