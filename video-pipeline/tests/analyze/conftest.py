"""Session fixtures: the pinned ASR stack and a TTS-generated Japanese clip."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.analyze.asr_models import AsrRequest, PinnedTool
from services.foundation_io import sha256_file
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

PHASE1_LOCK = Path("config/toolchains/phase-1-technical-v2.json")


@dataclass(frozen=True, slots=True)
class PinnedAsrStack:
    ffmpeg: Path
    ffprobe: Path
    cli: Path
    model: Path
    ffmpeg_sha256: str
    cli_sha256: str
    model_sha256: str
    fed_text: str
    key_phrase: str


@pytest.fixture(scope="session")
def phase1_lock() -> Phase1TechnicalToolchainLock:
    lock = load_lock(PHASE1_LOCK)
    assert isinstance(lock, Phase1TechnicalToolchainLock)
    return lock


@pytest.fixture(scope="session")
def pinned_asr(phase1_lock: Phase1TechnicalToolchainLock) -> PinnedAsrStack:
    section = phase1_lock.whisper_ja
    stack = PinnedAsrStack(
        ffmpeg=Path(phase1_lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(phase1_lock.ffmpeg.ffprobe.path),
        cli=Path(section.whisper_cli.path),
        model=Path(section.model.path),
        ffmpeg_sha256=phase1_lock.ffmpeg.ffmpeg.sha256,
        cli_sha256=section.whisper_cli.sha256,
        model_sha256=section.model.sha256,
        fed_text=section.smoke.fed_text,
        key_phrase=section.smoke.key_phrase,
    )
    missing = [
        f"{name}={path}"
        for name, path in (
            ("ffmpeg", stack.ffmpeg),
            ("ffprobe", stack.ffprobe),
            ("cli", stack.cli),
            ("model", stack.model),
        )
        if not path.is_file()
    ]
    if missing:
        pytest.skip(f"pinned ASR stack not bootstrapped: {', '.join(missing)}")
    return stack


@pytest.fixture(scope="session")
def japanese_aiff(
    phase1_lock: Phase1TechnicalToolchainLock, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """One short Japanese clip per the frozen recipe: say(Kyoko) -> aiff."""

    tts = phase1_lock.whisper_ja.tts
    tool = Path(tts.tool_path)
    if not tool.is_file():
        pytest.skip(f"TTS tool unavailable: {tool}")
    aiff = tmp_path_factory.mktemp("asr-tts") / "fed-ja.aiff"
    subprocess.run(
        [
            str(tool),
            "-v",
            tts.voice,
            "-o",
            str(aiff),
            phase1_lock.whisper_ja.smoke.fed_text,
        ],
        check=True,
        timeout=120,
        capture_output=True,
        text=True,
    )
    return aiff


@pytest.fixture
def asr_request(pinned_asr: PinnedAsrStack, japanese_aiff: Path) -> AsrRequest:
    return AsrRequest(
        input_media_path=str(japanese_aiff),
        input_media_sha256=sha256_file(japanese_aiff),
        model=PinnedTool(path=str(pinned_asr.model), sha256=pinned_asr.model_sha256),
        cli=PinnedTool(path=str(pinned_asr.cli), sha256=pinned_asr.cli_sha256),
        ffmpeg=PinnedTool(path=str(pinned_asr.ffmpeg), sha256=pinned_asr.ffmpeg_sha256),
    )
