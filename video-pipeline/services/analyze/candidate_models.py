"""Candidate and transcript-span models for the dialogue analyzers.

Candidates are PROPOSALS ONLY (``proposal_only`` is the literal ``True`` and
can never be anything else): they propose half-open sample spans with
confidence permille, full provenance (analyzer version, rule id, input
artifact hashes), and kind-specific measured evidence. No candidate type
carries any deletion, trim, or plan-mutation capability.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.analyze.analysis_models import (  # noqa: TC001 (pydantic runtime fields)
    AudioMeasurements,
    AudioProbeArtifact,
    DialogueAmbientSummary,
    SampleMsSpan,
    WavBinding,
)
from services.analyze.audio_constants import ANALYZER_NAME, ANALYZER_VERSION
from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactRef,
    Producer,
    Sha256,
    StrictModel,
)


class CandidateProvenance(StrictModel):
    analyzer_version: str = Field(min_length=1, strict=True)
    rule_id: str = Field(min_length=1, strict=True)
    input_artifact_hashes: tuple[str, ...] = Field(min_length=1)


class PauseEvidence(StrictModel):
    """A silence span bounded by adjacent speech segments."""

    prev_speech_end_sample: int = Field(ge=0, strict=True)
    next_speech_start_sample: int = Field(ge=0, strict=True)
    detected_silence_samples: int = Field(gt=0, strict=True)


class FillerEvidence(StrictModel):
    segment_index: int = Field(ge=0, strict=True)
    matched_text: str = Field(min_length=1, strict=True)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    segment_text: str


class FalseStartEvidence(StrictModel):
    abandoned_segment_index: int = Field(ge=0, strict=True)
    restart_segment_index: int = Field(ge=0, strict=True)
    abandoned_text: str
    restart_text: str
    gap_ms: int = Field(ge=0, strict=True)


CandidateEvidence = PauseEvidence | FillerEvidence | FalseStartEvidence


class Candidate(StrictModel):
    kind: Literal["pause", "filler", "false_start"]
    span: SampleMsSpan
    confidence: int = Field(ge=0, le=1000, strict=True)
    provenance: CandidateProvenance
    evidence: CandidateEvidence
    proposal_only: Literal[True] = True

    @model_validator(mode="after")
    def require_evidence_kind_match(self) -> Candidate:
        wanted: dict[str, type] = {
            "pause": PauseEvidence,
            "filler": FillerEvidence,
            "false_start": FalseStartEvidence,
        }
        if not isinstance(self.evidence, wanted[self.kind]):
            raise PydanticCustomError(
                "evidence_kind_mismatch", "evidence model must match candidate kind"
            )
        return self


class MappedTranscriptSegment(StrictModel):
    """One transcript segment mapped to exact sample positions."""

    index: int = Field(ge=0, strict=True)
    text: str
    is_speech: bool
    span: SampleMsSpan


class MappedTokenSpan(StrictModel):
    """Token-level timing mapped to samples — EXPERIMENTAL evidence only."""

    segment_index: int = Field(ge=0, strict=True)
    text: str
    span: SampleMsSpan


class TranscriptSpanMap(StrictModel):
    sample_rate: int = Field(gt=0, strict=True)
    segments: tuple[MappedTranscriptSegment, ...] = Field(min_length=1)
    experimental_label: Literal["experimental-token-timing"] = "experimental-token-timing"
    token_spans: tuple[MappedTokenSpan, ...] = ()


class AnalysisArtifact(ArtifactEnvelope[Literal["analysis_dialogue_candidates"]]):
    wav: WavBinding
    probe: AudioProbeArtifact
    measurements: AudioMeasurements
    transcript_map: TranscriptSpanMap
    candidates: tuple[Candidate, ...]
    dialogue_ambient: DialogueAmbientSummary
    fixture_only: bool = False

    @model_validator(mode="after")
    def require_content_hash_binding(self) -> AnalysisArtifact:
        expected = analysis_content_hash(
            self.wav,
            self.probe,
            self.measurements,
            self.transcript_map,
            self.candidates,
            self.dialogue_ambient,
            fixture_only=self.fixture_only,
        )
        if self.content_hash != expected:
            raise PydanticCustomError(
                "content_hash_mismatch", "content_hash must bind the canonical content bytes"
            )
        return self


def analysis_content_hash(  # noqa: PLR0913, PLR0917 (envelope content fields are the record)
    wav: WavBinding,
    probe: AudioProbeArtifact,
    measurements: AudioMeasurements,
    transcript_map: TranscriptSpanMap,
    candidates: tuple[Candidate, ...],
    dialogue_ambient: DialogueAmbientSummary,
    *,
    fixture_only: bool,
) -> str:
    payload = {
        "wav": wav.model_dump(mode="json"),
        "probe": probe.model_dump(mode="json"),
        "measurements": measurements.model_dump(mode="json"),
        "transcript_map": transcript_map.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "dialogue_ambient": dialogue_ambient.model_dump(mode="json"),
        "fixture_only": fixture_only,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


ANALYSIS_PRODUCER = Producer(name=ANALYZER_NAME, version=ANALYZER_VERSION)


def input_refs(wav_sha256: Sha256, transcript_sha256: str) -> tuple[ArtifactRef, ...]:
    return (
        ArtifactRef(artifact_id="edit-source-audio", sha256=wav_sha256),
        ArtifactRef(artifact_id="transcript-asr-whisper-cpp", sha256=transcript_sha256),
    )
