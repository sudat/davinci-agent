"""Measured-media honesty gates for the live replay (Todo 67 / F3).

Anchor frame extraction uses the exact ffmpeg selection shape the F3
verifier re-runs, render-policy facts come only from measured ffprobe
bytes, and a render claim is verified by full recompute — never by
trusting the claim's own success statement.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from services.foundation_io import sha256_file
from services.release.live_models import AnchorFrame, AnchorPosition, RenderPolicy

PROBE_TIMEOUT_SECONDS: Final = 120
ANCHOR_ORDER: tuple[AnchorPosition, ...] = (
    "frame-0",
    "frame-25pct",
    "frame-50pct",
    "frame-75pct",
    "frame-last",
)


DURATION_TOLERANCE_MS: Final = 250


def declared_render_policy() -> RenderPolicy:
    """The fixed final-render declaration the live flow must reproduce.

    Duration carries an explicit declared tolerance: MP4 muxing with AAC
    adds codec padding (priming samples) beyond the exact 600-frame/30fps
    program duration, so container duration is compared within bounds.
    """

    return RenderPolicy(
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        frame_rate="30/1",
        frame_count=600,
        duration_ms=20000,
        duration_tolerance_ms=DURATION_TOLERANCE_MS,
        audio_channels=2,
        audio_sample_rate_hz=48000,
        audio_layout="stereo",
    )


def policy_matches(declared: RenderPolicy, measured: RenderPolicy) -> bool:
    """Exact policy equality except duration, which compares within the
    declared tolerance."""

    duration_fields = {"duration_ms", "duration_tolerance_ms"}
    if measured.model_dump(exclude=duration_fields) != declared.model_dump(
        exclude=duration_fields
    ):
        return False
    return abs(measured.duration_ms - declared.duration_ms) <= declared.duration_tolerance_ms


def anchor_indices(frame_count: int) -> dict[AnchorPosition, int]:
    """Anchor frame numbers for {0, 25%, 50%, 75%, last} (deduplicated)."""

    if frame_count < 1:
        raise ValueError(f"frame_count must be positive, got {frame_count}")
    quarters = frame_count // 4
    return {
        "frame-0": 0,
        "frame-25pct": quarters,
        "frame-50pct": 2 * quarters,
        "frame-75pct": 3 * quarters,
        "frame-last": frame_count - 1,
    }


def _select_expression(indices: Mapping[AnchorPosition, int]) -> str:
    return "+".join(f"eq(n\\,{indices[position]})" for position in ANCHOR_ORDER)


def anchor_extraction_argv(
    ffmpeg_bin: Path, media: Path, out_dir: Path, indices: Mapping[AnchorPosition, int]
) -> tuple[str, ...]:
    """The exact, reproducible anchor-extraction command (bmp output)."""

    return (
        str(ffmpeg_bin),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(media),
        "-vf",
        f"select='{_select_expression(indices)}'",
        "-fps_mode",
        "vfr",
        str(out_dir / "frame-%02d.bmp"),
    )


def extract_anchor_frames(
    ffmpeg_bin: Path,
    media: Path,
    out_dir: Path,
    indices: Mapping[AnchorPosition, int],
) -> tuple[AnchorFrame, ...]:
    """Extract the five anchor frames deterministically (bmp; the pinned
    ffmpeg build ships no png encoder, bmp is byte-stable)."""

    out_dir.mkdir(parents=True, exist_ok=True)
    argv = anchor_extraction_argv(ffmpeg_bin, media, out_dir, indices)
    result = subprocess.run(
        argv, check=False, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise RuntimeError(f"anchor extraction failed: {result.stderr[-300:]!r}")
    frames: list[AnchorFrame] = []
    for order, position in enumerate(ANCHOR_ORDER, start=1):
        image = out_dir / f"frame-{order:02d}.bmp"
        if not image.is_file():
            raise RuntimeError(f"anchor frame missing: {image}")
        frames.append(
            AnchorFrame(
                position=position,
                frame_index=indices[position],
                frame_sha256=sha256_file(image),
            )
        )
    return tuple(frames)


def _probe_payload(ffprobe_bin: Path, media: Path) -> dict[str, object]:
    result = subprocess.run(
        (
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-count_frames",
            "-show_streams",
            "-show_format",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[-300:]}")
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"ffprobe payload malformed: {media}")
    return payload


def _stream_of(payload: dict[str, object], codec_type: str) -> dict[str, object]:
    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == codec_type:
                return stream
    raise RuntimeError(f"no {codec_type} stream")


def _as_str(stream: dict[str, object], key: str) -> str:
    value = stream.get(key)
    return value if isinstance(value, str) else ""


def _as_duration_ms(fmt: object) -> int:
    if isinstance(fmt, dict) and isinstance(fmt.get("duration"), str):
        return int(float(str(fmt["duration"])) * 1000)
    return 0


def _as_int(stream: dict[str, object], key: str) -> int:
    value = stream.get(key)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int):
        return value
    raise RuntimeError(f"field {key} missing or non-integer")


def measure_render_policy(ffprobe_bin: Path, media: Path) -> RenderPolicy:
    """Measure the render policy from the rendered bytes only."""

    payload = _probe_payload(ffprobe_bin, media)
    video = _stream_of(payload, "video")
    audio = _stream_of(payload, "audio")
    fmt = payload.get("format")
    raw_name = fmt.get("format_name") if isinstance(fmt, dict) else None
    format_name = str(raw_name) if isinstance(raw_name, str) else ""
    container = "mp4" if "mp4" in format_name else format_name
    layout = _as_str(audio, "channel_layout") or f"{_as_int(audio, 'channels')}ch"
    duration_ms = _as_duration_ms(fmt)
    return RenderPolicy(
        container=container,
        video_codec=_as_str(video, "codec_name"),
        audio_codec=_as_str(audio, "codec_name"),
        width=_as_int(video, "width"),
        height=_as_int(video, "height"),
        frame_rate=_as_str(video, "r_frame_rate"),
        frame_count=_as_int(video, "nb_read_frames"),
        duration_ms=duration_ms,
        audio_channels=_as_int(audio, "channels"),
        audio_sample_rate_hz=_as_int(audio, "sample_rate"),
        audio_layout=layout,
    )


ClaimOutcome = Literal[
    "verified",
    "claim_missing_output",
    "claim_hash_mismatch",
    "claim_policy_mismatch",
]


@dataclass(frozen=True, slots=True)
class RenderClaimResult:
    """Outcome of a render-claim verification by recompute."""

    outcome: ClaimOutcome
    detail: str
    measured_policy: RenderPolicy | None = None


def verify_render_claim(
    claim_path: Path,
    *,
    declared_sha256: str,
    declared_policy: RenderPolicy,
    measure: Callable[[Path], RenderPolicy],
) -> RenderClaimResult:
    """Detect a step that claims success without doing the work."""

    if not claim_path.is_file():
        return RenderClaimResult(
            "claim_missing_output", f"claimed output absent: {claim_path}", None
        )
    actual = sha256_file(claim_path)
    if actual != declared_sha256:
        return RenderClaimResult(
            "claim_hash_mismatch",
            f"claimed sha256 {declared_sha256[:12]} != recomputed {actual[:12]}",
            None,
        )
    measured = measure(claim_path)
    if not policy_matches(declared_policy, measured):
        return RenderClaimResult(
            "claim_policy_mismatch",
            f"declared policy != measured policy (frame_count "
            f"{declared_policy.frame_count} vs {measured.frame_count})",
            measured,
        )
    return RenderClaimResult("verified", "claim matches recomputed bytes and policy", measured)


__all__ = [
    "ClaimOutcome",
    "RenderClaimResult",
    "anchor_extraction_argv",
    "anchor_indices",
    "declared_render_policy",
    "extract_anchor_frames",
    "measure_render_policy",
    "policy_matches",
    "verify_render_claim",
]
