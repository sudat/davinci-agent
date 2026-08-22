"""Deterministic pinned-ffmpeg argv assembly for the Editorial Preview v2.

Mirrors the v1 adapter's integer-exact conventions (``trim``/``atrim`` on
frame and 48 kHz sample indices, ``concat``, 640x360 ``scale``) and adds the
PRD 13.1 editorial layers:

- B-roll/insert cut-ins: full-frame ``overlay`` gated by ``enable`` with
  ``between(t, S*den/num, E*den/num)`` (replacement, not picture-in-picture;
  the +-1 frame enable-window boundary tolerance is documented fidelity);
- still/graphic roles: a white ``color`` card with a ``drawbox`` border at
  the placement span (placeholder title — the pinned ffmpeg has no
  drawtext/libass, so no text is burned; labels live in the trace manifest);
- Moment Deep Review flags: a red 24x24 corner-marker ``overlay`` at the
  flagged span (topmost layer, ``FLAG_HOLD_FRAMES`` long);
- audio: dialogue ``atrim`` segments sample-delayed via ``adelay``'s ``S``
  suffix plus placeholder ``sine`` tones for music/ambience/sfx placements,
  mixed with ``amix ... normalize=0:duration=longest`` and hard-trimmed to
  the exact record extent in samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.preview.v2_models import MediaAudioView, ToneAudioView

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools
    from services.preview.v2_models import EditorialLayoutV2

PREVIEW_WIDTH: Final = 640
PREVIEW_HEIGHT: Final = 360
MARKER_SIZE: Final = 24
MARKER_MARGIN: Final = 8
CARD_BORDER: Final = 8
VIDEO_BITRATE: Final = "1500k"
AUDIO_BITRATE: Final = "128k"
VIDEO_TRACK_TIMESCALE: Final = 30000
AUDIO_SAMPLE_RATE_HZ: Final = 48000
FLAG_HOLD_FRAMES: Final = 30
FLAG_MARKER_COLOR: Final = "red"
TITLE_CARD_COLOR: Final = "white"

FLAG_X: Final = PREVIEW_WIDTH - MARKER_SIZE - MARKER_MARGIN
FLAG_Y: Final = MARKER_MARGIN


@dataclass(frozen=True, slots=True)
class EditorialRenderCommand:
    argv: tuple[str, ...]
    filter_complex: str


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


def _duration_text(frames: int, rate: Fraction) -> str:
    """Lavfi duration parameter, rounded up to whole microseconds."""

    micros = -(-frames * 1_000_000 * rate.denominator // rate.numerator)
    whole, remainder = divmod(micros, 1_000_000)
    return f"{whole}.{remainder:06d}"


def _enable_expr(start_frame: int, end_frame: int, rate: Fraction) -> str:
    return (
        f"between(t,{start_frame}*{rate.denominator}/{rate.numerator},"
        f"{end_frame}*{rate.denominator}/{rate.numerator})"
    )


def _video_chain(
    layout: EditorialLayoutV2, inputs: _InputTable, rate: Fraction, total_text: str
) -> tuple[list[str], str]:
    filters: list[str] = []
    pads: list[str] = []
    for index, clip in enumerate(layout.primary):
        source_input = inputs.media(clip.media_path)
        span = clip.source_span
        filters.append(
            f"[{source_input}:v]trim=start_frame={span[0]}:end_frame={span[1]},"
            f"setpts=PTS-STARTPTS[pv{index}]"
        )
        pads.append(f"[pv{index}]")
    filters.append(f"{''.join(pads)}concat=n={len(pads)}:v=1:a=0[pvcat]")
    filters.append(f"[pvcat]scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}[pvbase]")
    current = "[pvbase]"

    for index, cut in enumerate(layout.media_cut_ins):
        source_input = inputs.media(cut.media_path)
        span = cut.source_span
        filters.append(
            f"[{source_input}:v]trim=start_frame={span[0]}:end_frame={span[1]},"
            f"setpts=PTS-STARTPTS,scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}[ci{index}]"
        )
        filters.append(
            f"{current}[ci{index}]overlay=x=0:y=0:"
            f"enable='{_enable_expr(cut.record_span[0], cut.record_span[1], rate)}'[vc{index}]"
        )
        current = f"[vc{index}]"

    for index, card in enumerate(layout.card_views):
        card_input = inputs.lavfi(
            f"color=c={TITLE_CARD_COLOR}:s={PREVIEW_WIDTH}x{PREVIEW_HEIGHT}:"
            f"r={layout.rate.num}/{layout.rate.den}:d={total_text}"
        )
        filters.append(
            f"[{card_input}:v]drawbox=x=0:y=0:w={PREVIEW_WIDTH}:h={PREVIEW_HEIGHT}:"
            f"color=black@0.5:t={CARD_BORDER}[tc{index}]"
        )
        filters.append(
            f"{current}[tc{index}]overlay=x=0:y=0:"
            f"enable='{_enable_expr(card.record_span[0], card.record_span[1], rate)}'[vt{index}]"
        )
        current = f"[vt{index}]"

    if layout.flags:
        flag_input = inputs.lavfi(
            f"color=c={FLAG_MARKER_COLOR}:s={MARKER_SIZE}x{MARKER_SIZE}:"
            f"r={layout.rate.num}/{layout.rate.den}:d={total_text}"
        )
        for index, flag in enumerate(layout.flags):
            end = flag.at_frame + FLAG_HOLD_FRAMES
            filters.append(
                f"{current}[{flag_input}:v]overlay=x={FLAG_X}:y={FLAG_Y}:"
                f"enable='{_enable_expr(flag.at_frame, end, rate)}'[vf{index}]"
            )
            current = f"[vf{index}]"
    return filters, current


def _audio_chain(layout: EditorialLayoutV2, inputs: _InputTable, total_text: str) -> list[str]:
    filters: list[str] = []
    pads: list[str] = []
    for index, segment in enumerate(layout.audio_segments):
        match segment:
            case MediaAudioView():
                media_input = inputs.media(segment.media_path)
                chain = (
                    f"[{media_input}:a]atrim=start_sample={segment.source_start_sample}:"
                    f"end_sample={segment.source_end_sample},asetpts=PTS-STARTPTS"
                )
            case ToneAudioView():
                tone_input = inputs.lavfi(
                    f"sine=frequency={segment.tone_hz}:sample_rate={AUDIO_SAMPLE_RATE_HZ}:"
                    f"d={total_text}"
                )
                chain = (
                    f"[{tone_input}:a]atrim=start_sample=0:"
                    f"end_sample={segment.length_samples},asetpts=PTS-STARTPTS"
                )
        if segment.delay_samples > 0:
            chain += f",adelay={segment.delay_samples}S:all=1"
        filters.append(f"{chain}[as{index}]")
        pads.append(f"[as{index}]")

    total_samples = layout.total_record_frames * layout.samples_per_frame
    if pads:
        filters.append(
            f"{''.join(pads)}amix=inputs={len(pads)}:duration=longest:"
            f"dropout_transition=0:normalize=0,atrim=end_sample={total_samples},"
            f"asetpts=PTS-STARTPTS[aout]"
        )
    else:
        silence = inputs.lavfi(f"anullsrc=r={AUDIO_SAMPLE_RATE_HZ}:cl=mono:d={total_text}")
        filters.append(f"[{silence}:a]atrim=end_sample={total_samples},asetpts=PTS-STARTPTS[aout]")
    return filters


def build_editorial_command(
    *,
    layout: EditorialLayoutV2,
    tools: PinnedTools,
    output: Path,
    subtitle_srt: Path | None,
) -> EditorialRenderCommand:
    rate_fraction = Fraction(layout.rate.num, layout.rate.den)
    total_text = _duration_text(layout.total_record_frames, rate_fraction)
    argv: list[str] = [str(tools.ffmpeg), "-nostdin", "-y", "-v", "error"]
    inputs = _InputTable(argv)

    video_filters, video_out = _video_chain(layout, inputs, rate_fraction, total_text)
    audio_filters = _audio_chain(layout, inputs, total_text)

    subtitle_input: int | None = None
    if subtitle_srt is not None:
        subtitle_input = inputs.media(subtitle_srt)

    filter_complex = ";".join([*video_filters, *audio_filters])
    argv += ["-filter_complex", filter_complex, "-map", video_out, "-map", "[aout]"]
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
        f"{layout.rate.num}/{layout.rate.den}",
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
        argv += ["-c:s", "mov_text", "-metadata:s:s:0", "language=jpn"]
    argv += ["-movflags", "+faststart", str(output)]
    return EditorialRenderCommand(argv=tuple(argv), filter_complex=filter_complex)


__all__ = [
    "FLAG_HOLD_FRAMES",
    "EditorialRenderCommand",
    "build_editorial_command",
]
