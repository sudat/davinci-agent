from __future__ import annotations

import subprocess
import wave
from array import array
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.fixtures.materialize_validation import MaterializationError

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest

COMMAND_TIMEOUT_SECONDS: Final = 90
SHA256_HEX_LENGTH: Final = 64
PROBE_ARGUMENTS: Final = (
    "-v",
    "error",
    "-show_entries",
    (
        "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,"
        "avg_frame_rate,nb_frames,sample_rate,channels:format=duration,format_name"
    ),
    "-of",
    "json",
)


def _run(argv: tuple[str, ...], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise MaterializationError("media command exceeded 90 second limit") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "media command failed"
        raise MaterializationError(detail)
    return result.stdout if capture else ""


def _write_wave(path: Path, samples: array[int], sample_rate: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(samples.tobytes())


def write_audio(stage: Path, manifest: Phase0AFixtureManifest) -> None:
    recipe = manifest.recipe.audio
    samples = array("h", [0]) * recipe.duration_samples
    for position in recipe.pulse_sample_positions:
        samples[position : position + recipe.pulse_width_samples] = array(
            "h", [recipe.amplitude]
        ) * recipe.pulse_width_samples
    _write_wave(stage / "pulse.wav", samples, recipe.sample_rate)
    _write_wave(
        stage / "slate-silence.wav",
        array("h", [0]) * recipe.sample_rate,
        recipe.sample_rate,
    )


def encode_source(ffmpeg: Path, stage: Path, manifest: Phase0AFixtureManifest) -> None:
    source = manifest.recipe.source
    _run(
        (
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
            f"testsrc2=size={source.width}x{source.height}:rate=30:duration=20",
            "-i", str(stage / "pulse.wav"), "-map", "0:v:0", "-map", "1:a:0",
            "-frames:v", str(source.duration_frames), "-vf",
            "fps=30,setpts=N/(30*TB)", "-r", "30", "-c:v", "h264_videotoolbox",
            "-pix_fmt", source.pixel_format, "-c:a", "pcm_s16le", "-ar", "48000",
            "-ac", "1", "-map_metadata", "-1", "-metadata",
            "creation_time=1970-01-01T00:00:00Z", "-y", str(stage / "source.mov"),
        )
    )


def encode_slate(
    ffmpeg: Path,
    stage: Path,
    manifest: Phase0AFixtureManifest,
    name: str,
    color: str,
) -> None:
    source = manifest.recipe.source
    _run(
        (
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
            f"color=c={color}:s={source.width}x{source.height}:r=30:d=1",
            "-i", str(stage / "slate-silence.wav"), "-map", "0:v:0", "-map", "1:a:0",
            "-frames:v", "30", "-vf", "fps=30,setpts=N/(30*TB)", "-r", "30",
            "-c:v", "h264_videotoolbox", "-pix_fmt", source.pixel_format,
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1", "-map_metadata", "-1",
            "-metadata", "creation_time=1970-01-01T00:00:00Z", "-y",
            str(stage / f"{name}.mov"),
        )
    )


def probe_media(ffprobe: Path, media: Path) -> str:
    return _run((str(ffprobe), *PROBE_ARGUMENTS, str(media)), capture=True)


def decoded_video_sha256(ffmpeg: Path, media: Path) -> str:
    output = _run(
        (
            str(ffmpeg), "-v", "error", "-i", str(media), "-map", "0:v:0",
            "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "hash", "-hash",
            "sha256", "-",
        ),
        capture=True,
    ).strip()
    prefix = "SHA256="
    if not output.startswith(prefix):
        raise MaterializationError("decoded video hash output is malformed")
    digest = output.removeprefix(prefix)
    invalid_character = any(
        character not in "0123456789abcdef" for character in digest
    )
    if len(digest) != SHA256_HEX_LENGTH or invalid_character:
        raise MaterializationError("decoded video SHA-256 is malformed")
    return digest
