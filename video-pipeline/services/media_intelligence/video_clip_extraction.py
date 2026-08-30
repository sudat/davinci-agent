"""T4 deterministic short-clip extraction through the pinned ffmpeg.

One exact half-open Edit Source interval becomes TWO hashed local clips:
the Gemini clip retains the interval's audio (when the source has any) and
the GLM clip is encoded with ``-an`` so no audio can ever leave the
machine — verified by ffprobe BEFORE the evidence objects exist. Both
clips live only in the caller-provided run workspace (rebuildable
evidence, deterministic names, never under version control). Frame→second
math uses the EXACT rational frame rate (``30000/1001`` is never
approximated as 30 — T9 lesson).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.audio_probe import (
    DEFAULT_LOCK_PATH,
    PinnedAudioTools,
    resolve_audio_tools,
    run_bounded,
)
from services.analyze.visual_decode import probe_video_facts
from services.foundation_io import sha256_file
from services.media_intelligence.video_clip_evidence import (
    VideoClipError,
    VideoClipEvidence,
    probe_streams,
)

if TYPE_CHECKING:
    from services.analyze.visual_models import VisualStreamFacts
    from services.media_intelligence.moment_review import ReviewWindow

_CLIP_DIR_NAME: Final = "moment-review-clips"
_ENCODE_TIMEOUT_SEC: Final = 300

#: Review-clip scaling (MEASURED 2026-08-30, T11 pre-flight on the real
#: 3840x2160 v44-real-01 mezzanine): a full 3600-frame local-map window at
#: mezzanine resolution is 800,410,980 bytes — 53x over the providers'
#: 15 MiB media bound. Model-facing review clips are therefore scaled down
#: to at most 960px wide at 700k video / 64k audio, which bounds the
#: worst-case 3600-frame window at 11,614,068 bytes (base64 ≈ 15.5 MB,
#: inside the 20 MiB total-request bound). Sources already at or below the
#: review width pass through untouched.
_REVIEW_WIDTH: Final = 960
_REVIEW_VIDEO_BITRATE: Final = "700k"
_REVIEW_AUDIO_BITRATE: Final = "64k"


def _review_scale_filter(facts: VisualStreamFacts) -> tuple[str, ...]:
    """``-vf scale=W:-2`` only when the source is wider than the review width."""

    if facts.width <= _REVIEW_WIDTH:
        return ()
    return ("-vf", f"scale={_REVIEW_WIDTH}:-2")


@dataclass(frozen=True, slots=True)
class ClipEvidencePair:
    """The two clips one interval yields: audio-kept (Gemini) and silent (GLM)."""

    gemini: VideoClipEvidence
    glm: VideoClipEvidence


def _seek_seconds(facts: VisualStreamFacts, frame: int) -> str:
    """Exact rational frame→seconds, truncated DOWN to the micro (the shared
    accurate-seek boundary discipline: truncation keeps the boundary frame)."""

    seconds = Fraction(frame * facts.rate_den, facts.rate_num)
    micros = floor(seconds * 1_000_000)
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def _duration(facts: VisualStreamFacts, span: int) -> tuple[str, float]:
    """Exact rational span→(seconds-string, seconds-float); the string CEILS
    to the micro so audio is never truncated before the last video frame."""

    seconds = Fraction(span * facts.rate_den, facts.rate_num)
    micros = ceil(seconds * 1_000_000)
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}", float(seconds)


@dataclass(frozen=True, slots=True)
class _ClipPlan:
    """One output clip's extraction plan (grouped inputs, Smell-2 discipline)."""

    target: Path
    window: ReviewWindow
    start: int
    span: int
    duration_str: str
    duration_value: float


@dataclass(frozen=True, slots=True)
class ClipExtractor:
    """Deterministic short-clip extraction over one Edit Source mezzanine."""

    media_path: Path
    workspace_dir: Path
    lock_path: Path = DEFAULT_LOCK_PATH

    def extract(self, window: ReviewWindow) -> ClipEvidencePair:
        """Extract + verify both clips for ``window`` (half-open frames)."""

        start, end = int(window.start_frame), int(window.end_frame)
        span = end - start
        if span < 1:
            raise VideoClipError(
                "clip-range-empty",
                f"clip window [{start}, {end}) must be a forward half-open interval",
            )
        tools = resolve_audio_tools(self.lock_path)
        facts = probe_video_facts(tools.ffprobe, self.media_path)
        if end > facts.frame_count:
            raise VideoClipError(
                "clip-window-outside-source",
                f"clip window [{start}, {end}) exceeds the {facts.frame_count}-frame stream",
            )
        source_streams = probe_streams(tools.ffprobe, self.media_path)
        audio_present = any(stream.get("codec_type") == "audio" for stream in source_streams)
        duration_str, duration_value = _duration(facts, span)

        clip_dir = self.workspace_dir / _CLIP_DIR_NAME
        clip_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{sha256_file(self.media_path)[:12]}-{start:06d}-{end:06d}"
        gemini = _ClipPlan(
            clip_dir / f"clip-av-{stem}.mp4", window, start, span, duration_str, duration_value
        )
        glm = _ClipPlan(
            clip_dir / f"clip-v-{stem}.mp4", window, start, span, duration_str, duration_value
        )
        self._encode(tools, facts, gemini, with_audio=audio_present)
        self._encode(tools, facts, glm, with_audio=False)
        return ClipEvidencePair(
            gemini=self._verified_clip(
                tools,
                gemini,
                expected_audio=1 if audio_present else 0,
                audio_present=audio_present,
            ),
            glm=self._verified_clip(tools, glm, expected_audio=0, audio_present=False),
        )

    def _encode(
        self,
        tools: PinnedAudioTools,
        facts: VisualStreamFacts,
        plan: _ClipPlan,
        *,
        with_audio: bool,
    ) -> None:
        scale = _review_scale_filter(facts)
        middle: tuple[str, ...] = (
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            _REVIEW_AUDIO_BITRATE,
        ) if with_audio else ("-an",)
        argv = (
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-ss",
            _seek_seconds(facts, plan.start),
            "-i",
            str(self.media_path),
            "-map",
            "0:v:0",
            *middle,
            *scale,
            "-b:v",
            _REVIEW_VIDEO_BITRATE,
            "-frames:v",
            str(plan.span),
            "-t",
            plan.duration_str,
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(plan.target),
        )
        result = run_bounded(argv, _ENCODE_TIMEOUT_SEC, "clip extraction")
        if result.returncode != 0 or not plan.target.is_file():
            raise VideoClipError(
                "clip-extraction-failed",
                f"ffmpeg clip extraction failed for {plan.target.name}: "
                f"{result.stderr.strip()[-300:]}",
            )

    def _verified_clip(
        self, tools: PinnedAudioTools, plan: _ClipPlan, *, expected_audio: int, audio_present: bool
    ) -> VideoClipEvidence:
        """ffprobe-verify frame count + audio streams BEFORE building evidence."""

        streams = probe_streams(tools.ffprobe, plan.target)
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        frames = [stream.get("nb_frames") for stream in video]
        if len(video) != 1 or not isinstance(frames[0], str) or int(frames[0]) != plan.span:
            raise VideoClipError(
                "clip-verification-failed",
                f"{plan.target.name}: expected exactly {plan.span} video frames, probed "
                f"{len(video)} video stream(s) with nb_frames={frames}",
            )
        if len(audio) != expected_audio:
            raise VideoClipError(
                "clip-verification-failed",
                f"{plan.target.name}: expected {expected_audio} audio stream(s), "
                f"probed {len(audio)}",
            )
        return VideoClipEvidence(
            ref=plan.target.as_uri(),
            sha256=sha256_file(plan.target),
            requested_range=plan.window,
            analyzed_range=plan.window,
            audio_present=audio_present,
            duration_seconds=plan.duration_value,
        )


__all__ = [
    "ClipEvidencePair",
    "ClipExtractor",
]
