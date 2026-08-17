"""Real-binary tests for the pinned local Japanese ASR adapter (whisper.cpp).

Recorded comparison rule for the happy path: the transcript text is asserted
to contain the declared fed text after trivial normalization only — the
``_normalize_japanese`` rule the frozen whisper-ja smoke already uses (strip
whitespace and Japanese/ASCII punctuation 、。.,!?「」『』 and full-width
spaces). No other fuzzy matching is allowed.

Runtime is bounded by contract: exactly ONE real whisper-cli transcription
(the happy case); every failure case refuses before any heavy execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.analyze.asr_cache import (
    AsrCache,
    AsrCacheConflictError,
    cache_key,
)
from services.analyze.asr_cache import (
    CacheBinding as Binding,
)
from services.analyze.asr_models import (
    FORBIDDEN_ACCELERATOR_FLAGS,
    FROZEN_PREPROCESSING_ARGV,
    FROZEN_WHISPER_ARGV_TEMPLATE,
    AsrError,
    AsrInputBinding,
    AsrParseError,
    AsrRequest,
    AsrSettingsEcho,
    AsrToolDriftError,
    PinnedTool,
    TranscriptArtifact,
    WavProbeSummary,
    transcript_content_hash,
)
from services.analyze.asr_parse import build_transcript_artifact, parse_whisper_payload
from services.analyze.asr_whisper_cpp import transcribe
from services.foundation_io import sha256_file
from services.toolchain.models import (
    Phase1TechnicalToolchainLock,
    load_lock,
    write_lock,
)
from services.toolchain.whisper_ja import _normalize_japanese
from tests.analyze.conftest import PHASE1_LOCK, PinnedAsrStack

PHASE1_LOCK_PATH = PHASE1_LOCK


def _fake_lock_with(
    tmp_path: Path, *, cli_path: Path | None = None, model_path: Path | None = None
) -> Path:
    lock = load_lock(PHASE1_LOCK)
    assert isinstance(lock, Phase1TechnicalToolchainLock)
    section = lock.whisper_ja
    updates: dict[str, object] = {}
    if cli_path is not None:
        updates["whisper_cli"] = section.whisper_cli.model_copy(update={"path": str(cli_path)})
    if model_path is not None:
        updates["model"] = section.model.model_copy(update={"path": str(model_path)})
    fake = lock.model_copy(update={"whisper_ja": section.model_copy(update=updates)})
    path = tmp_path / "fake-lock.json"
    write_lock(path, fake)
    return path


def _unused_work_dir_is_clean(work_dir: Path) -> bool:
    return not work_dir.exists() or not any(work_dir.iterdir())


def test_pinned_local_asr_transcribes_frozen_japanese_fixture(
    asr_request: AsrRequest, pinned_asr: PinnedAsrStack, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    cache_dir = tmp_path / "cache"
    first = transcribe(asr_request, work_dir=work, cache_dir=cache_dir)
    assert first.cache_status == "miss"

    artifact = first.artifact
    assert isinstance(artifact, TranscriptArtifact)
    assert len(artifact.segments) >= 1
    # comparison rule: trivial whitespace/punctuation normalization only
    joined = _normalize_japanese("".join(segment.text for segment in artifact.segments))
    assert _normalize_japanese(pinned_asr.key_phrase) in joined

    wav = work / "asr-16k.wav"
    assert artifact.input_binding.media_sha256 == asr_request.input_media_sha256
    assert artifact.input_binding.media_path == asr_request.input_media_path
    assert artifact.input_binding.wav_sha256 == sha256_file(wav)
    assert artifact.input_binding.wav_probe == WavProbeSummary()

    # misleading-success guard: recomputed content hash binds the content bytes
    assert artifact.content_hash == transcript_content_hash(
        artifact.input_binding,
        artifact.segments,
        artifact.experimental,
        artifact.settings_echo,
    )

    # settings echo: absolute pinned binaries only, no scheme-bearing token
    echo = artifact.settings_echo
    assert Path(echo.ffmpeg_argv[0]) == pinned_asr.ffmpeg
    assert Path(echo.whisper_argv[0]) == pinned_asr.cli
    for argv in (echo.ffmpeg_argv, echo.whisper_argv):
        assert Path(argv[0]).is_absolute()
        assert all("://" not in token for token in argv)
        assert all(token not in {"ffmpeg", "ffprobe", "whisper-cli"} for token in argv)
    assert echo.ffmpeg_argv[1:] == (
        "-nostdin",
        "-i",
        asr_request.input_media_path,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(work / "asr-16k.wav"),
    )
    assert "--language" in echo.whisper_argv
    assert "ja" in echo.whisper_argv

    # token timing is labeled experimental on the real run
    assert artifact.experimental.label == "experimental-token-timing"
    assert artifact.experimental.consistent_with_segments is True

    # REPLAY: identical call must be a cache hit without re-running whisper-cli
    whisper_out = work / "whisper-out.json"
    mtime_before = whisper_out.stat().st_mtime_ns
    first_bytes = Path(first.artifact_path).read_bytes()
    second = transcribe(asr_request, work_dir=work, cache_dir=cache_dir)
    assert second.cache_status == "hit"
    assert whisper_out.stat().st_mtime_ns == mtime_before
    assert Path(second.artifact_path).read_bytes() == first_bytes
    assert second.artifact == first.artifact


def test_frozen_flag_set_has_no_accelerator_force_flag(
    phase1_lock: Phase1TechnicalToolchainLock,
) -> None:
    """CPU fallback is whisper.cpp-internal: the pinned build is GGML_METAL=OFF
    (CPU-only by lock) and the frozen argv passes no accelerator-force flag."""

    assert not FORBIDDEN_ACCELERATOR_FLAGS.intersection(FROZEN_WHISPER_ARGV_TEMPLATE)
    for required in (
        "--language",
        "ja",
        "--temperature",
        "--threads",
        "--no-prints",
        "--output-json-full",
    ):
        assert required in FROZEN_WHISPER_ARGV_TEMPLATE
    assert "--translate" not in FROZEN_WHISPER_ARGV_TEMPLATE
    configure = phase1_lock.whisper_ja.build.cmake_configure_argv
    assert "-DGGML_METAL=OFF" in configure
    assert phase1_lock.whisper_ja.smoke.preprocessing_argv == FROZEN_PREPROCESSING_ARGV


def test_binary_drift_refused_before_execution(
    asr_request: AsrRequest, pinned_asr: PinnedAsrStack, tmp_path: Path
) -> None:
    tampered = tmp_path / "whisper-cli-drifted"
    tampered.write_bytes(pinned_asr.cli.read_bytes() + b"\x00")
    fake_lock = _fake_lock_with(tmp_path, cli_path=tampered)
    drifted = asr_request.model_copy(
        update={"cli": PinnedTool(path=str(tampered), sha256=pinned_asr.cli_sha256)}
    )
    with pytest.raises(AsrToolDriftError):
        transcribe(drifted, work_dir=tmp_path / "work", cache_dir=tmp_path / "cache",
                   lock_path=fake_lock)
    assert _unused_work_dir_is_clean(tmp_path / "work")


def test_model_drift_refused_before_execution(
    asr_request: AsrRequest, pinned_asr: PinnedAsrStack, tmp_path: Path
) -> None:
    tampered = tmp_path / "ggml-drifted.bin"
    tampered.write_bytes(b"not a whisper model\n")
    fake_lock = _fake_lock_with(tmp_path, model_path=tampered)
    drifted = asr_request.model_copy(
        update={"model": PinnedTool(path=str(tampered), sha256=pinned_asr.model_sha256)}
    )
    with pytest.raises(AsrToolDriftError):
        transcribe(drifted, work_dir=tmp_path / "work", cache_dir=tmp_path / "cache",
                   lock_path=fake_lock)
    assert _unused_work_dir_is_clean(tmp_path / "work")


def test_cloud_network_input_refused_pre_execution(pinned_asr: PinnedAsrStack) -> None:
    for remote in ("https://media.example.com/clip.wav", "http://10.0.0.1/aiff"):
        with pytest.raises(ValidationError, match="absolute local file path"):
            AsrRequest(
                input_media_path=remote,
                input_media_sha256="0" * 64,
                model=PinnedTool(path=str(pinned_asr.model), sha256=pinned_asr.model_sha256),
                cli=PinnedTool(path=str(pinned_asr.cli), sha256=pinned_asr.cli_sha256),
                ffmpeg=PinnedTool(path=str(pinned_asr.ffmpeg), sha256=pinned_asr.ffmpeg_sha256),
            )


def test_corrupt_cli_json_is_structured_parse_error() -> None:
    with pytest.raises(AsrParseError) as broken:
        parse_whisper_payload(b'{"transcription": [{"te')
    assert broken.value.label == "asr_cli_output_malformed"
    with pytest.raises(AsrParseError, match=r"transcription\[0\].text"):
        parse_whisper_payload(b'{"transcription": [{"offsets": {"from": 0, "to": 10}}]}')
    with pytest.raises(AsrParseError, match="non-empty transcription"):
        parse_whisper_payload(b"{}")


def _artifact_from_payload(payload: bytes) -> TranscriptArtifact:
    segments, token_groups = parse_whisper_payload(payload)
    return build_transcript_artifact(
        binding=AsrInputBinding(
            media_path="/var/media/fed.aiff",
            media_sha256="1" * 64,
            wav_sha256="2" * 64,
            wav_probe=WavProbeSummary(),
        ),
        segments=segments,
        token_groups=token_groups,
        settings_echo=AsrSettingsEcho(
            ffmpeg_argv=("/usr/local/bin/ffmpeg", "-nostdin"),
            whisper_argv=("/usr/local/bin/whisper-cli", "--model", "/models/ggml.bin"),
        ),
    )


def test_experimental_timing_outside_tolerance_is_flagged() -> None:
    payload = (
        b'{"transcription": [{"timestamps": {"from": "0:00.000", "to": "0:03.000"},'
        b'"offsets": {"from": 0, "to": 3000},'
        b'"text": "\xe3\x81\x93\xe3\x82\x93\xe3\x81\xab\xe3\x81\xa1\xe3\x81\xaf",'
        b'"tokens": [{"text": "\xe3\x81\x93", "offsets": {"from": 900000, "to": 950000},'
        b'"id": 1, "p": 0.5, "t_dtw": 0.0}]}]}'
    )
    artifact = _artifact_from_payload(payload)
    assert artifact.experimental.consistent_with_segments is False
    assert artifact.experimental.inconsistency is not None
    assert "deviates from segment bounds" in artifact.experimental.inconsistency
    # canonical segments stay integer-millisecond and unflagged
    assert artifact.segments[0].start_ms == 0
    assert artifact.segments[0].end_ms == 3000
    assert artifact.experimental.label == "experimental-token-timing"


def test_bare_path_tool_refused(pinned_asr: PinnedAsrStack, japanese_aiff: Path) -> None:
    base = {
        "input_media_path": str(japanese_aiff),
        "input_media_sha256": "0" * 64,
        "model": {"path": str(pinned_asr.model), "sha256": pinned_asr.model_sha256},
        "cli": {"path": str(pinned_asr.cli), "sha256": pinned_asr.cli_sha256},
    }
    with pytest.raises(ValidationError, match="absolute"):
        AsrRequest.model_validate(base | {"ffmpeg": {"path": "ffmpeg", "sha256": "0" * 64}})
    with pytest.raises(ValidationError, match="absolute"):
        AsrRequest.model_validate(base | {"cli": {"path": "whisper-cli", "sha256": "0" * 64}})


def test_foreign_or_stale_cache_entry_is_overwrite_refused(tmp_path: Path) -> None:
    binding = Binding(
        model_sha256="a" * 64,
        cli_sha256="b" * 64,
        wav_sha256="c" * 64,
        whisper_argv_template=FROZEN_WHISPER_ARGV_TEMPLATE,
        media_sha256="d" * 64,
        media_path="/var/media/fed.aiff",
    )
    key = cache_key(
        binding.model_sha256,
        binding.cli_sha256,
        binding.wav_sha256,
        FROZEN_WHISPER_ARGV_TEMPLATE,
    )
    cache = AsrCache(tmp_path)
    cache.store(key, binding, b"artifact", b"raw")
    assert cache.lookup(key, binding) is not None

    foreign = binding.model_copy(update={"media_path": "/var/media/other.aiff"})
    with pytest.raises(AsrCacheConflictError):
        cache.lookup(key, foreign)
    with pytest.raises(AsrCacheConflictError):
        cache.store(key, foreign, b"artifact2", b"raw2")

    entry = tmp_path / key
    (entry / "binding.json").unlink()
    with pytest.raises(AsrCacheConflictError):
        cache.lookup(key, binding)
    assert issubclass(AsrCacheConflictError, AsrError)
