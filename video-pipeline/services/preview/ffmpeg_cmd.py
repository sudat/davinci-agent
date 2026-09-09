"""Deterministic pinned-ffmpeg argv assembly for the low-resolution preview.

Strategy notes (all verified by probing the pinned binary first):
- hard cuts via integer-exact ``trim``/``atrim`` filters (frame and 48 kHz
  sample indices — no decimal seek arithmetic);
- ``concat`` filter for segment assembly, ``scale`` to 640x360;
- simple overlay: a lavfi ``color`` corner marker with a ``drawbox`` border,
  composited for the full span via ``overlay``;
- audio: item-linked concat plus the BGM fixture through ``amix`` with
  ``normalize=0:duration=first`` when a BGM binding exists;
- subtitles: a locally generated SRT muxed as a soft ``mov_text`` stream;
- ``-r`` on the output is required: without it the VideoToolbox/MP4 track
  reports ``avg_frame_rate=19800/659`` and a one-frame-short duration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.preview.models import (
    PreviewBindingError,
    PreviewLayout,
    PreviewMediaBindings,
)

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate
    from services.preview.tools import PinnedTools

PREVIEW_WIDTH: Final = 640
PREVIEW_HEIGHT: Final = 360
PREVIEW_VERTICAL_WIDTH: Final = 360
PREVIEW_VERTICAL_HEIGHT: Final = 640
MARKER_SIZE: Final = 24
MARKER_MARGIN: Final = 8
MARKER_BORDER: Final = 4
VIDEO_BITRATE: Final = "1500k"
AUDIO_BITRATE: Final = "128k"
VIDEO_TRACK_TIMESCALE: Final = 30000
AUDIO_SAMPLE_RATE_HZ: Final = 48000


@dataclass(frozen=True, slots=True)
class RenderCommand:
    argv: tuple[str, ...]
    filter_complex: str
    marker_input_index: int
    subtitle_input_index: int | None


class _InputTable:
    """Deterministic input ordering: media files in first-use order, then lavfi."""

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self._indices: dict[str, int] = {}
        self._next = 0

    def media(self, path: Path) -> int:
        key = str(path)
        if key not in self._indices:
            self._argv += ["-i", key]
            self._indices[key] = self._next
            self._next += 1
        return self._indices[key]

    def lavfi(self, graph: str) -> int:
        self._argv += ["-f", "lavfi", "-i", graph]
        index = self._next
        self._next += 1
        return index


def _marker_duration_micros(total_frames: int, rate: RationalFrameRate) -> str:
    micros = -(-total_frames * 1_000_000 * rate.den // rate.num)
    whole, remainder = divmod(micros, 1_000_000)
    return f"{whole}.{remainder:06d}"


def _binding_path(bindings: PreviewMediaBindings, item_id: str) -> Path:
    binding = bindings.binding_for(item_id)
    if binding is None:
        raise PreviewBindingError(f"missing media binding for item {item_id}")
    return Path(binding.media_path)


def preview_size_for(output_id: str) -> tuple[int, int]:
    """Low-resolution preview canvas: 640x360 landscape, 360x640 vertical."""
    if output_id == "vertical":
        return (PREVIEW_VERTICAL_WIDTH, PREVIEW_VERTICAL_HEIGHT)
    return (PREVIEW_WIDTH, PREVIEW_HEIGHT)


def build_render_command(  # noqa: PLR0913 (render argv contract: layout/bindings/tools/output/subtitle/size)
    *,
    layout: PreviewLayout,
    bindings: PreviewMediaBindings,
    tools: PinnedTools,
    output: Path,
    subtitle_srt: Path | None,
    preview_width: int = PREVIEW_WIDTH,
    preview_height: int = PREVIEW_HEIGHT,
    bgm_gain_mb: int | None = None,
) -> RenderCommand:
    rate = layout.rate
    argv: list[str] = [str(tools.ffmpeg), "-nostdin", "-y", "-v", "error"]
    inputs = _InputTable(argv)

    video_filters: list[str] = []
    video_pads: list[str] = []
    for index, item in enumerate(layout.video_items):
        source_input = inputs.media(_binding_path(bindings, item.item_id))
        span = item.source.span
        video_filters.append(
            f"[{source_input}:v]trim=start_frame={span.start_frame}:"
            f"end_frame={span.end_frame},setpts=PTS-STARTPTS[v{index}]"
        )
        video_pads.append(f"[v{index}]")
    video_filters.append(f"{''.join(video_pads)}concat=n={len(video_pads)}:v=1:a=0[vcut]")
    video_filters.append(f"[vcut]scale={preview_width}:{preview_height}[vscaled]")
    marker_input = inputs.lavfi(
        f"color=c=black:s={MARKER_SIZE}x{MARKER_SIZE}:r={rate.num}/{rate.den}:"
        f"d={_marker_duration_micros(layout.total_record_frames, rate)}"
    )
    video_filters.append(
        f"[{marker_input}:v]drawbox=x=0:y=0:w={MARKER_SIZE}:h={MARKER_SIZE}:"
        f"color=white:t={MARKER_BORDER}[mark]"
    )
    video_filters.append(
        f"[vscaled][mark]overlay=x=W-w-{MARKER_MARGIN}:y={MARKER_MARGIN}:shortest=0[vout]"
    )

    audio_filters: list[str] = []
    audio_pads: list[str] = []
    for index, item in enumerate(layout.audio_items):
        source_input = inputs.media(_binding_path(bindings, item.item_id))
        span = item.source.span
        start_sample = span.start_frame * layout.samples_per_frame
        end_sample = span.end_frame * layout.samples_per_frame
        audio_filters.append(
            f"[{source_input}:a]atrim=start_sample={start_sample}:"
            f"end_sample={end_sample},asetpts=PTS-STARTPTS[a{index}]"
        )
        audio_pads.append(f"[a{index}]")
    audio_filters.append(f"{''.join(audio_pads)}concat=n={len(audio_pads)}:v=0:a=1[acat]")
    if bindings.bgm is not None:
        bgm_input = inputs.media(Path(bindings.bgm.media_path))
        bgm_source = f"[{bgm_input}:a]"
        if bgm_gain_mb is not None:
            audio_filters.append(f"{bgm_source}volume={bgm_gain_mb / 1000:.3f}dB[bgmvol]")
            bgm_source = "[bgmvol]"
        audio_filters.append(
            f"[acat]{bgm_source}amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[aout]"
        )
    else:
        audio_filters.append("[acat]anull[aout]")

    subtitle_input: int | None = None
    if subtitle_srt is not None:
        subtitle_input = inputs.media(subtitle_srt)

    filter_complex = ";".join([*video_filters, *audio_filters])
    argv += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
    ]
    if subtitle_input is not None:
        argv += ["-map", f"{subtitle_input}:s:0"]
    argv += [
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        VIDEO_BITRATE,
        "-pix_fmt",
        "yuv420p",
        "-video_track_timescale",
        str(VIDEO_TRACK_TIMESCALE),
        "-r",
        f"{rate.num}/{rate.den}",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_SAMPLE_RATE_HZ),
        "-ac",
        "1",
    ]
    if subtitle_input is not None:
        argv += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    argv += ["-movflags", "+faststart", str(output)]
    return RenderCommand(
        argv=tuple(argv),
        filter_complex=filter_complex,
        marker_input_index=marker_input,
        subtitle_input_index=subtitle_input,
    )


__all__ = ["RenderCommand", "build_render_command", "preview_size_for"]
