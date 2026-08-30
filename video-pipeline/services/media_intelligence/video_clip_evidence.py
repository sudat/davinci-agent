"""T4 short-clip real evidence: the model + local verification gate.

``VideoClipEvidence`` is one hashed local short clip over an exact
half-open Edit Source frame interval: an ABSOLUTE ``file://`` ref (so a
``synthetic://`` placeholder or a remote URL cannot be represented),
SHA-256, requested == analyzed ranges, an audio-present flag, and the
exact duration. ``verify_local_clip`` proves the bytes exist, match the
declared hash, AND inspects the ACTUAL streams with the pinned ffprobe:
the declared ``audio_present`` must match reality (a forged or stale flag
is a typed rejection), and with ``audio_forbidden`` (the GLM specialist
gate) any actually-audio-bearing file is rejected regardless of the
declaration — all BEFORE any provider transport.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self
from urllib.parse import unquote

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.analyze.audio_probe import DEFAULT_LOCK_PATH, resolve_audio_tools, run_bounded
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import sha256_file
from services.media_intelligence.moment_review import ReviewWindow  # noqa: TC001 (pydantic field)

_PROBE_TIMEOUT_SEC: Final = 120


class VideoClipError(Exception):
    """Typed short-clip evidence failure — never a silent fallback."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class VideoClipEvidence(StrictModel):
    """One hashed local short clip over an exact half-open frame interval.

    Requested and analyzed ranges are exact Edit Source frames and must
    agree: a provider observing a different span is a mismatch, never
    authority.
    """

    ref: str = Field(min_length=1, strict=True)
    sha256: Sha256
    requested_range: ReviewWindow
    analyzed_range: ReviewWindow
    audio_present: bool
    duration_seconds: float = Field(ge=0.0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def clip_fields_are_consistent(self) -> Self:
        path = unquote(self.ref.removeprefix("file://"))
        if not self.ref.startswith("file://") or not Path(path).is_absolute():
            raise PydanticCustomError(
                "not_a_local_clip_ref",
                "clip ref must be an ABSOLUTE file:// path of a real local clip: {ref}",
                {"ref": self.ref},
            )
        start, end = int(self.requested_range.start_frame), int(self.requested_range.end_frame)
        if end <= start:
            raise PydanticCustomError(
                "clip_range_empty",
                "clip ranges must be a forward half-open [start, end) interval",
            )
        if (int(self.analyzed_range.start_frame), int(self.analyzed_range.end_frame)) != (
            start,
            end,
        ):
            raise PydanticCustomError(
                "clip_range_mismatch",
                "analyzed range must equal the requested range (provider spans are "
                "observations, never authority)",
            )
        return self


def clip_local_path(clip: VideoClipEvidence) -> Path:
    """The clip's absolute local path (the model guarantees a file:// ref)."""

    return Path(unquote(clip.ref.removeprefix("file://")))


@dataclass(frozen=True, slots=True)
class VerifiedLocalClip:
    """Locally proven clip facts: the hashed path + ACTUAL probed streams.

    ``audio_streams`` comes from the pinned ffprobe over the verified
    bytes, never from the (forgeable) declared flag.
    """

    path: Path
    audio_streams: int

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0


def probe_streams(ffprobe: Path, media: Path) -> list[dict[str, object]]:
    """Stream inventory via the pinned ffprobe (shared with extraction)."""

    result = run_bounded(
        (str(ffprobe), "-v", "error", "-print_format", "json", "-show_streams", str(media)),
        _PROBE_TIMEOUT_SEC,
        "clip stream probe",
    )
    if result.returncode != 0:
        raise VideoClipError(
            "clip-probe-failed",
            f"ffprobe failed on {media}: {result.stderr.strip()[-300:]}",
        )
    try:
        streams: list[dict[str, object]] = json.loads(result.stdout)["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise VideoClipError("clip-probe-failed", f"ffprobe payload malformed: {error}") from None
    return streams


def verify_local_clip(
    clip: VideoClipEvidence,
    *,
    audio_forbidden: bool = False,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> VerifiedLocalClip:
    """Prove the clip is REAL local evidence before any provider transport.

    Accepts only an existing, non-empty regular file whose bytes hash to
    the declared SHA-256, then inspects the ACTUAL streams with the pinned
    ffprobe: a declared ``audio_present`` that contradicts reality is a
    typed rejection, and ``audio_forbidden`` demands ZERO actual audio
    streams regardless of the declaration.
    """

    path = clip_local_path(clip)
    if not path.exists():
        raise VideoClipError("clip-missing", f"clip evidence file not found: {path}")
    if not path.is_file():
        raise VideoClipError("clip-not-a-file", f"clip evidence path is not a file: {path}")
    if path.stat().st_size == 0:
        raise VideoClipError("clip-empty", f"clip evidence file is empty: {path}")
    actual_hash = sha256_file(path)
    if actual_hash != clip.sha256:
        raise VideoClipError(
            "clip-hash-mismatch",
            f"clip bytes hash to {actual_hash[:12]}… but the evidence declares "
            f"{clip.sha256[:12]}…: {path}",
        )
    streams = probe_streams(resolve_audio_tools(lock_path).ffprobe, path)
    audio_streams = sum(1 for stream in streams if stream.get("codec_type") == "audio")
    if bool(audio_streams) != clip.audio_present:
        raise VideoClipError(
            "clip-declaration-mismatch",
            f"evidence declares audio_present={clip.audio_present} but the local file "
            f"probes {audio_streams} audio stream(s): {path}",
        )
    if audio_forbidden and audio_streams:
        raise VideoClipError(
            "clip-audio-forbidden",
            f"the local file probes {audio_streams} audio stream(s); audio-bearing "
            f"evidence cannot be sent to the video-only specialist: {path}",
        )
    return VerifiedLocalClip(path=path, audio_streams=audio_streams)


__all__ = [
    "VerifiedLocalClip",
    "VideoClipError",
    "VideoClipEvidence",
    "clip_local_path",
    "probe_streams",
    "verify_local_clip",
]
