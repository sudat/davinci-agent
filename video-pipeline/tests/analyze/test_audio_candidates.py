"""Deterministic dialogue-audio analyzers: designed-audio golden tests.

Every expectation below is pre-registered from the SYNTHESIS parameters and
the frozen analyzer constants (window grid, thresholds, lexicon) — nothing is
measured-then-asserted. Designed audio is synthesized with the pinned ffmpeg
(lavfi ``aevalsrc``) so that:

- a square tone at +/-0.5 gives every window exactly ``10000*log10(0.25)`` mB;
- a 440 Hz sine at 0.5 gives every window exactly ``10000*log10(0.125)`` mB
  and an ebur128 integrated loudness of ``rms_mb - 691`` mLU within the
  frozen K-weighting tolerance (K-weighting is ~0 dB at 440 Hz);
- pauses land on exact hop-aligned sample spans and are detected exactly;
- clipping is a +/-1.0 square region counted sample-exactly.

The golden tie-in realizes the ``p1-ref-02-pauses-fillers`` manifest recipe
audio (48 kHz mezzanine, declared frame spans at 1600 samples/frame) and
asserts the analyzers cover its declared pause/filler spans at rule level.
"""

from __future__ import annotations

import json
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.analyze.analysis_models import (
    AnalyzeDecodeError,
    AnalyzeRequestError,
    AnalyzeTimeBaseError,
    DialogueAmbientSummary,
    LoudnessSummary,
    StreamFacts,
)
from services.analyze.analyze_cache import (
    AnalyzeCacheConflictError,
    AnalyzerCache,
    AnalyzerCacheBinding,
    cache_key,
)
from services.analyze.asr_models import (
    TRANSCRIPT_PRODUCER,
    AsrInputBinding,
    AsrSettingsEcho,
    ExperimentalTiming,
    TranscriptArtifact,
    TranscriptSegment,
    WavProbeSummary,
    transcript_content_hash,
)
from services.analyze.audio_analysis import AudioAnalysisRequest, analyze_dialogue
from services.analyze.audio_constants import (
    ANALYZER_VERSION,
    FILLER_LEXICON,
    MIN_SILENCE_MS,
    WINDOW_MS,
    frozen_constants_hash,
)
from services.analyze.audio_measure import read_s16_mono_wav
from services.analyze.candidate_models import (
    Candidate,
    FalseStartEvidence,
    FillerEvidence,
    PauseEvidence,
)
from services.analyze.candidates import (
    compute_dialogue_ambient,
    generate_false_start_candidates,
    generate_filler_candidates,
    generate_pause_candidates,
)
from services.analyze.transcript_spans import map_transcript_to_samples
from services.contracts.edit_plan_0c import EditPlan0C
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from tests.analyze.conftest import PinnedAsrStack

# --- pre-registered golden values (derived from synthesis parameters only) ---
MB_SQUARE_HALF = -6021  # 10000*log10(0.25) = -6020.60 -> round-half-away
MB_SINE_HALF = -9031  # 10000*log10(0.125) = -9030.90 -> round-half-away
MB_FLOOR = -120000  # digital-silence floor
EBUR128_OFFSET_MLU = -691  # steady tone: LUFS = RMS dBFS - 0.691
EBUR128_TOLERANCE_MLU = 150  # K-weighting (~0 dB at 440 Hz) + 0.1 LU print step

MANIFEST_PATH = Path("tests/fixtures/manifests/phase-1-technical/p1-ref-02-pauses-fillers.json")
SQUARE = r"if(lt(mod(t*440\,1)\,0.5)\,0.5\,-0.5)"
SQUARE_FS = r"if(lt(mod(t*440\,1)\,0.5)\,1\,-1)"
SINE_HALF = "sin(2*PI*440*t)*0.5"


@dataclass(frozen=True, slots=True)
class SynthStack:
    ffmpeg: Path
    ffprobe: Path
    root: Path


def _run_ffmpeg(ffmpeg: Path, argv: tuple[str, ...], out: Path) -> None:
    result = subprocess.run(
        (str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert out.is_file()


def _lavfi(expr: str, rate: int, duration: str) -> tuple[str, ...]:
    return ("-f", "lavfi", "-i", f"aevalsrc=exprs={expr}:s={rate}:d={duration}")


def _concat(tmp: Path, ffmpeg: Path, inputs: tuple[tuple[str, str], ...], rate: int) -> Path:
    argv: list[str] = []
    for expr, duration in inputs:
        argv.extend(_lavfi(expr, rate, duration))
    chain = "".join(f"[{index}:a]" for index in range(len(inputs)))
    argv.extend(
        (
            "-filter_complex",
            f"{chain}concat=n={len(inputs)}:v=0:a=1",
            "-c:a",
            "pcm_s16le",
        )
    )
    existing = len(list(tmp.iterdir()))
    out = tmp / f"synth-{rate}-{existing:02d}.wav"
    _run_ffmpeg(ffmpeg, (*argv, str(out)), out)
    return out


def _atrim_assemble(
    tmp: Path,
    ffmpeg: Path,
    tone: Path,
    silence: Path,
    cuts: tuple[tuple[str, int, int], ...],
) -> Path:
    parts = []
    for index, (source, start, end) in enumerate(cuts):
        src = "0:a" if source == "tone" else "1:a"
        parts.append(
            f"[{src}]atrim=start_sample={start}:end_sample={end},asetpts=N/SR/TB[g{index}]"
        )
    chain = "".join(f"[g{index}]" for index in range(len(cuts)))
    existing = len(list(tmp.iterdir()))
    out = tmp / f"assembled-{existing:02d}.wav"
    _run_ffmpeg(
        ffmpeg,
        (
            "-i",
            str(tone),
            "-i",
            str(silence),
            "-filter_complex",
            ";".join(parts) + f";{chain}concat=n={len(cuts)}:v=0:a=1",
            "-c:a",
            "pcm_s16le",
            str(out),
        ),
        out,
    )
    return out


def _frame_count(path: Path) -> int:
    with wave.open(str(path)) as stream:
        return stream.getnframes()


@pytest.fixture(scope="session")
def synth(pinned_asr: PinnedAsrStack, tmp_path_factory: pytest.TempPathFactory) -> SynthStack:
    return SynthStack(
        ffmpeg=pinned_asr.ffmpeg,
        ffprobe=pinned_asr.ffprobe,
        root=tmp_path_factory.mktemp("todo34-synth"),
    )


@pytest.fixture(scope="session")
def designed48(synth: SynthStack) -> Path:
    """48 kHz: tone 5s / silence 0.5s / tone 7s -> pause [240000, 264000)."""
    return _concat(
        synth.root,
        synth.ffmpeg,
        ((SQUARE, "5"), ("0", "0.5"), (SQUARE, "7")),
        48000,
    )


@pytest.fixture(scope="session")
def designed16(synth: SynthStack) -> Path:
    """16 kHz ASR-path wav: tone 3s / silence 0.6s / tone 5.4s."""
    return _concat(
        synth.root,
        synth.ffmpeg,
        ((SQUARE, "3"), ("0", "0.6"), (SQUARE, "5.4")),
        16000,
    )


@pytest.fixture(scope="session")
def sine48(synth: SynthStack) -> Path:
    return _concat(synth.root, synth.ffmpeg, ((SINE_HALF, "5"),), 48000)


@pytest.fixture(scope="session")
def clip48(synth: SynthStack) -> Path:
    """48 kHz: 1s half square / 0.25s full-scale square / 0.25s half square."""
    return _concat(
        synth.root,
        synth.ffmpeg,
        ((SQUARE, "1"), (SQUARE_FS, "0.25"), (SQUARE, "0.25")),
        48000,
    )


@pytest.fixture(scope="session")
def trunc48(designed48: Path, synth: SynthStack) -> Path:
    data = designed48.read_bytes()
    out = synth.root / "truncated.wav"
    out.write_bytes(data[: 44 + 599977])  # odd data tail -> partial s16 sample
    return out


@pytest.fixture(scope="session")
def golden48(synth: SynthStack) -> Path:
    """Realizes the p1-ref-02 manifest recipe: 48 kHz, 827 frames * 1600."""
    tone = _concat(synth.root, synth.ffmpeg, ((SQUARE, "30"),), 48000)
    silence = _concat(synth.root, synth.ffmpeg, (("0", "30"),), 48000)
    segs = (
        ("tone", 0, 240000),
        ("silence", 240000, 262400),
        ("tone", 262400, 502400),
        ("tone", 502400, 550400),
        ("silence", 550400, 574400),
        ("tone", 574400, 814400),
        ("silence", 814400, 843200),
        ("tone", 843200, 1083200),
        ("tone", 1083200, 1323200),
    )
    wav = _atrim_assemble(synth.root, synth.ffmpeg, tone, silence, segs)
    assert _frame_count(wav) == 827 * 1600
    return wav


def build_transcript(
    segments: tuple[tuple[int, int, str], ...],
    *,
    wav_sha: str = "a" * 64,
) -> TranscriptArtifact:
    binding = AsrInputBinding(
        media_path="/var/media/episode.m4v",
        media_sha256="b" * 64,
        wav_sha256=wav_sha,
        wav_probe=WavProbeSummary(),
    )
    parsed = tuple(
        TranscriptSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in segments
    )
    experimental = ExperimentalTiming(
        label="experimental-token-timing", token_level=None, consistent_with_segments=True
    )
    echo = AsrSettingsEcho(
        ffmpeg_argv=("/usr/local/bin/ffmpeg", "-nostdin"),
        whisper_argv=("/usr/local/bin/whisper-cli", "--model", "/models/ggml.bin"),
    )
    content_hash = transcript_content_hash(binding, parsed, experimental, echo)
    return TranscriptArtifact(
        artifact_id=f"transcript-{content_hash[:16]}",
        artifact_type="transcript_asr_whisper_cpp",
        schema_version="asr-transcript-v1",
        content_hash=content_hash,
        producer=TRANSCRIPT_PRODUCER,
        inputs=(),
        input_binding=binding,
        segments=parsed,
        experimental=experimental,
        settings_echo=echo,
    )


def _request(wav: Path, transcript: TranscriptArtifact, rate: int) -> AudioAnalysisRequest:
    return AudioAnalysisRequest(
        wav_path=str(wav),
        wav_sha256=sha256_file(wav),
        declared=StreamFacts.model_validate(
            {"sample_rate": rate, "channels": 1, "codec": "pcm_s16le"}
        ),
        transcript=transcript,
    )


def _as_filler(candidate: Candidate) -> FillerEvidence:
    assert isinstance(candidate.evidence, FillerEvidence)
    return candidate.evidence


def _as_false_start(candidate: Candidate) -> FalseStartEvidence:
    assert isinstance(candidate.evidence, FalseStartEvidence)
    return candidate.evidence


def _assert_ebur128_exact(loudness: LoudnessSummary) -> None:
    assert loudness.method == "ebur128"
    assert loudness.honest_label == "itu_r_bs_1770_ebur128"
    assert loudness.rms_mean_mb == MB_SINE_HALF
    integrated = loudness.integrated_loudness_mlufs
    assert integrated is not None
    assert abs(integrated - (MB_SINE_HALF + EBUR128_OFFSET_MLU)) <= EBUR128_TOLERANCE_MLU


def _analyze(request: AudioAnalysisRequest, tmp_path: Path):
    return analyze_dialogue(request, work_dir=tmp_path / "work", cache_dir=tmp_path / "cache")


def _pause_candidate(candidates: tuple[Candidate, ...]) -> Candidate:
    pauses = [candidate for candidate in candidates if candidate.kind == "pause"]
    assert len(pauses) == 1
    return pauses[0]


def test_designed_pauses_fillers_clipping_detected(
    designed48: Path, sine48: Path, clip48: Path, tmp_path: Path
) -> None:
    transcript = build_transcript(
        (
            (0, 5000, "今日は撮影の裏側をお見せします。"),
            (5000, 5500, ""),
            (5500, 12500, "まずカメラのセッティングからです。"),
        )
    )
    request = _request(designed48, transcript, 48000)
    first = _analyze(request, tmp_path)
    assert first.cache_status == "miss"

    artifact = first.artifact
    assert first.probe.decode_ok is True
    assert first.probe.facts == StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le")

    measurements = artifact.measurements
    assert measurements.frame_count == 600000
    assert measurements.peak_sample == 16384
    assert measurements.peak_mb == MB_SQUARE_HALF
    assert measurements.clipping_count == 0
    assert len(measurements.silence_spans) == 1
    span = measurements.silence_spans[0]
    assert (span.start_sample, span.end_sample) == (240000, 264000)
    assert (span.start_ms, span.end_ms) == (5000, 5500)

    pause = _pause_candidate(artifact.candidates)
    assert (pause.span.start_sample, pause.span.end_sample) == (240000, 264000)
    assert isinstance(pause.evidence, PauseEvidence)
    assert pause.evidence.prev_speech_end_sample == 240000
    assert pause.evidence.next_speech_start_sample == 264000
    assert pause.evidence.detected_silence_samples == 24000
    assert pause.proposal_only is True

    for candidate in artifact.candidates:
        assert 0 <= candidate.confidence <= 1000
        assert candidate.provenance.analyzer_version == ANALYZER_VERSION
        assert request.wav_sha256 in candidate.provenance.input_artifact_hashes
        assert transcript.content_hash in candidate.provenance.input_artifact_hashes
        assert candidate.proposal_only is True

    # Dialogue vs Ambient stay strictly separate fields with exact values
    summary = artifact.dialogue_ambient
    assert summary.dialogue_rms_mb == MB_SQUARE_HALF
    assert summary.ambient_noise_floor_mb == MB_FLOOR
    assert summary.dialogue_sample_count == 576000  # 5 s + 7 s of speech at 48 kHz
    assert summary.ambient_sample_count == 24000

    # sine loudness: every window exact; ebur128 integrated within tolerance
    sine_transcript = build_transcript(((0, 5000, "テストトーンです。"),))
    sine = _analyze(_request(sine48, sine_transcript, 48000), tmp_path)
    sine_stats = sine.artifact.measurements.window_stats
    assert len(sine_stats) == 99  # (240000 - 4800) // 2400 + 1 full windows
    assert all(stat.rms_mb == MB_SINE_HALF for stat in sine_stats)
    _assert_ebur128_exact(sine.artifact.measurements.loudness)

    # clipping: full-scale square region counted exactly
    clip_transcript = build_transcript(((0, 1500, "最大化のテストです。"),))
    clip = _analyze(_request(clip48, clip_transcript, 48000), tmp_path)
    clip_measure = clip.artifact.measurements
    assert clip_measure.peak_sample == 32768
    assert clip_measure.peak_mb == 0
    assert clip_measure.clipping_count == 12000


def test_cache_replay_is_hit_without_recompute(
    designed48: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = build_transcript(
        (
            (0, 5000, "今日は撮影の裏側をお見せします。"),
            (5000, 5500, ""),
            (5500, 12500, "まずカメラのセッティングからです。"),
        )
    )
    request = _request(designed48, transcript, 48000)

    calls: list[tuple[str, ...]] = []
    import services.analyze.audio_probe as probe_module  # noqa: PLC0415 (monkeypatch target)

    original = probe_module.run_bounded

    def counting(argv: tuple[str, ...], timeout_sec: int, what: str):
        calls.append(argv)
        return original(argv, timeout_sec, what)

    monkeypatch.setattr(probe_module, "run_bounded", counting)

    first = _analyze(request, tmp_path)
    assert first.cache_status == "miss"
    assert len(calls) >= 2  # ffprobe + bounded decode at minimum

    calls.clear()
    second = _analyze(request, tmp_path)
    assert second.cache_status == "hit"
    assert calls == []
    assert Path(second.artifact_path).read_bytes() == Path(first.artifact_path).read_bytes()
    assert second.artifact == first.artifact


def test_designed16_pause_exact_and_transcript_mapping(designed16: Path, tmp_path: Path) -> None:
    transcript = build_transcript(
        (
            (0, 3000, "機材の紹介をします。"),
            (3000, 3600, ""),
            (3600, 9000, "設定の説明です。"),
        )
    )
    result = _analyze(_request(designed16, transcript, 16000), tmp_path)
    assert result.artifact.measurements.frame_count == 144000
    assert len(result.artifact.measurements.silence_spans) == 1
    span = result.artifact.measurements.silence_spans[0]
    assert (span.start_sample, span.end_sample) == (48000, 57600)
    assert (span.start_ms, span.end_ms) == (3000, 3600)
    pause = _pause_candidate(result.artifact.candidates)
    assert (pause.span.start_sample, pause.span.end_sample) == (48000, 57600)
    assert result.artifact.dialogue_ambient.dialogue_rms_mb == MB_SQUARE_HALF
    assert result.artifact.dialogue_ambient.ambient_noise_floor_mb == MB_FLOOR
    mapping = map_transcript_to_samples(transcript, 16000)
    assert [(seg.span.start_sample, seg.span.end_sample) for seg in mapping.segments] == [
        (0, 48000),
        (48000, 57600),
        (57600, 144000),
    ]
    assert mapping.segments[0].is_speech is True
    assert mapping.segments[1].is_speech is False


def test_golden_manifest_recipe_pause_filler_coverage(golden48: Path, tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["fixture_id"] == "p1-ref-02-pauses-fillers"

    def floor_ms(frame: int) -> int:
        return frame * 1000 // 30

    seg_frames = [
        (seg["span"]["start_frame"], seg["span"]["end_frame"], seg["text"])
        for seg in manifest["transcript"]["segments"]
    ]
    transcript = build_transcript(
        tuple((floor_ms(start), floor_ms(end), text) for start, end, text in seg_frames)
    )
    result = _analyze(_request(golden48, transcript, 48000), tmp_path)
    artifact = result.artifact

    assert artifact.measurements.frame_count == 827 * 1600
    assert artifact.measurements.peak_sample == 16384
    assert artifact.measurements.clipping_count == 0
    assert artifact.dialogue_ambient.dialogue_rms_mb == MB_SQUARE_HALF
    assert artifact.dialogue_ambient.ambient_noise_floor_mb == MB_FLOOR

    # pauses: expected spans derived from declared frame spans + frozen grid
    hop = 2400
    window = 4800
    expected: dict[tuple[int, int], int] = {}
    for declared in manifest["analyzer_expectations"]["pauses"]:
        entry = next(
            seg
            for seg in manifest["transcript"]["segments"]
            if seg["segment_id"] == declared["segment_id"]
        )
        start = entry["span"]["start_frame"] * 1600
        end = entry["span"]["end_frame"] * 1600
        first = -(-start // hop) * hop  # ceil onto the hop grid
        last = (end - window) // hop * hop + window
        expected[(first, last)] = declared["pause_ms"]
    pauses = [candidate for candidate in artifact.candidates if candidate.kind == "pause"]
    assert len(pauses) == 3
    detected = {(pause.span.start_sample, pause.span.end_sample) for pause in pauses}
    assert detected == set(expected)
    for pause in pauses:
        declared_ms = expected[(pause.span.start_sample, pause.span.end_sample)]
        assert pause.span.end_sample - pause.span.start_sample >= declared_ms * 48 - window

    # filler coverage: f1 ("えっと") proposed over its declared span
    fillers = [candidate for candidate in artifact.candidates if candidate.kind == "filler"]
    assert len(fillers) == 1
    filler = fillers[0]
    assert _as_filler(filler).matched_text == "えっと"
    f1_frame_start = next(
        seg["span"]["start_frame"]
        for seg in manifest["transcript"]["segments"]
        if seg["segment_id"] == "f1"
    )
    assert abs(filler.span.start_sample - f1_frame_start * 1600) <= 1600
    assert filler.span.end_sample > filler.span.start_sample

    # speech texts are terminated sentences -> no false starts in this fixture
    assert [candidate for candidate in artifact.candidates if candidate.kind == "false_start"] == []


def test_false_start_rule_fires_on_designed_structure() -> None:
    transcript = build_transcript(
        (
            (0, 2000, "今日は機材の紹介を"),
            (2100, 4000, "今日は機材の紹介をします。"),
            (5000, 7000, "次は設定です。"),
            (7100, 9000, "次は設定の続きです。"),
        )
    )
    mapping = map_transcript_to_samples(transcript, 16000)
    starts = generate_false_start_candidates(mapping, ANALYZER_VERSION, ("c" * 64,))
    assert len(starts) == 1
    candidate = starts[0]
    assert candidate.kind == "false_start"
    assert (candidate.span.start_sample, candidate.span.end_sample) == (0, 32000)
    assert (candidate.span.start_ms, candidate.span.end_ms) == (0, 2000)
    assert _as_false_start(candidate).abandoned_text == "今日は機材の紹介を"
    assert _as_false_start(candidate).restart_text == "今日は機材の紹介をします。"
    assert _as_false_start(candidate).gap_ms == 100


def test_filler_lexicon_longest_match_and_spans() -> None:
    transcript = build_transcript(
        (
            (0, 1500, "えっと、これがカメラです。"),
            (2000, 3500, "画角が、みたいな、感じです。"),
            (4000, 5000, "えーっと、始めます。"),
            (5500, 7000, "普通の文です。"),
        )
    )
    mapping = map_transcript_to_samples(transcript, 16000)
    fillers = generate_filler_candidates(mapping, ANALYZER_VERSION, ("d" * 64,))
    assert [_as_filler(candidate).matched_text for candidate in fillers] == [
        "えっと",
        "みたいな",
        "えー",
    ]
    offsets = [
        (_as_filler(candidate).char_start, _as_filler(candidate).char_end)
        for candidate in fillers
    ]
    assert offsets == [(0, 3), (4, 8), (0, 2)]
    first = fillers[0]
    assert (first.span.start_sample, first.span.end_sample) == (0, 24000)
    assert all(candidate.confidence > 0 for candidate in fillers)
    assert all(candidate.proposal_only for candidate in fillers)
    assert "えっと" in FILLER_LEXICON
    assert "あの" in FILLER_LEXICON
    assert "そのー" in FILLER_LEXICON
    assert "みたいな" in FILLER_LEXICON


def test_time_base_confusion_refused(designed16: Path, tmp_path: Path) -> None:
    transcript = build_transcript(((0, 9000, "時間基準の確認です。"),))
    request = AudioAnalysisRequest(
        wav_path=str(designed16),
        wav_sha256=sha256_file(designed16),
        declared=StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le"),
        transcript=transcript,
    )
    with pytest.raises(AnalyzeTimeBaseError) as refused:
        _analyze(request, tmp_path)
    assert refused.value.label == "analyze_time_base_mismatch"
    assert not (tmp_path / "work" / "analysis-artifact.json").exists()


def test_corrupt_wav_structured_decode_failure(trunc48: Path, tmp_path: Path) -> None:
    transcript = build_transcript(((0, 6000, "壊れたファイルのテスト。"),))
    request = _request(trunc48, transcript, 48000)
    with pytest.raises(AnalyzeDecodeError) as failed:
        _analyze(request, tmp_path)
    assert failed.value.label == "corrupt_decode"
    probe = failed.value.probe
    assert probe is not None
    assert probe.decode_ok is False
    assert probe.failure is not None
    assert probe.failure.code == "corrupt_decode"


def test_all_fillers_auto_delete_impossible(designed48: Path, tmp_path: Path) -> None:
    transcript = build_transcript(
        ((0, 1000, "えっと"), (1100, 2000, "あの"), (2100, 3000, "そのー"))
    )
    result = _analyze(_request(designed48, transcript, 48000), tmp_path)
    fillers = [candidate for candidate in result.artifact.candidates if candidate.kind == "filler"]
    assert len(fillers) == 3
    assert all(candidate.proposal_only is True for candidate in fillers)
    with pytest.raises(ValidationError):
        fillers[0].kind = "deletion"  # type: ignore[misc]

    import services.analyze.candidates as candidates_module  # noqa: PLC0415 (__all__ probe)

    forbidden = {"delete_span", "trim", "apply_to_plan", "mutate_plan", "remove_speech"}
    assert forbidden.isdisjoint(candidates_module.__all__)

    # an EditPlan0C passed to any analyzer entry point is refused
    plan = object.__new__(EditPlan0C)
    with pytest.raises(AnalyzeRequestError, match="Edit Plan"):
        analyze_dialogue(plan, work_dir=tmp_path / "w2", cache_dir=tmp_path / "c2")
    with pytest.raises((AnalyzeRequestError, TypeError)):
        generate_pause_candidates(
            (plan,),  # type: ignore[arg-type]
            map_transcript_to_samples(transcript, 48000),
            ANALYZER_VERSION,
            ("e" * 64,),
        )


def test_missing_confidence_or_version_rejected() -> None:
    good = {
        "kind": "filler",
        "span": {
            "start_sample": 0,
            "end_sample": 24000,
            "sample_rate": 16000,
            "start_ms": 0,
            "end_ms": 1500,
        },
        "confidence": 800,
        "provenance": {
            "analyzer_version": ANALYZER_VERSION,
            "rule_id": "filler-lexicon-ja-v1",
            "input_artifact_hashes": ("f" * 64,),
        },
        "evidence": {
            "segment_index": 0,
            "matched_text": "えっと",
            "char_start": 0,
            "char_end": 3,
            "segment_text": "えっと、これがカメラです。",
        },
        "proposal_only": True,
    }
    Candidate.model_validate(good)
    missing_confidence = {key: value for key, value in good.items() if key != "confidence"}
    with pytest.raises(ValidationError):
        Candidate.model_validate(missing_confidence)
    missing_version = json.loads(json.dumps(good))
    del missing_version["provenance"]["analyzer_version"]
    with pytest.raises(ValidationError):
        Candidate.model_validate(missing_version)
    float_payload = json.loads(json.dumps(good))
    float_payload["span"]["start_ms"] = 0.5
    with pytest.raises(ValidationError):
        Candidate.model_validate(float_payload)


def test_dialogue_ambient_conflation_rejected() -> None:
    separated = DialogueAmbientSummary.model_validate(
        {
            "dialogue_rms_mb": MB_SQUARE_HALF,
            "ambient_noise_floor_mb": MB_FLOOR,
            "dialogue_sample_count": 100,
            "ambient_sample_count": 10,
        }
    )
    assert separated.dialogue_rms_mb != separated.ambient_noise_floor_mb
    conflated = {
        "dialogue_rms_mb": MB_SQUARE_HALF,
        "ambient_noise_floor_mb": MB_FLOOR,
        "dialogue_sample_count": 100,
        "ambient_sample_count": 10,
        "dialogue_ambient_rms_mb": -3000,
    }
    with pytest.raises(ValidationError):
        DialogueAmbientSummary.model_validate(conflated)


def test_cache_key_binds_version_inputs_and_frozen_constants() -> None:
    declared48 = StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le")
    declared16 = StreamFacts(sample_rate=16000, channels=1, codec="pcm_s16le")
    constants = frozen_constants_hash()
    base = cache_key(
        analyzer_version=ANALYZER_VERSION,
        wav_sha256="1" * 64,
        transcript_sha256="2" * 64,
        declared=declared48,
        frozen_constants_hash=constants,
    )
    assert (
        cache_key(
            analyzer_version="todo34-v2",
            wav_sha256="1" * 64,
            transcript_sha256="2" * 64,
            declared=declared48,
            frozen_constants_hash=constants,
        )
        != base
    )
    assert (
        cache_key(
            analyzer_version=ANALYZER_VERSION,
            wav_sha256="9" * 64,
            transcript_sha256="2" * 64,
            declared=declared48,
            frozen_constants_hash=constants,
        )
        != base
    )
    assert (
        cache_key(
            analyzer_version=ANALYZER_VERSION,
            wav_sha256="1" * 64,
            transcript_sha256="8" * 64,
            declared=declared48,
            frozen_constants_hash=constants,
        )
        != base
    )
    assert (
        cache_key(
            analyzer_version=ANALYZER_VERSION,
            wav_sha256="1" * 64,
            transcript_sha256="2" * 64,
            declared=declared48,
            frozen_constants_hash="0" * 64,
        )
        != base
    )
    assert (
        cache_key(
            analyzer_version=ANALYZER_VERSION,
            wav_sha256="1" * 64,
            transcript_sha256="2" * 64,
            declared=declared16,
            frozen_constants_hash=constants,
        )
        != base
    )
    assert issubclass(AnalyzeCacheConflictError, Exception)


def test_dialogue_ambient_on_silence_only_spans(designed48: Path) -> None:
    transcript = build_transcript(((0, 12500, "最初のセグメントです。"),))
    mapping = map_transcript_to_samples(transcript, 48000)

    pcm = read_s16_mono_wav(designed48)
    summary = compute_dialogue_ambient(pcm, mapping, ((240000, 264000),))
    assert summary.dialogue_sample_count > 0
    assert summary.ambient_sample_count == 24000


def test_analyzer_cache_conflict_refuses_overwrite(tmp_path: Path) -> None:
    binding = AnalyzerCacheBinding(
        analyzer_version=ANALYZER_VERSION,
        wav_sha256="1" * 64,
        transcript_sha256="2" * 64,
        declared=StreamFacts(sample_rate=48000, channels=1, codec="pcm_s16le"),
        frozen_constants_hash=frozen_constants_hash(),
        wav_path="/var/media/episode.wav",
    )
    key = cache_key(
        analyzer_version=binding.analyzer_version,
        wav_sha256=binding.wav_sha256,
        transcript_sha256=binding.transcript_sha256,
        declared=binding.declared,
        frozen_constants_hash=binding.frozen_constants_hash,
    )
    cache = AnalyzerCache(tmp_path)
    cache.store(key, binding, b"artifact-bytes")
    assert cache.lookup(key, binding) == b"artifact-bytes"

    foreign = binding.model_copy(update={"wav_path": "/var/media/other.wav"})
    with pytest.raises(AnalyzeCacheConflictError):
        cache.lookup(key, foreign)
    with pytest.raises(AnalyzeCacheConflictError):
        cache.store(key, foreign, b"other")

    (tmp_path / key / "binding.json").unlink()
    with pytest.raises(AnalyzeCacheConflictError):
        cache.lookup(key, binding)


def test_window_constants_declared() -> None:
    assert WINDOW_MS == 100
    assert MIN_SILENCE_MS == 300
