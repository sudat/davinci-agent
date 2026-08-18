"""Deterministic fixture edit-source synthesis per the frozen media recipes.

Video: the manifest's lavfi filter, encoded CFR30 with the pinned ffmpeg.
Audio: per-segment Japanese TTS (pinned ``say`` voice), padded with silence to
each declared frame span, concatenated to one 48 kHz mono track, and muxed
with the video into one edit-source mezzanine. Every subprocess is bounded; a
missing tool is a typed failure. Media bytes are Rebuildable artifacts —
plan/IR hashes never derive from them.
"""

from __future__ import annotations

import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.orchestrator_models import (
    EditSourceFile,
    EditSourceRef,
    edit_source_world_sha,
)
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.toolchain.models import Phase1TechnicalToolchainLock

VIDEO_ROLE: Final = "edit-source-video"
AUDIO_ROLE: Final = "edit-source-audio"
MEZZANINE_NAME: Final = "edit-source.mov"
TTS_TIMEOUT_SECONDS: Final = 60
FFMPEG_TIMEOUT_SECONDS: Final = 180
SAMPLE_RATE: Final = 48000
FRAMES_PER_SECOND: Final = 30
SAMPLES_PER_FRAME: Final = SAMPLE_RATE // FRAMES_PER_SECOND


class MediaSynthesisError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _run_bounded(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(part) for part in argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise MediaSynthesisError(
            "synthesis_timeout", f"bounded media command exceeded {timeout}s: {argv[0]}"
        ) from error


@dataclass(frozen=True, slots=True)
class SynthesizedMedia:
    mezzanine: Path
    edit_source: EditSourceRef
    world_sha256: str


def _samples_for_frames(frames: int) -> int:
    return frames * SAMPLES_PER_FRAME


def _speech_samples(aiff: Path, ffmpeg: Path) -> array[int]:
    wav = aiff.with_suffix(".wav")
    result = _run_bounded(
        (
            str(ffmpeg),
            "-v",
            "error",
            "-y",
            "-i",
            str(aiff),
            "-vn",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ),
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise MediaSynthesisError(
            "tts_convert_failed", result.stderr.strip()[-800:] or "ffmpeg failed"
        )
    with wave.open(str(wav), "rb") as stream:
        raw = stream.readframes(stream.getnframes())
    wav.unlink(missing_ok=True)
    return array("h", raw)


def _segment_audio(
    manifest: Phase1TechnicalFixtureManifest, ffmpeg: Path, say: Path, voice: str, out_dir: Path
) -> array[int]:
    total = _samples_for_frames(manifest.edit_source.total_frames)
    buffer = array("h", [0]) * total
    for position, segment in enumerate(manifest.transcript.segments):
        if not segment.text:
            continue
        aiff = out_dir / f"tts-{position:02d}.aiff"
        spoken = _run_bounded(
            (str(say), "-v", voice, "-o", str(aiff), segment.text),
            timeout=TTS_TIMEOUT_SECONDS,
        )
        if spoken.returncode != 0 or not aiff.is_file():
            raise MediaSynthesisError(
                "tts_failed",
                spoken.stderr.strip()[-800:] or f"say produced no audio for {segment.segment_id}",
            )
        samples = _speech_samples(aiff, ffmpeg)
        aiff.unlink(missing_ok=True)
        start = _samples_for_frames(segment.span.start_frame)
        room = _samples_for_frames(segment.span.end_frame - segment.span.start_frame)
        buffer[start : start + min(room, len(samples))] = samples[:room]
    return buffer


def _write_wave(path: Path, samples: array[int]) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(samples.tobytes())


def synthesize_edit_source(
    manifest: Phase1TechnicalFixtureManifest,
    lock: Phase1TechnicalToolchainLock,
    out_dir: Path,
) -> SynthesizedMedia:
    """Materialize the declared edit-source mezzanine and its world reference."""

    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    ffprobe = Path(lock.ffmpeg.ffprobe.path)
    say = Path(lock.whisper_ja.tts.tool_path)
    missing = [
        f"{tool}={path}"
        for tool, path in (("ffmpeg", ffmpeg), ("say", say))
        if not path.is_file()
    ]
    if missing:
        raise MediaSynthesisError("tool_missing", f"pinned media tools unavailable: {missing}")

    wav = out_dir / "edit-source.wav"
    mezzanine = out_dir / MEZZANINE_NAME
    _write_wave(
        wav, _segment_audio(manifest, ffmpeg, say, lock.whisper_ja.tts.voice, out_dir)
    )
    recipe = manifest.media_recipe.video
    encode = _run_bounded(
        (
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            recipe.filter,
            "-i",
            str(wav),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-frames:v",
            str(recipe.duration_frames),
            "-vf",
            "fps=30,setpts=N/(30*TB)",
            "-r",
            "30",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-map_metadata",
            "-1",
            "-y",
            str(mezzanine),
        ),
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    wav.unlink(missing_ok=True)
    if encode.returncode != 0 or not mezzanine.is_file():
        raise MediaSynthesisError(
            "encode_failed", encode.stderr.strip()[-800:] or "mezzanine missing after encode"
        )
    probe = _run_bounded(
        (
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,sample_rate,r_frame_rate",
            "-of",
            "json",
            str(mezzanine),
        ),
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if probe.returncode != 0:
        raise MediaSynthesisError("probe_failed", probe.stderr.strip()[-800:])
    report = probe.stdout
    if "48000" not in report or "30/1" not in report:
        raise MediaSynthesisError(
            "mezzanine_shape_wrong", f"mezzanine is not CFR30/48kHz: {report[:400]}"
        )

    mezzanine_sha = sha256_file(mezzanine)
    edit_source = EditSourceRef(
        source_id=manifest.edit_source.source_id,
        files=(
            EditSourceFile(role=VIDEO_ROLE, path=str(mezzanine.resolve()), sha256=mezzanine_sha),
            EditSourceFile(role=AUDIO_ROLE, path=str(mezzanine.resolve()), sha256=mezzanine_sha),
        ),
    )
    return SynthesizedMedia(
        mezzanine=mezzanine,
        edit_source=edit_source,
        world_sha256=edit_source_world_sha(edit_source),
    )


__all__ = [
    "AUDIO_ROLE",
    "MEZZANINE_NAME",
    "VIDEO_ROLE",
    "MediaSynthesisError",
    "SynthesizedMedia",
    "synthesize_edit_source",
]
