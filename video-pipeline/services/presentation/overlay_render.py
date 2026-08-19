"""Pinned-ffmpeg transparent overlay rendering and pixel verification (Todo 58).

The external path renders the registry asset onto a fully transparent
ProRes 4444 canvas at the deterministic anchor region — the exact argv was
live-verified for determinism (byte-identical reruns) — and the rendered-
presence gate proves presence, position, and duration from extracted frame
pixels: region coverage at anchor frames. API-only success never suffices.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.foundation_io import sha256_file
from services.presentation.overlay_models import (
    OverlayPathDecision,
    OverlayPathError,
    OverlayRegion,
    PresenceAnchor,
    Rgb,
)

FFMPEG_TIMEOUT_SECONDS: Final = 300
EXTRACT_TIMEOUT_SECONDS: Final = 120
COLOR_TOLERANCE: Final = 48
SAMPLE_STEP: Final = 4


class OverlayRenderError(Exception):
    """The pinned render/extraction tool call failed."""


@dataclass(frozen=True, slots=True)
class RenderedOverlay:
    path: Path
    sha256: str
    argv: tuple[str, ...]
    pix_fmt: str
    nb_frames: int
    expected_rgb: Rgb


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    anchor: PresenceAnchor
    coverage_percent: float
    passed: bool
    detail: str


def _run_text(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise OverlayRenderError(f"pinned ffmpeg exceeded {timeout}s: {argv[0]}") from error


def _run_binary(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise OverlayRenderError(f"pinned ffmpeg exceeded {timeout}s: {argv[0]}") from error


def overlay_render_argv(
    *,
    ffmpeg_bin: Path,
    asset: Path,
    region: OverlayRegion,
    timeline_width: int,
    timeline_height: int,
    rate_num: int,
    duration_frames: int,
    output: Path,
) -> tuple[str, ...]:
    """The deterministic transparent-overlay render command (recorded evidence)."""

    return (
        str(ffmpeg_bin),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black@0.0:s={timeline_width}x{timeline_height}:r={rate_num},format=rgba",
        "-i",
        str(asset),
        "-filter_complex",
        (
            f"[1:v]scale={region.width}:{region.height}[ov];"
            f"[0:v][ov]overlay={region.x}:{region.y}:format=auto,format=yuva444p10le[v]"
        ),
        "-map",
        "[v]",
        "-frames:v",
        str(duration_frames),
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-map_metadata",
        "-1",
        str(output),
    )


def _probe_streams(ffprobe_bin: Path, media: Path) -> dict[str, str]:
    result = subprocess.run(
        (
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise OverlayRenderError(f"ffprobe failed: {result.stderr.strip()[-300:]}")
    payload: object = json.loads(result.stdout)
    streams = payload["streams"][0] if isinstance(payload, dict) else {}
    if not isinstance(streams, dict):
        raise OverlayRenderError(f"ffprobe payload malformed for {media}")
    return {
        str(key): str(value)
        for key, value in streams.items()
        if key in ("pix_fmt", "nb_frames", "width", "height")
    }


def render_transparent_overlay(
    *,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    asset: Path,
    asset_sha256: str,
    region: OverlayRegion,
    timeline_width: int,
    timeline_height: int,
    rate_num: int,
    duration_frames: int,
    output: Path,
) -> RenderedOverlay:
    """Render the registry asset as transparent overlay media; typed on drift."""

    if sha256_file(asset) != asset_sha256:
        raise OverlayPathError(
            "overlay_media_hash_drift",
            f"registry asset bytes drifted from the declared hash: {asset}",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    argv = overlay_render_argv(
        ffmpeg_bin=ffmpeg_bin,
        asset=asset,
        region=region,
        timeline_width=timeline_width,
        timeline_height=timeline_height,
        rate_num=rate_num,
        duration_frames=duration_frames,
        output=output,
    )
    result = _run_text(argv, timeout=FFMPEG_TIMEOUT_SECONDS)
    if result.returncode != 0 or not output.is_file():
        raise OverlayRenderError(
            f"transparent overlay render failed: {result.stderr.strip()[-500:]}"
        )
    facts = _probe_streams(ffprobe_bin, output)
    if not facts.get("pix_fmt", "").startswith("yuva"):
        raise OverlayRenderError(
            f"overlay render lost alpha: pix_fmt={facts.get('pix_fmt')!r}"
        )
    if facts.get("nb_frames") != str(duration_frames):
        raise OverlayRenderError(
            f"overlay render frame count {facts.get('nb_frames')} != {duration_frames}"
        )
    if facts.get("width") != str(timeline_width) or facts.get("height") != str(
        timeline_height
    ):
        raise OverlayRenderError(
            f"overlay render geometry {facts.get('width')}x{facts.get('height')} != "
            f"{timeline_width}x{timeline_height}"
        )
    frame = extract_frame(
        ffmpeg_bin,
        output,
        frame_index=0,
        width=timeline_width,
        height=timeline_height,
        pix_fmt="rgba",
    )
    expected_rgb = _region_center_rgb(
        frame, timeline_width, region
    )
    return RenderedOverlay(
        path=output,
        sha256=sha256_file(output),
        argv=argv,
        pix_fmt=facts["pix_fmt"],
        nb_frames=duration_frames,
        expected_rgb=expected_rgb,
    )


def extract_frame(
    ffmpeg_bin: Path,
    media: Path,
    *,
    frame_index: int,
    width: int,
    height: int,
    pix_fmt: str = "rgb24",
) -> bytes:
    """Extract one decoded frame as raw pixels (bounded, deterministic)."""

    argv = (
        str(ffmpeg_bin),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(media),
        "-vf",
        f"select=eq(n\\,{frame_index})",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        pix_fmt,
        "-",
    )
    result = _run_binary(argv, timeout=EXTRACT_TIMEOUT_SECONDS)
    bytes_per_pixel = 4 if pix_fmt == "rgba" else 3
    expected = width * height * bytes_per_pixel
    if result.returncode != 0 or len(result.stdout) != expected:
        raise OverlayRenderError(
            f"frame {frame_index} extraction failed: got {len(result.stdout)} bytes "
            f"(want {expected}): {result.stderr[-300:]!r}"
        )
    return bytes(result.stdout)


def _region_center_rgb(frame: bytes, width: int, region: OverlayRegion) -> Rgb:
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    offset = (y * width + x) * 4
    return (frame[offset], frame[offset + 1], frame[offset + 2])


def region_coverage(
    frame: bytes,
    *,
    width: int,
    region: OverlayRegion,
    expected_rgb: Rgb,
    tolerance: int = COLOR_TOLERANCE,
    sample_step: int = SAMPLE_STEP,
) -> float:
    """Fraction of sampled region pixels matching the overlay color signature."""

    hits = 0
    samples = 0
    er, eg, eb = expected_rgb
    for y in range(region.y, region.y + region.height, sample_step):
        row = y * width
        for x in range(region.x, region.x + region.width, sample_step):
            offset = (row + x) * 3
            r, g, b = frame[offset], frame[offset + 1], frame[offset + 2]
            samples += 1
            if abs(r - er) <= tolerance and abs(g - eg) <= tolerance and abs(b - eb) <= (
                tolerance
            ):
                hits += 1
    return hits / samples if samples else 0.0


def verify_rendered_presence(
    *,
    ffmpeg_bin: Path,
    render_path: Path,
    width: int,
    height: int,
    decision: OverlayPathDecision,
    anchors: tuple[PresenceAnchor, ...],
    expected_rgb: Rgb,
) -> tuple[AnchorCheck, ...]:
    """Verify presence/position/duration from pixels; typed failure otherwise.

    ``anchor.frame_index`` is the record-relative frame (render frame 0 is
    the timeline origin), exactly the coordinate the extracted file frame
    uses.
    """

    checks: list[AnchorCheck] = []
    for anchor in anchors:
        file_frame = anchor.frame_index
        if file_frame < 0:
            checks.append(
                AnchorCheck(
                    anchor=anchor,
                    coverage_percent=-1.0,
                    passed=False,
                    detail=f"anchor {anchor.frame_index} precedes the render origin",
                )
            )
            continue
        frame = extract_frame(
            ffmpeg_bin,
            render_path,
            frame_index=file_frame,
            width=width,
            height=height,
        )
        coverage = region_coverage(
            frame, width=width, region=decision.region, expected_rgb=expected_rgb
        )
        percent = coverage * 100
        passed = (
            percent >= anchor.min_coverage_percent
            if anchor.expect_covered
            else percent < anchor.min_coverage_percent
        )
        checks.append(
            AnchorCheck(
                anchor=anchor,
                coverage_percent=round(percent, 2),
                passed=passed,
                detail=(
                    f"frame {anchor.frame_index} region {decision.region.x},"
                    f"{decision.region.y} {decision.region.width}x{decision.region.height} "
                    f"coverage {percent:.1f}% (expect covered={anchor.expect_covered})"
                ),
            )
        )
    if not checks or not all(check.passed for check in checks):
        failed = "; ".join(check.detail for check in checks if not check.passed)
        raise OverlayPathError(
            "rendered_presence_missing",
            f"{decision.item_id}: rendered presence verification failed: {failed}",
        )
    return tuple(checks)


__all__ = [
    "AnchorCheck",
    "OverlayRenderError",
    "RenderedOverlay",
    "extract_frame",
    "overlay_render_argv",
    "region_coverage",
    "render_transparent_overlay",
    "verify_rendered_presence",
]
