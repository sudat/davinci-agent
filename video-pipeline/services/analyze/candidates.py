"""The four PROPOSAL-ONLY editorial candidate generators.

These functions never delete speech, never trim, and never mutate an Edit
Plan: they consume read-only analysis inputs and return new immutable
``Candidate`` objects whose ``proposal_only`` flag is the literal ``True``.
Final keep/delete decisions belong to the downstream Selection Plan flow.

Frozen rules (see audio_constants):

- pause: a detected silence span bounded by transcript segment adjacency —
  clipped into the gap between the previous speech end and the next speech
  start, kept when the clipped span still satisfies MIN_SILENCE_MS;
- filler: leftmost occurrence with longest-match-wins against the frozen
  Japanese lexicon; the proposed span is the containing segment's span;
- false start: a speech segment that lacks terminal punctuation followed
  within the frozen gap by a speech segment restarting with the same
  leading character (deterministic on the declared transcript structure);
- dialogue vs ambient: RMS over raw samples of speech spans and silence
  spans reported in strictly separate fields.
"""

from __future__ import annotations

from itertools import pairwise

from services.analyze.analysis_models import (
    AnalyzeRequestError,
    DialogueAmbientSummary,
    SampleMsSpan,
    refuse_edit_plan,
)
from services.analyze.audio_constants import (
    CONFIDENCE_FALSE_START_CANDIDATE,
    CONFIDENCE_FILLER_CANDIDATE,
    CONFIDENCE_PAUSE_CANDIDATE,
    FALSE_START_RULE_ID,
    FILLER_LEXICON,
    FILLER_RULE_ID,
    MAX_FALSE_START_GAP_MS,
    MIN_SILENCE_MS,
    PAUSE_RULE_ID,
    SILENCE_FLOOR_MB,
    TERMINAL_PUNCTUATION,
)
from services.analyze.audio_measure import RawPcm, milli_belles
from services.analyze.candidate_models import (
    Candidate,
    CandidateProvenance,
    FalseStartEvidence,
    FillerEvidence,
    MappedTranscriptSegment,
    PauseEvidence,
    TranscriptSpanMap,
)


def _provenance(
    rule_id: str, analyzer_version: str, hashes: tuple[str, ...]
) -> CandidateProvenance:
    if not hashes:
        raise AnalyzeRequestError("candidate provenance requires input artifact hashes")
    return CandidateProvenance(
        analyzer_version=analyzer_version, rule_id=rule_id, input_artifact_hashes=hashes
    )


def speech_segments(span_map: TranscriptSpanMap) -> tuple[MappedTranscriptSegment, ...]:
    return tuple(segment for segment in span_map.segments if segment.is_speech)


def generate_pause_candidates(
    silence_spans: tuple[SampleMsSpan, ...],
    span_map: TranscriptSpanMap,
    analyzer_version: str,
    input_hashes: tuple[str, ...],
) -> tuple[Candidate, ...]:
    """Silence spans bounded by adjacent speech segments (proposals only)."""

    for value in silence_spans:
        refuse_edit_plan(value)
    speech = speech_segments(span_map)
    min_samples = span_map.sample_rate * MIN_SILENCE_MS // 1000
    candidates: list[Candidate] = []
    for silence in silence_spans:
        previous = [s for s in speech if s.span.end_sample <= silence.start_sample]
        following = [s for s in speech if s.span.start_sample >= silence.end_sample]
        if not previous or not following:
            continue
        prev_end = previous[-1].span.end_sample
        next_start = following[0].span.start_sample
        start = max(silence.start_sample, prev_end)
        end = min(silence.end_sample, next_start)
        if end - start < min_samples or end <= start:
            continue
        clipped = SampleMsSpan(
            start_sample=start,
            end_sample=end,
            sample_rate=silence.sample_rate,
            start_ms=start * 1000 // silence.sample_rate,
            end_ms=end * 1000 // silence.sample_rate,
        )
        candidates.append(
            Candidate(
                kind="pause",
                span=clipped,
                confidence=CONFIDENCE_PAUSE_CANDIDATE,
                provenance=_provenance(PAUSE_RULE_ID, analyzer_version, input_hashes),
                evidence=PauseEvidence(
                    prev_speech_end_sample=prev_end,
                    next_speech_start_sample=next_start,
                    detected_silence_samples=silence.length,
                ),
                proposal_only=True,
            )
        )
    return tuple(candidates)


def generate_filler_candidates(
    span_map: TranscriptSpanMap,
    analyzer_version: str,
    input_hashes: tuple[str, ...],
) -> tuple[Candidate, ...]:
    """Frozen Japanese filler lexicon text match (proposals only)."""

    candidates: list[Candidate] = []
    for segment in span_map.segments:
        text = segment.text
        position = 0
        while position < len(text):
            match = ""
            for entry in sorted(FILLER_LEXICON, key=len, reverse=True):
                if entry and text.startswith(entry, position):
                    match = entry
                    break
            if not match:
                position += 1
                continue
            candidates.append(
                Candidate(
                    kind="filler",
                    span=segment.span,
                    confidence=CONFIDENCE_FILLER_CANDIDATE,
                    provenance=_provenance(FILLER_RULE_ID, analyzer_version, input_hashes),
                    evidence=FillerEvidence(
                        segment_index=segment.index,
                        matched_text=match,
                        char_start=position,
                        char_end=position + len(match),
                        segment_text=text,
                    ),
                    proposal_only=True,
                )
            )
            position += len(match)
    return tuple(candidates)


def generate_false_start_candidates(
    span_map: TranscriptSpanMap,
    analyzer_version: str,
    input_hashes: tuple[str, ...],
) -> tuple[Candidate, ...]:
    """Deterministic false-start rule on the declared transcript structure."""

    speech = speech_segments(span_map)
    candidates: list[Candidate] = []
    for abandoned, restart in pairwise(speech):
        abandoned_text = abandoned.text.strip()
        restart_text = restart.text.strip()
        if not abandoned_text or not restart_text:
            continue
        if abandoned_text.endswith(TERMINAL_PUNCTUATION):
            continue
        gap_ms = restart.span.start_ms - abandoned.span.end_ms
        if gap_ms < 0 or gap_ms > MAX_FALSE_START_GAP_MS:
            continue
        if not restart_text.startswith(abandoned_text[0]):
            continue
        candidates.append(
            Candidate(
                kind="false_start",
                span=abandoned.span,
                confidence=CONFIDENCE_FALSE_START_CANDIDATE,
                provenance=_provenance(FALSE_START_RULE_ID, analyzer_version, input_hashes),
                evidence=FalseStartEvidence(
                    abandoned_segment_index=abandoned.index,
                    restart_segment_index=restart.index,
                    abandoned_text=abandoned.text,
                    restart_text=restart.text,
                    gap_ms=gap_ms,
                ),
                proposal_only=True,
            )
        )
    return tuple(candidates)


def compute_dialogue_ambient(
    pcm: RawPcm,
    span_map: TranscriptSpanMap,
    silence_spans: tuple[tuple[int, int], ...],
) -> DialogueAmbientSummary:
    """Dialogue RMS on speech spans vs ambient floor on silence spans —
    strictly separate fields computed from raw samples."""

    refuse_edit_plan(pcm)
    samples = pcm.samples
    total = len(samples)

    def _sum_sq(spans: tuple[tuple[int, int], ...]) -> tuple[int, int]:
        accumulator = 0
        count = 0
        for start, end in spans:
            for value in samples[start : min(end, total)]:
                accumulator += value * value
                count += 1
        return accumulator, count

    speech_spans = tuple(
        (segment.span.start_sample, segment.span.end_sample)
        for segment in span_map.segments
        if segment.is_speech
    )
    dialogue_sum, dialogue_count = _sum_sq(speech_spans)
    ambient_sum, ambient_count = _sum_sq(silence_spans)
    return DialogueAmbientSummary(
        dialogue_rms_mb=(
            milli_belles(dialogue_sum // dialogue_count) if dialogue_count else SILENCE_FLOOR_MB
        ),
        ambient_noise_floor_mb=(
            milli_belles(ambient_sum // ambient_count) if ambient_count else SILENCE_FLOOR_MB
        ),
        dialogue_sample_count=dialogue_count,
        ambient_sample_count=ambient_count,
    )


__all__ = [
    "compute_dialogue_ambient",
    "generate_false_start_candidates",
    "generate_filler_candidates",
    "generate_pause_candidates",
    "speech_segments",
]
