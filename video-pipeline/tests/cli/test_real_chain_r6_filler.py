"""r6 end-to-end regression: the real derivation chain corroborates ``fi127``.

The r6 blocker (``ep-043e1e769957f8c1``): filler candidate ``fi127`` — a
「えー」 lexicon match inside transcript segment 「えーと」 at 135.72-138.08s —
was declared with EMPTY text (``director_request`` attaches speech-keyed text
only), and the evidence gate had no corroboration path for it. This test does
NOT hardcode the r6 frame numbers: it runs the real derivation chain over the
r6 segment shape and proves the containment holds —

    TranscriptArtifact (ms 135720-138080, 「えーと」)
      → speech_segments           → declared speech s1 on the 3-frame lattice
      → map_transcript_to_samples → generate_filler_candidates
      → pool_for/_audio_records   → declared filler with EMPTY text, span
                                    floor-start/floor-end frames [4071, 4142)
      → director_request          → the r6 declaration table
      → real media.duckdb index   → assemble_evidence corroborates the filler
                                    by transcript containment.

Every stage is production code; the only fixture is the transcript segment
itself. Before the containment rule the final call raised ``EvidenceIncomplete``
deterministically on the same inputs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from services.analyze.analysis_models import (
    AudioMeasurements,
    AudioProbeArtifact,
    DialogueAmbientSummary,
    LoudnessSummary,
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
from services.analyze.audio_constants import ANALYZER_VERSION
from services.analyze.candidate_models import (
    ANALYSIS_PRODUCER,
    AnalysisArtifact,
    analysis_content_hash,
    input_refs,
)
from services.analyze.candidates import generate_filler_candidates
from services.analyze.transcript_spans import map_transcript_to_samples
from services.cli.real_director import director_request
from services.cli.real_pool import pool_for
from services.contracts.primitives import Producer
from services.editorial.evidence import assemble_evidence
from services.media_query.api import MediaQueryApi
from services.media_query.index import MEDIA_DB_NAME, MediaQueryIndex
from tests.editorial.support import load_manifest

R6_SEGMENT_START_MS = 135720
R6_SEGMENT_END_MS = 138080
R6_SEGMENT_TEXT = "えーと"
R6_MATCHED_TEXT = "えー"
R6_TOTAL_FRAMES = 8468  # 282.2s real edit source, ceil on the 3-frame lattice


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _transcript_artifact() -> TranscriptArtifact:
    """The r6 transcript shape: one segment 「えーと」 at 135720-138080ms."""

    segments = (
        TranscriptSegment(
            start_ms=R6_SEGMENT_START_MS,
            end_ms=R6_SEGMENT_END_MS,
            text=R6_SEGMENT_TEXT,
        ),
    )
    binding = AsrInputBinding(
        media_path="/real/DJI_20260824114257_0039_D.MP4",
        media_sha256=_hex("r6:media"),
        wav_sha256=_hex("r6:wav"),
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
        producer=Producer(name="asr-whisper-cpp", version="todo33-v1"),
        inputs=(),
        input_binding=binding,
        segments=segments,
        experimental=experimental,
        settings_echo=echo,
    )


def _analysis_artifact(transcript: TranscriptArtifact) -> AnalysisArtifact:
    """Real dialogue analysis over the r6 transcript (filler candidates only)."""

    wav = WavBinding(
        wav_path="/real/camera-001.wav",
        wav_sha256=_hex("r6:wav"),
        declared=StreamFacts(sample_rate=48000),
    )
    probe = AudioProbeArtifact(
        decode_ok=True,
        facts=StreamFacts(sample_rate=48000),
        ffprobe_sha256=_hex("r6:ffprobe"),
        ffmpeg_sha256=_hex("r6:ffmpeg"),
    )
    measurements = AudioMeasurements(
        window_ms=100,
        hop_ms=100,
        sample_rate=48000,
        frame_count=R6_TOTAL_FRAMES * 1600,
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
    dialogue = DialogueAmbientSummary(
        dialogue_rms_mb=-6000,
        ambient_noise_floor_mb=-120000,
        dialogue_sample_count=0,
        ambient_sample_count=0,
    )
    mapped = map_transcript_to_samples(transcript, 48000)
    candidates = generate_filler_candidates(
        mapped, ANALYZER_VERSION, (_hex("r6:wav"), _hex("r6:transcript"))
    )
    content_hash = analysis_content_hash(
        wav,
        probe,
        measurements,
        mapped,
        candidates,
        dialogue,
        fixture_only=False,
    )
    return AnalysisArtifact(
        artifact_id=f"analysis-{content_hash[:16]}",
        artifact_type="analysis_dialogue_candidates",
        schema_version="dialogue-analysis-v1",
        content_hash=content_hash,
        producer=ANALYSIS_PRODUCER,
        inputs=input_refs(_hex("r6:wav"), _hex("r6:transcript")),
        wav=wav,
        probe=probe,
        measurements=measurements,
        transcript_map=mapped,
        candidates=candidates,
        dialogue_ambient=dialogue,
        fixture_only=False,
    )


def test_r6_derivation_chain_corroborates_the_empty_text_filler(tmp_path: Path) -> None:
    transcript = _transcript_artifact()
    analysis = _analysis_artifact(transcript)
    pool, speech = pool_for(
        transcript,
        analysis,
        analysis.inputs,
        source_id="camera-001",
        edit_source_sha=_hex("r6:edit-source"),
        total_frames=R6_TOTAL_FRAMES,
    )

    # r6 shape: the speech record carries the transcript text, the filler
    # record carries NONE — speech_text is keyed by speech ids only.
    # (r6's id was fi127 because 126 pause candidates preceded it; with a
    # single candidate the same derivation yields fi1.)
    assert [record.segment_id for record in pool.segments] == ["s1", "fi1"]
    assert [record.kind for record in pool.segments] == ["speech", "filler"]

    request = director_request(
        episode_id="ep-043e1e769957f8c1",
        source_id="camera-001",
        total_frames=R6_TOTAL_FRAMES,
        rules=load_manifest("p1-ref-01-clean-ja").editorial_rules,
        pool=pool,
        speech_text={segment.segment_id: segment.text for segment in speech},
    )
    declared = {candidate.segment_id: candidate for candidate in request.candidates}
    assert declared["fi1"].text == ""  # the exact r6 declaration shape
    assert declared["fi1"].kind == "filler"
    assert (declared["fi1"].start_frame, declared["fi1"].end_frame) == (4071, 4142)
    assert (declared["s1"].start_frame, declared["s1"].end_frame) == (4071, 4143)

    db_path = tmp_path / MEDIA_DB_NAME
    with MediaQueryIndex.open(db_path) as index:
        index.upsert_analyzer_artifact(transcript)
    api = MediaQueryApi.open(db_path)
    try:
        bundle = assemble_evidence(api, request)
    finally:
        api.close()

    assert [(c.segment_id, c.method) for c in bundle.corroborations] == [
        ("s1", "transcript_search"),
        ("fi1", "transcript_containment"),
    ]
    assert tuple(bundle.admissible_segment_ids) == ("s1", "fi1")
    assert analysis.candidates[0].evidence.matched_text == R6_MATCHED_TEXT
