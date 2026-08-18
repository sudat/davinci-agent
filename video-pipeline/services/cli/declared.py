"""Declared-observation analyzer evidence for the Phase-1 fixture chain.

The frozen Phase-1 fixtures pre-register their transcript spans and analyzer
observations in the manifest. The deterministic chain replays the DECLARED
observations through the real Todo-38 orchestrator machinery: these builders
produce the transcript and dialogue-analysis artifacts the orchestrator
publishes and indexes — no clocks, no randomness, no media-derived bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.analysis_models import (
    AudioMeasurements,
    AudioProbeArtifact,
    DialogueAmbientSummary,
    LoudnessSummary,
    SampleMsSpan,
    StreamFacts,
    WavBinding,
)
from services.analyze.asr_models import (
    AsrInputBinding,
    AsrSettingsEcho,
    ExperimentalTiming,
    TranscriptArtifact,
    TranscriptSegment,
    WavProbeSummary,
    transcript_content_hash,
)
from services.analyze.audio_constants import ANALYZER_VERSION as AUDIO_VERSION
from services.analyze.candidate_models import (
    AnalysisArtifact,
    Candidate,
    CandidateProvenance,
    MappedTranscriptSegment,
    PauseEvidence,
    TranscriptSpanMap,
    analysis_content_hash,
)
from services.analyze.orchestrator_models import AnalyzerSpec
from services.contracts.primitives import Producer
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.toolchain.models import Phase1TechnicalToolchainLock

TRANSCRIPT_PRODUCER: Final = Producer(name="asr-whisper-cpp", version="todo33-v1")
ANALYSIS_PRODUCER: Final = Producer(name="analyze-dialogue", version="todo34-v1")
_FRAME_MS_NUM: Final = 1000


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _frame_ms(frame: int) -> int:
    return frame * _FRAME_MS_NUM // 30


def _sample_span(start_frame: int, end_frame: int) -> SampleMsSpan:
    return SampleMsSpan(
        start_sample=start_frame * 1600,
        end_sample=end_frame * 1600,
        sample_rate=48000,
        start_ms=_frame_ms(start_frame),
        end_ms=_frame_ms(end_frame),
    )


def declared_transcript(manifest: Phase1TechnicalFixtureManifest) -> TranscriptArtifact:
    fixture_id = manifest.fixture_id
    segments = tuple(
        TranscriptSegment(
            start_ms=_frame_ms(segment.span.start_frame),
            end_ms=_frame_ms(segment.span.end_frame),
            text=segment.text,
        )
        for segment in manifest.transcript.segments
    )
    binding = AsrInputBinding(
        media_path=f"/synthetic/{fixture_id}.m4v",
        media_sha256=_hex(f"{fixture_id}:media"),
        wav_sha256=_hex(f"{fixture_id}:wav"),
        wav_probe=WavProbeSummary(),
    )
    experimental = ExperimentalTiming(
        label="experimental-token-timing", token_level=None, consistent_with_segments=True
    )
    echo = AsrSettingsEcho(
        ffmpeg_argv=("/synthetic/bin/ffmpeg", "-nostdin"),
        whisper_argv=("/synthetic/bin/whisper-cli", "--model", "/synthetic/models/ggml.bin"),
    )
    content_hash = transcript_content_hash(binding, segments, experimental, echo)
    return TranscriptArtifact(
        artifact_id=f"transcript-{content_hash[:16]}",
        artifact_type="transcript_asr_whisper_cpp",
        schema_version="asr-transcript-v1",
        content_hash=content_hash,
        producer=TRANSCRIPT_PRODUCER,
        inputs=(),
        input_binding=binding,
        segments=segments,
        experimental=experimental,
        settings_echo=echo,
    )


def declared_analysis(manifest: Phase1TechnicalFixtureManifest) -> AnalysisArtifact:
    fixture_id = manifest.fixture_id
    wav = WavBinding(
        wav_path=f"/synthetic/{fixture_id}.wav",
        wav_sha256=_hex(f"{fixture_id}:wav"),
        declared=StreamFacts(sample_rate=48000),
    )
    probe = AudioProbeArtifact(
        decode_ok=True,
        facts=StreamFacts(sample_rate=48000),
        ffprobe_sha256=_hex(f"{fixture_id}:ffprobe"),
        ffmpeg_sha256=_hex(f"{fixture_id}:ffmpeg"),
    )
    measurements = AudioMeasurements(
        window_ms=100,
        hop_ms=100,
        sample_rate=48000,
        frame_count=manifest.edit_source.total_frames * 1600,
        peak_sample=0,
        peak_mb=0,
        clipping_count=0,
        window_stats=(),
        silence_spans=(),
        loudness=LoudnessSummary(
            method="rms_fallback",
            honest_label="rms_based_not_bs1770",
            integrated_loudness_mlufs=None,
            rms_mean_mb=-6000,
        ),
    )
    transcript_map = TranscriptSpanMap(
        sample_rate=48000,
        segments=tuple(
            MappedTranscriptSegment(
                index=index,
                text=segment.text,
                is_speech=segment.kind == "speech",
                span=_sample_span(segment.span.start_frame, segment.span.end_frame),
            )
            for index, segment in enumerate(manifest.transcript.segments)
        ),
    )
    dialogue = DialogueAmbientSummary(
        dialogue_rms_mb=-6000,
        ambient_noise_floor_mb=-120000,
        dialogue_sample_count=0,
        ambient_sample_count=0,
    )
    pauses = tuple(
        (index, segment)
        for index, segment in enumerate(manifest.transcript.segments)
        if segment.kind == "pause"
    )
    candidates: list[Candidate] = []
    for index, segment in pauses:
        previous = manifest.transcript.segments[index - 1] if index > 0 else None
        following = (
            manifest.transcript.segments[index + 1]
            if index + 1 < len(manifest.transcript.segments)
            else None
        )
        candidates.append(
            Candidate(
                kind="pause",
                span=_sample_span(segment.span.start_frame, segment.span.end_frame),
                confidence=min(1000, segment.observed.pause_ms),
                provenance=CandidateProvenance(
                    analyzer_version=AUDIO_VERSION,
                    rule_id="p1-pauses-v1",
                    input_artifact_hashes=(_hex(f"{fixture_id}:wav"),),
                ),
                evidence=PauseEvidence(
                    prev_speech_end_sample=(
                        previous.span.end_frame * 1600 if previous is not None else 0
                    ),
                    next_speech_start_sample=(
                        following.span.start_frame * 1600 if following is not None else 0
                    ),
                    detected_silence_samples=(
                        segment.span.end_frame - segment.span.start_frame
                    )
                    * 1600,
                ),
            )
        )
    content_hash = analysis_content_hash(
        wav, probe, measurements, transcript_map, tuple(candidates), dialogue, fixture_only=True
    )
    return AnalysisArtifact(
        artifact_id=f"analysis-{content_hash[:16]}",
        artifact_type="analysis_dialogue_candidates",
        schema_version="dialogue-analysis-v1",
        content_hash=content_hash,
        producer=ANALYSIS_PRODUCER,
        inputs=(),
        wav=wav,
        probe=probe,
        measurements=measurements,
        transcript_map=transcript_map,
        candidates=tuple(candidates),
        dialogue_ambient=dialogue,
        fixture_only=True,
    )


def declared_specs(lock: Phase1TechnicalToolchainLock) -> tuple[AnalyzerSpec, ...]:
    del lock  # the declared analyzers carry no model/prompt hash by contract
    return (
        AnalyzerSpec(
            analyzer_name=TRANSCRIPT_PRODUCER.name,
            analyzer_version=TRANSCRIPT_PRODUCER.version,
            parameter_hash=_hex("phase1-declared-transcript-v1"),
        ),
        AnalyzerSpec(
            analyzer_name=ANALYSIS_PRODUCER.name,
            analyzer_version=ANALYSIS_PRODUCER.version,
            parameter_hash=_hex("phase1-declared-analysis-v1"),
        ),
    )


def policy_hashes(lock_path: Path) -> tuple[str, str]:
    """(toolchain lock sha, translator gate policy sha) for bundle lineage."""

    return (
        sha256_file(lock_path),
        sha256_file(Path("config/gates/phase-0c-v1.json")),
    )


__all__ = [
    "ANALYSIS_PRODUCER",
    "TRANSCRIPT_PRODUCER",
    "declared_analysis",
    "declared_specs",
    "declared_transcript",
    "policy_hashes",
]
