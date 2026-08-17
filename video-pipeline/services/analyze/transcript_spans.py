"""Sample-bound transcript mapping via exact rational arithmetic.

ASR segments are integer milliseconds (Todo 33 contract). Mapping ms to
samples goes through ``Fraction(ms, 1000) * sample_rate`` and requires an
integer result — supported rates (16000/48000) always satisfy this, and any
off-lattice request is refused rather than rounded. Token-level timing is
mapped identically but kept under the Todo-33 experimental label; it is
evidence, never canonical.
"""

from __future__ import annotations

from fractions import Fraction

from services.analyze.analysis_models import (
    AnalyzeRequestError,
    SampleMsSpan,
    refuse_edit_plan,
)
from services.analyze.asr_models import TranscriptArtifact
from services.analyze.audio_constants import SUPPORTED_SAMPLE_RATES
from services.analyze.candidate_models import (
    MappedTokenSpan,
    MappedTranscriptSegment,
    TranscriptSpanMap,
)


def _sample_for_ms(ms: int, sample_rate: int, what: str) -> int:
    exact = Fraction(ms, 1000) * sample_rate
    if exact.denominator != 1 or exact < 0:
        raise AnalyzeRequestError(
            f"{what} {ms}ms does not land on an integer sample at {sample_rate} Hz"
        )
    return exact.numerator


def _span_for_ms(start_ms: int, end_ms: int, sample_rate: int, what: str) -> SampleMsSpan:
    start = _sample_for_ms(start_ms, sample_rate, what)
    end = _sample_for_ms(end_ms, sample_rate, what)
    if end < start:
        raise AnalyzeRequestError(f"{what} span {start_ms}-{end_ms}ms is inverted")
    return SampleMsSpan(
        start_sample=start,
        end_sample=end,
        sample_rate=sample_rate,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def map_transcript_to_samples(
    transcript: TranscriptArtifact, sample_rate: int
) -> TranscriptSpanMap:
    """Map every segment (and experimental token) to exact sample spans."""

    refuse_edit_plan(transcript)
    if not isinstance(transcript, TranscriptArtifact):
        raise AnalyzeRequestError("map_transcript_to_samples requires a TranscriptArtifact")
    if sample_rate not in SUPPORTED_SAMPLE_RATES:
        raise AnalyzeRequestError(
            f"unsupported sample rate {sample_rate}; supported: {SUPPORTED_SAMPLE_RATES}"
        )
    segments = tuple(
        MappedTranscriptSegment(
            index=index,
            text=segment.text,
            is_speech=bool(segment.text.strip()),
            span=_span_for_ms(
                segment.start_ms, segment.end_ms, sample_rate, f"segment[{index}]"
            ),
        )
        for index, segment in enumerate(transcript.segments)
    )
    token_spans: list[MappedTokenSpan] = []
    experimental = transcript.experimental
    if experimental.token_level is not None:
        token_spans.extend(
            MappedTokenSpan(
                segment_index=group.segment_index,
                text=token.text,
                span=_span_for_ms(
                    token.start_ms,
                    token.end_ms,
                    sample_rate,
                    f"token {token.text!r}",
                ),
            )
            for group in experimental.token_level
            for token in group.tokens
        )
    return TranscriptSpanMap(
        sample_rate=sample_rate,
        segments=segments,
        token_spans=tuple(token_spans),
    )
