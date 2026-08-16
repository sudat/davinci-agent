"""Frozen-recipe materialization of the six Phase-0B ingest inputs.

Executes the generation recipes exactly as frozen in
``tests/fixtures/manifests/phase-0b/*.json`` with the pinned FFmpeg binary:
the pulse WAV generator (``python-wave-pcm-pulse-v1``), the generation argv
(``{ffmpeg}``/``{audio_input}``/``{output}`` placeholders), and the MOV tkhd
rotation post-step (``python-mov-tkhd-rotate90-v1``). Determinism is bound to
the manifest and argv hashes recorded next to the media; h264_videotoolbox
bytes themselves are not asserted stable (Phase-0A precedent).
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import wave
from array import array
from pathlib import Path
from typing import Final

from services.fixtures.manifest_phase0b import GenerationAudio, Phase0BFixtureManifest
from services.foundation_io import atomic_write, sha256_file

PHASE_0B_FIXTURE_IDS: Final = (
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
)
GENERATION_TIMEOUT_SECONDS: Final = 300
ROTATE_90_DEGREES: Final = 90


class FixtureMaterializationError(ValueError):
    """The frozen generation recipe could not be executed."""


def _load_manifest(manifest_path: Path) -> Phase0BFixtureManifest:
    raw = manifest_path.read_bytes()
    manifest = Phase0BFixtureManifest.model_validate_json(raw)
    if raw != manifest.canonical_bytes():
        raise FixtureMaterializationError(
            f"phase-0b fixture manifest is noncanonical: {manifest_path}"
        )
    return manifest


def _write_pulse_wav(path: Path, audio: GenerationAudio) -> None:
    samples = array("h", [0]) * audio.duration_samples
    for position in audio.pulse_sample_positions:
        samples[position : position + audio.pulse_width_samples] = (
            array("h", [audio.amplitude]) * audio.pulse_width_samples
        )
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(audio.channels)
        stream.setsampwidth(2)
        stream.setframerate(audio.sample_rate)
        stream.writeframes(samples.tobytes())


def _run(argv: tuple[str, ...]) -> None:
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True,
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise FixtureMaterializationError("generation command timed out") from error
    if result.returncode != 0:
        raise FixtureMaterializationError(result.stderr.strip() or "generation failed")


def _boxes(
    data: bytearray, start: int, end: int
) -> list[tuple[int, bytes, int]]:
    found: list[tuple[int, bytes, int]] = []
    offset = start
    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = bytes(data[offset + 4 : offset + 8])
        if size == 0:
            size = end - offset
        found.append((offset, kind, size))
        offset += size
    return found


def _rotation_matrix(degrees: int) -> tuple[int, ...]:
    if degrees == ROTATE_90_DEGREES:
        return (0, 1 << 16, 0, -(1 << 16), 0, 0, 0, 0, 1 << 16)
    raise FixtureMaterializationError(f"unsupported rotation {degrees}")


def _tkhd_matrix_offset(data: bytearray) -> int | None:
    for offset, kind, size in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for trak_offset, trak_kind, trak_size in _boxes(data, offset + 8, offset + size):
            if trak_kind != b"trak":
                continue
            for tkhd_offset, tkhd_kind, tkhd_size in _boxes(
                data, trak_offset + 8, trak_offset + trak_size
            ):
                if tkhd_kind != b"tkhd":
                    continue
                payload = tkhd_offset + 8
                header = 52 if data[payload] == 1 else 40
                if tkhd_size >= header + 36:
                    return payload + header
    return None


def _patch_tkhd_rotation(media: Path, degrees: int) -> None:
    data = bytearray(media.read_bytes())
    matrix_offset = _tkhd_matrix_offset(data)
    if matrix_offset is None:
        raise FixtureMaterializationError("no tkhd box found for rotation patch")
    matrix = _rotation_matrix(degrees)
    data[matrix_offset : matrix_offset + 36] = struct.pack(">9i", *matrix)
    media.write_bytes(bytes(data))


def materialize_fixture(manifest_path: Path, ffmpeg: Path, destination_dir: Path) -> Path:
    manifest = _load_manifest(manifest_path)
    destination_dir.mkdir(parents=True, exist_ok=True)
    media = destination_dir / f"{manifest.fixture_id}.mov"
    report_path = destination_dir / f"{manifest.fixture_id}.generation-report.json"
    argv_hash = hashlib.sha256(
        json.dumps(
            list(manifest.generation.video.argv), ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    manifest_hash = sha256_file(manifest_path)
    if media.is_file() and _report_matches(report_path, manifest_hash, argv_hash):
        return media
    audio_input = destination_dir / f"{manifest.fixture_id}.pulse.wav"
    _write_pulse_wav(audio_input, manifest.generation.audio)
    placeholders = {
        "{ffmpeg}": str(ffmpeg),
        "{audio_input}": str(audio_input),
        "{output}": str(media),
    }
    _run(tuple(placeholders.get(token, token) for token in manifest.generation.video.argv))
    for step in manifest.generation.post:
        if step.generator == "python-mov-tkhd-rotate90-v1":
            _patch_tkhd_rotation(media, step.rotation_degrees)
        else:
            raise FixtureMaterializationError(f"unknown post step: {step.generator}")
    atomic_write(
        report_path,
        json.dumps(
            {
                "fixture_id": manifest.fixture_id,
                "manifest_sha256": manifest_hash,
                "generation_argv_sha256": argv_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    return media


def _report_matches(report_path: Path, manifest_hash: str, argv_hash: str) -> bool:
    try:
        payload = json.loads(report_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("manifest_sha256") == manifest_hash
        and payload.get("generation_argv_sha256") == argv_hash
    )
