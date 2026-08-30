"""Candidate pool + rule spec + cue table derived from REAL analyzer output.

Everything here is deterministic code over the published artifacts: speech
candidates come from the whisper transcript (frame-quantized at CFR30,
evidence-bound to the published transcript + dialogue refs), pause/filler/
false-start records from the Todo-34 dialogue candidates, and the editorial
rule spec is the frozen Phase-1 shape with a keep-all-speech budget sized to
the episode. No model output enters this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.analyze.audio_constants import (
    FALSE_START_RULE_ID,
    FILLER_RULE_ID,
)
from services.compile.subtitle_policy import (
    FrameCueSpan,
    TranscriptCueSegment,
    TranscriptCueSource,
)
from services.editorial.candidate_models import (
    AnalyzerSegmentRecord,
    CandidatePool,
    CandidateProvenance,
    CandidateSpan,
    EvidenceIndex,
)
from services.fixtures.manifest_phase1 import (
    DurationBudgetRules,
    EditorialRules,
    MustIncludeRules,
    OrderingRules,
    PauseRules,
    RetakeRules,
    ScoringRules,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.analyze.asr_models import TranscriptArtifact
    from services.analyze.candidate_models import AnalysisArtifact
    from services.contracts.primitives import ArtifactRef

RATE_NUM: Final = 30
RATE_DEN: Final = 1
LATTICE: Final = 3  # 30 fps: only frames divisible by 3 are exact millisecond bounds
SPEECH_RULE_ID: Final = "real-transcript-speech-v1"
NO_MODEL_MARKER: Final = "no-model-involved"


class RealPoolError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    segment_id: str
    text: str
    start_frame: int
    end_frame: int


def _span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=RATE_NUM,
        rate_den=RATE_DEN,
        start_ms=start_frame * 1000 // RATE_NUM,
        end_ms=end_frame * 1000 // RATE_NUM,
    )


def _frame(ms: int, *, end: bool) -> int:
    if end:
        return (ms * RATE_NUM + 999) // 1000
    return ms * RATE_NUM // 1000


def segments_from_ms(
    segments_ms: Sequence[tuple[int, int, str]], total_frames: int
) -> tuple[SpeechSegment, ...]:
    """Non-empty ``(start_ms, end_ms, text)`` spans on the exact-ms frame
    lattice, ordered ``s1..sN``.

    The ONE deterministic ms→frame conversion, shared by the ASR transcript
    path (:func:`speech_segments`) and the operator corrected-sample path
    (Task-3 diagnostic lane). 30fps half-open lattice: floor-start snapped
    down to a multiple of 3, ceil-end snapped up; the ≤LATTICE boundary
    residue collapses to contiguity and clamps at the source tail. A genuine
    overlap beyond the residue, a span past the source, and a collapsed span
    stay typed refusals; no non-empty input is a typed refusal too.
    """

    segments: list[SpeechSegment] = []
    position = 0
    previous_end = 0
    for start_ms, end_ms, text in segments_ms:
        if not text.strip():
            continue
        position += 1
        start = _frame(start_ms, end=False) // LATTICE * LATTICE
        end = _ceil_lattice(max(_frame(end_ms, end=True), start + 1))
        # Lattice residue, MEASURED on v44-real-01 real speech (99 segments,
        # 73 collisions): adjacent segments share one boundary timestamp, and
        # ceil-end (N → next multiple of 3) vs floor-start (same N → previous
        # multiple of 3) collide by up to LATTICE frames. Collapse that
        # residue to contiguity (previous end == next start — the same true
        # boundary, no frame invented or lost) and clamp the same ≤LATTICE
        # residue at the source tail (ceil pushed 8467.44 → 8469 past 8467).
        # A GENUINE overlap beyond the residue is still a typed refusal.
        if start < previous_end:
            if previous_end - start > LATTICE:
                raise RealPoolError(
                    "speech_span_invalid",
                    f"transcript segment {position} span [{start},{end}) overlaps the "
                    f"previous segment by {previous_end - start} frames",
                )
            start = previous_end
        if end > total_frames:
            if end - total_frames > LATTICE:
                raise RealPoolError(
                    "speech_span_invalid",
                    f"transcript segment {position} span [{start},{end}) exceeds the "
                    f"edit source [0,{total_frames})",
                )
            end = total_frames
        if end <= start:
            raise RealPoolError(
                "speech_span_invalid",
                f"transcript segment {position} span collapses at [{start},{end}) "
                f"inside [0,{total_frames})",
            )
        previous_end = end
        segments.append(
            SpeechSegment(
                segment_id=f"s{position}", text=text.strip(),
                start_frame=start, end_frame=end,
            )
        )
    if not segments:
        raise RealPoolError(
            "speech_absent", "the transcript carries no non-empty speech segments"
        )
    return tuple(segments)


def speech_segments(
    transcript: TranscriptArtifact, total_frames: int
) -> tuple[SpeechSegment, ...]:
    """Non-empty transcript segments on the exact-ms frame lattice, ordered."""
    return segments_from_ms(
        tuple((raw.start_ms, raw.end_ms, raw.text) for raw in transcript.segments),
        total_frames,
    )


def _ceil_lattice(frame: int) -> int:
    return (frame + LATTICE - 1) // LATTICE * LATTICE


def _audio_records(
    analysis: AnalysisArtifact, evidence: tuple[ArtifactRef, ...], total_frames: int
) -> tuple[AnalyzerSegmentRecord, ...]:
    rows: list[AnalyzerSegmentRecord] = []
    for position, candidate in enumerate(analysis.candidates, start=1):
        start = candidate.span.start_sample * RATE_NUM // 48000
        end = candidate.span.end_sample * RATE_NUM // 48000
        if end <= start or end > total_frames:
            continue
        rows.append(
            AnalyzerSegmentRecord(
                segment_id=f"{candidate.kind[:2]}{position}",
                kind=candidate.kind,
                span=_span(start, end),
                evidence=evidence,
                provenance=CandidateProvenance(
                    analyzer_version=analysis.producer.version,
                    rule_ids=(
                        {
                            "pause": "p1-pauses-v1",
                            "filler": FILLER_RULE_ID,
                            "false_start": FALSE_START_RULE_ID,
                        }[candidate.kind],
                    ),
                ),
            )
        )
    return tuple(rows)


def pool_for(  # noqa: PLR0913 (pool derivation binds every real evidence input)
    transcript: TranscriptArtifact,
    analysis: AnalysisArtifact,
    evidence: tuple[ArtifactRef, ...],
    *,
    source_id: str,
    edit_source_sha: str,
    total_frames: int,
) -> tuple[CandidatePool, tuple[SpeechSegment, ...]]:
    speech = speech_segments(transcript, total_frames)
    records = [
        AnalyzerSegmentRecord(
            segment_id=segment.segment_id,
            kind="speech",
            span=_span(segment.start_frame, segment.end_frame),
            evidence=evidence,
            provenance=CandidateProvenance(
                analyzer_version=transcript.producer.version,
                rule_ids=(SPEECH_RULE_ID,),
            ),
        )
        for segment in speech
    ]
    pool = CandidatePool(
        source_id=source_id,
        edit_source_sha=edit_source_sha,
        total_frames=total_frames,
        segments=(*records, *_audio_records(analysis, evidence, total_frames)),
    )
    return pool, speech


def evidence_index_for(
    evidence: tuple[ArtifactRef, ...], edit_source_sha: str
) -> EvidenceIndex:
    return EvidenceIndex(rows=evidence, edit_source_sha=edit_source_sha)


def rules_for(total_frames: int, speech_ids: tuple[str, ...]) -> EditorialRules:
    return EditorialRules(
        scoring=ScoringRules(
            rule_id="p1-scoring-v1",
            content_weight=2,
            clarity_weight=1,
            min_speech_score=20,
        ),
        pauses=PauseRules(rule_id="p1-pauses-v1", delete_threshold_frames=15),
        retakes=RetakeRules(rule_id="p1-retakes-v1", selection="max-score-per-group"),
        duration_budget=DurationBudgetRules(
            rule_id="p1-budget-v1",
            max_output_frames=total_frames,
            min_output_frames=1,
            enforcement="drop-lowest-score-non-must",
        ),
        ordering=OrderingRules(rule_id="p1-ordering-v1", rule="source-order-stable"),
        must_include=MustIncludeRules(
            rule_id="p1-must-include-v1", segment_ids=speech_ids
        ),
    )


def cue_source_for(speech: tuple[SpeechSegment, ...]) -> TranscriptCueSource:
    return TranscriptCueSource(
        language="ja",
        segments=tuple(
            TranscriptCueSegment(
                segment_id=segment.segment_id,
                text=segment.text,
                span=FrameCueSpan(
                    start_frame=segment.start_frame, end_frame=segment.end_frame
                ),
            )
            for segment in speech
        ),
    )


__all__ = [
    "NO_MODEL_MARKER",
    "RealPoolError",
    "SpeechSegment",
    "cue_source_for",
    "evidence_index_for",
    "pool_for",
    "rules_for",
    "segments_from_ms",
    "speech_segments",
]
