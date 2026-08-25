"""Local SRT parsing/rendering for the preview adapter (Todo 17 precedent, reimplemented).

``services.preview`` must never import ``services.resolve_bridge`` — not even
for this small pure utility — so the strict parser and canonical renderer live
here. Cue bounds are integer milliseconds derived from record-frame spans.
"""

from __future__ import annotations

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
    """Map a record-frame span to integer-millisecond SRT bounds.

    The frozen invariant required exact millisecond bounds (``denominator == 1``)
    at the locked mezzanine rate ``30/1``. At 30 fps one frame is 33.333 ms
    and only spans whose endpoints are multiples of 3 frames are exact — the
    real speech lattice (``LATTICE = 3`` in ``services.cli.real_pool``) keeps
    most bounds lattice-aligned, but the tail clamp (8467 frames, not divisible
    by 3) and timeline recompilation can emit off-lattice bounds such as
    ``[7926, 8005)`` (8005 * 1000/30 = 266833.333 ms). SRT requires integer
    milliseconds, so we round to the nearest ms (half-up). Max drift vs the
    exact frame instant is ``< 0.5 ms`` (at 30/1 the residue is at most
    ``1/3 ms ≈ 0.33 ms``; at ``30000/1001`` the same formula applies). The
    strict validator therefore remains safe: the cue is still ordered
    (``end_ms > start_ms`` is checked by ``SubtitleCue``) and the rendered
    SRT is integer ms.
    """

    start_ms = (span.start_frame * rate.den * 1000 + rate.num // 2) // rate.num
    end_ms = (span.end_frame * rate.den * 1000 + rate.num // 2) // rate.num
    # Keep a narrow guard: a collapsed or inverted rounded cue is still a
    # programming error (e.g. zero-length span) — surface as PreviewTraceError
    # so callers see the same error family as before.
    if end_ms <= start_ms:
        raise PreviewTraceError(
            f"subtitle record span [{span.start_frame},{span.end_frame}) "
            f"collapses to [{start_ms},{end_ms}) ms at {rate.num}/{rate.den}"
        )
    return SubtitleCue(start_ms=start_ms, end_ms=end_ms, text=text)


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
