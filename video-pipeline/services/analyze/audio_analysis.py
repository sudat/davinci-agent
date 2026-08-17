"""End-to-end dialogue analysis entry point (probe -> measure -> candidates).

``analyze_dialogue`` accepts an ``AudioAnalysisRequest`` (artifact references
only — passing an Edit Plan is refused before anything executes), validates
decode against the pinned tools, computes deterministic measurements, maps
the transcript to sample spans, generates PROPOSAL-ONLY candidates, and
returns a new ``AnalysisArtifact``. Replay goes through the content-addressed
cache keyed on analyzer version + input artifact hashes + frozen constants
hash, so a cache hit re-runs no subprocess at all.

The analyzer stack is read-only: no function here mutates an Edit Plan, a
Timeline, or any input artifact; output is always a new immutable artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator

from services.analyze.analysis_models import (
    AnalyzeDecodeError,
    AnalyzeRequestError,
    AnalyzeTimeBaseError,
    AudioMeasurements,
    AudioProbeArtifact,
    StreamFacts,
    WavBinding,
    refuse_edit_plan,
)
from services.analyze.analyze_cache import AnalyzerCache, AnalyzerCacheBinding, cache_key
from services.analyze.asr_models import TranscriptArtifact  # noqa: TC001 (pydantic runtime field)
from services.analyze.audio_constants import (
    ANALYZER_VERSION,
    HOP_MS,
    WINDOW_MS,
    frozen_constants_hash,
)
from services.analyze.audio_measure import (
    RawPcm,
    compute_window_stats,
    detect_silence_spans,
    measure_loudness,
    peak_and_clipping,
    read_s16_mono_wav,
)
from services.analyze.audio_probe import (
    DEFAULT_LOCK_PATH,
    PinnedAudioTools,
    probe_audio,
    resolve_audio_tools,
)
from services.analyze.candidate_models import (
    ANALYSIS_PRODUCER,
    AnalysisArtifact,
    analysis_content_hash,
    input_refs,
)
from services.analyze.candidates import (
    compute_dialogue_ambient,
    generate_false_start_candidates,
    generate_filler_candidates,
    generate_pause_candidates,
)
from services.analyze.transcript_spans import map_transcript_to_samples
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

ARTIFACT_NAME = "analysis-artifact.json"


class AudioAnalysisRequest(StrictModel):
    wav_path: str
    wav_sha256: Sha256
    declared: StreamFacts
    transcript: TranscriptArtifact
    fixture_only: bool = False

    @field_validator("wav_path")
    @classmethod
    def require_local_file_path(cls, value: str) -> str:
        if "://" in value or not Path(value).is_absolute():
            raise ValueError(f"wav must be an absolute local file path, got {value!r}")
        return value


class AudioAnalysisResult(StrictModel):
    artifact: AnalysisArtifact
    artifact_path: str
    cache_status: Literal["hit", "miss"]
    probe: AudioProbeArtifact


def _measure(pcm: RawPcm, tools: PinnedAudioTools, wav: Path) -> AudioMeasurements:
    stats = compute_window_stats(pcm)
    silence = detect_silence_spans(stats, pcm.sample_rate)
    peak, peak_mb, clipping = peak_and_clipping(pcm)
    loudness = measure_loudness(tools.ffmpeg, wav, stats)
    return AudioMeasurements(
        window_ms=WINDOW_MS,
        hop_ms=HOP_MS,
        sample_rate=pcm.sample_rate,
        frame_count=len(pcm.samples),
        peak_sample=peak,
        peak_mb=peak_mb,
        clipping_count=clipping,
        window_stats=stats,
        silence_spans=silence,
        loudness=loudness,
    )


def _validate_or_raise(probe: AudioProbeArtifact) -> None:
    if probe.decode_ok:
        return
    failure = probe.failure
    code = failure.code if failure is not None else "corrupt_decode"
    detail = failure.detail if failure is not None else "decode validation failed"
    if code == "time_base_mismatch":
        raise AnalyzeTimeBaseError(detail)
    raise AnalyzeDecodeError(detail, probe)


def _analyze_fresh(
    request: AudioAnalysisRequest, wav: Path, lock_path: Path
) -> tuple[AnalysisArtifact, AudioProbeArtifact]:
    tools = resolve_audio_tools(lock_path)
    probe = probe_audio(wav, request.declared, tools=tools, lock_path=lock_path)
    _validate_or_raise(probe)

    pcm = read_s16_mono_wav(wav)
    if pcm.sample_rate != request.declared.sample_rate:
        raise AnalyzeTimeBaseError(
            f"parsed wav rate {pcm.sample_rate} != declared {request.declared.sample_rate}"
        )
    measurements = _measure(pcm, tools, wav)
    span_map = map_transcript_to_samples(request.transcript, request.declared.sample_rate)
    input_hashes = (request.wav_sha256, request.transcript.content_hash)
    candidates = (
        generate_pause_candidates(
            measurements.silence_spans, span_map, ANALYZER_VERSION, input_hashes
        )
        + generate_filler_candidates(span_map, ANALYZER_VERSION, input_hashes)
        + generate_false_start_candidates(span_map, ANALYZER_VERSION, input_hashes)
    )
    silence_pairs = tuple(
        (span.start_sample, span.end_sample) for span in measurements.silence_spans
    )
    dialogue_ambient = compute_dialogue_ambient(pcm, span_map, silence_pairs)
    wav_binding = WavBinding(
        wav_path=str(wav),
        wav_sha256=request.wav_sha256,
        declared=request.declared,
    )
    content_hash = analysis_content_hash(
        wav_binding,
        probe,
        measurements,
        span_map,
        candidates,
        dialogue_ambient,
        fixture_only=request.fixture_only,
    )
    artifact = AnalysisArtifact(
        artifact_id=f"analysis-{content_hash[:16]}",
        artifact_type="analysis_dialogue_candidates",
        schema_version="dialogue-analysis-v1",
        content_hash=content_hash,
        producer=ANALYSIS_PRODUCER,
        inputs=input_refs(request.wav_sha256, request.transcript.content_hash),
        wav=wav_binding,
        probe=probe,
        measurements=measurements,
        transcript_map=span_map,
        candidates=candidates,
        dialogue_ambient=dialogue_ambient,
        fixture_only=request.fixture_only,
    )
    return artifact, probe


def analyze_dialogue(
    request: object,
    *,
    work_dir: Path,
    cache_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> AudioAnalysisResult:
    refuse_edit_plan(request)
    if not isinstance(request, AudioAnalysisRequest):
        raise AnalyzeRequestError(
            "analyze_dialogue accepts an AudioAnalysisRequest only; "
            "Edit Plans and other objects are refused"
        )
    wav = Path(request.wav_path)
    if not wav.is_file() or sha256_file(wav) != request.wav_sha256:
        raise AnalyzeRequestError(f"wav missing or hash drift: {wav}")

    binding = AnalyzerCacheBinding(
        analyzer_version=ANALYZER_VERSION,
        wav_sha256=request.wav_sha256,
        transcript_sha256=request.transcript.content_hash,
        declared=request.declared,
        frozen_constants_hash=frozen_constants_hash(),
        wav_path=str(wav),
    )
    key = cache_key(
        analyzer_version=binding.analyzer_version,
        wav_sha256=binding.wav_sha256,
        transcript_sha256=binding.transcript_sha256,
        declared=binding.declared,
        frozen_constants_hash=binding.frozen_constants_hash,
    )
    cache = AnalyzerCache(cache_dir)
    cached = cache.lookup(key, binding)

    if cached is not None:
        artifact = AnalysisArtifact.model_validate_json(cached)
        artifact_bytes = cached
        probe = artifact.probe
        cache_status: Literal["hit", "miss"] = "hit"
    else:
        artifact, probe = _analyze_fresh(request, wav, lock_path)
        artifact_bytes = canonical_model_bytes(artifact)
        cache.store(key, binding, artifact_bytes)
        cache_status = "miss"

    work_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = work_dir / ARTIFACT_NAME
    atomic_write(artifact_path, artifact_bytes)
    return AudioAnalysisResult(
        artifact=artifact,
        artifact_path=str(artifact_path),
        cache_status=cache_status,
        probe=probe,
    )
