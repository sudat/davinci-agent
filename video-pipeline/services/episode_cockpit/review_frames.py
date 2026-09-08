"""Gated real-frame materials for review investigations (工程2 rework #1).

When ``config/editorial-runtime.json`` carries
``"review_frame_materials": {"enabled": true, "max_frames": 3}``, the
feelings / both-different investigation routes pull a few read-only stills
from the episode's preview video (fallback: the intake source folder's
first video file) around the player position into
``<episode>/review-frames/`` and attach them to the LLM call.

Default OFF: the gate absent/false means NO extraction, no images to any
model, and no ``frames`` key in ``checked_materials`` — behavior is byte
for byte today's. The gate key IS the operator permission for frame-pixel
egress (PRD v4.4 §23); with it off, frame data never leaves the machine.

Honesty: a still is ONE frame. It is never an audio or whole-video
verification and nothing downstream may claim it is. Extraction, an
ATTEMPTED image-capable transport call WITH the stills (invocation fact;
pre-spawn failures included — actual transport-level delivery is
UNCONFIRMED), and a returned verification are separate facts
(``frames`` / ``frames_delivery_attempted`` / ``frames_verified`` in
``checked_materials``) and are never conflated. Everything written
here is rebuildable runtime state under the episode dir (the source media
is only ever READ).
"""

# allow: SIZE_OK — one gated-frame-materials concern per file (the gate
# loader, the route seam, the read-only extractor and its honesty docs are
# ONE story; splitting the seam from the extractor scatters the
# read-only-only invariant the gate exists to enforce).

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from services.episode_cockpit.episode_ops import INTAKE_NAME
from services.episode_cockpit.models import FrameMaterial, IntakeRecordV1
from services.episode_cockpit.review_interpreter import (
    _CONFIG_ROOT,
    RUNTIME_CONFIG_RELATIVE,
    _read_json_object,
)
from services.preview.models import PreviewError
from services.preview.render import PREVIEW_NAME
from services.preview.tools import load_pinned_tools

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

FRAME_MATERIALS_KEY: Final = "review_frame_materials"
DEFAULT_MAX_FRAMES: Final = 3
REVIEW_FRAMES_DIR: Final = "review-frames"
FRAME_MAX_WIDTH: Final = 512
FRAME_EXTRACT_TIMEOUT_SECONDS: Final = 60
_AROUND_OFFSETS_SECONDS: Final = (-2.0, 0.0, 2.0)
_VIDEO_SUFFIXES: Final = frozenset({".mp4", ".mov", ".m4v"})


class FrameMaterialWorkspace(Protocol):
    """The workspace surface the route seam needs (FileOps satisfies it)."""

    def gather_review_frames(
        self, episode_id: str, *, at_seconds: float | None, max_frames: int
    ) -> tuple[FrameMaterial, ...]: ...


def frame_gate(runtime_path: Path | None = None) -> tuple[bool, int]:
    """(enabled, max_frames) from the SAME editorial-runtime.json the
    transport factory reads; anything absent/malformed is OFF."""

    runtime: Mapping[str, object] | None = _read_json_object(
        runtime_path or (_CONFIG_ROOT / RUNTIME_CONFIG_RELATIVE)
    )
    raw: object = None if runtime is None else runtime.get(FRAME_MATERIALS_KEY)
    if not isinstance(raw, dict):
        return (False, DEFAULT_MAX_FRAMES)
    max_frames: object = raw.get("max_frames", DEFAULT_MAX_FRAMES)
    bounded = (
        DEFAULT_MAX_FRAMES
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1
        else max_frames
    )
    return (raw.get("enabled") is True, bounded)


def gather_route_frame_materials(
    workspace: FrameMaterialWorkspace,
    episode_id: str,
    *,
    at_seconds: float | None,
    eligible: bool,
) -> tuple[FrameMaterial, ...]:
    """The ONE route-level seam: gate first, then read-only extraction via
    the workspace. ``eligible`` is the route's own investigation decision
    (feelings-class message / both-different reaction)."""

    enabled, max_frames = frame_gate()
    if not enabled or not eligible:
        return ()
    return workspace.gather_review_frames(
        episode_id, at_seconds=at_seconds, max_frames=max_frames
    )


def extract_review_frames(
    episode_dir: Path, at_seconds: float | None, *, max_frames: int
) -> tuple[FrameMaterial, ...]:
    """Read-only stills around ``at_seconds`` (else evenly spaced over the
    video). Every failure mode — no media, no pinned ffmpeg, a failed
    decode — is an HONEST SKIP (logged, empty result), never an error the
    review-chat route has to surface."""

    source = _resolve_source(episode_dir)
    if source is None:
        return ()
    try:
        tools = load_pinned_tools()
    except (PreviewError, OSError) as error:
        _LOGGER.warning("review-frames: pinned ffmpeg unavailable, skipping (%s)", error)
        return ()
    positions = _positions(tools.ffprobe, source, at_seconds, max_frames)
    if not positions:
        return ()
    out_dir = episode_dir / REVIEW_FRAMES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[FrameMaterial] = []
    for index, position in enumerate(positions):
        target = out_dir / f"frame-{index:03d}.jpg"
        if _extract_still(tools.ffmpeg, source, position, target):
            records.append(
                FrameMaterial(path=str(target), at_seconds=position, source=str(source))
            )
    return tuple(records)


def _resolve_source(episode_dir: Path) -> Path | None:
    """The preview video, else the intake source folder's first video file."""

    preview = episode_dir / "previews" / PREVIEW_NAME
    if preview.is_file():
        return preview
    try:
        record = IntakeRecordV1.model_validate_json(
            (episode_dir / INTAKE_NAME).read_bytes()
        )
    except (OSError, ValueError):
        return None
    folder = Path(record.source_folder)
    if not folder.is_dir():
        return None
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in _VIDEO_SUFFIXES:
            return entry
    return None


def _positions(
    ffprobe: Path, source: Path, at_seconds: float | None, max_frames: int
) -> tuple[float, ...]:
    """Candidate still stamps. Around ``at_seconds`` the spread is ±2s
    (``_AROUND_OFFSETS_SECONDS``) clamped into the decodable span. Clamping
    can collapse offsets onto the SAME instant when the playhead sits at a
    video edge (at=ceiling → at-2, at, at): instead of silently delivering
    fewer stills than asked (the 2-stills live observation), the stamps are
    RE-SPREAD as ``max_frames`` distinct positions over the feasible window.
    Pinned observed behavior (8s testsrc2): at=1.0 → (0.0, 1.0, 3.0);
    end-collapsed at=2.0 on a 2.0s clip → (0.0, 1.0, 2.0)."""

    duration, ceiling = _probe_media(ffprobe, source)
    if at_seconds is not None:
        wanted = sorted(
            {
                position if ceiling is None else min(position, ceiling)
                for position in (
                    max(0.0, at_seconds + offset) for offset in _AROUND_OFFSETS_SECONDS
                )
            }
        )
        if len(wanted) >= max_frames:
            return tuple(wanted[:max_frames])
        high = ceiling if ceiling is not None else (wanted[-1] if wanted else 0.0)
        if high <= 0.0:
            return (0.0,) if wanted else ()
        return tuple(high * index / (max_frames - 1) for index in range(max_frames))
    if duration is None or duration <= 0.0:
        return ()
    return tuple(
        duration * (index + 0.5) / max_frames for index in range(max_frames)
    )


def _probe_media(ffprobe: Path, source: Path) -> tuple[float | None, float | None]:
    """(duration, decodable_ceiling); ceiling = (nb_frames - 1) / fps from
    the stream itself, None when either is missing."""

    try:
        result = subprocess.run(
            (
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=nb_frames,avg_frame_rate",
                "-of",
                "default=noprint_wrappers=1",
                str(source),
            ),
            check=False,
            capture_output=True,
            timeout=FRAME_EXTRACT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return (None, None)
    except (OSError, subprocess.TimeoutExpired) as error:
        _LOGGER.warning("review-frames: ffprobe failed for %s (%s)", source, error)
        return (None, None)
    facts: dict[str, str] = {}
    for line in result.stdout.decode(errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            facts[key.strip()] = value.strip()
    duration: float | None
    try:
        duration = float(facts.get("duration", ""))
    except ValueError:
        duration = None
    ceiling: float | None = None
    try:
        frames = int(facts.get("nb_frames", ""))
        rate_num, _, rate_den = facts.get("avg_frame_rate", "").partition("/")
        rate = int(rate_num) / int(rate_den)
        if frames > 0 and rate > 0:
            ceiling = (frames - 1) / rate
    except (ValueError, ZeroDivisionError):
        ceiling = None
    return (duration, ceiling)


def _extract_still(ffmpeg: Path, source: Path, at: float, target: Path) -> bool:
    """One honest still attempt: False on any failure — including a seek
    past the last frame's PTS, which decodes nothing (observed on an 8s
    15fps clip: stamps >119/15 ≈ 7.933 fail; the ceiling clamp in
    ``_positions`` keeps planned stamps decodable) — and the caller logs
    the skip while the remaining stamps still extract."""

    try:
        result = subprocess.run(
            (
                str(ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{at:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale=w='min(iw,{FRAME_MAX_WIDTH})':h=-2",
                "-q:v",
                "2",
                str(target),
            ),
            check=False,
            capture_output=True,
            timeout=FRAME_EXTRACT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _LOGGER.warning("review-frames: still at %.3fs failed, skipping (%s)", at, error)
        return False
    if result.returncode != 0 or not target.is_file():
        _LOGGER.warning(
            "review-frames: still at %.3fs failed (%s), skipping", at, target
        )
        return False
    return True


__all__ = [
    "DEFAULT_MAX_FRAMES",
    "FRAME_MATERIALS_KEY",
    "REVIEW_FRAMES_DIR",
    "FrameMaterial",
    "FrameMaterialWorkspace",
    "extract_review_frames",
    "frame_gate",
    "gather_route_frame_materials",
]
