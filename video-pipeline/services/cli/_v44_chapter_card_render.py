"""The deterministic chapter-card splice render (card raw + master MOV)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from PIL import Image

from services.cli._v44_chapter_card_media import ChapterCardMediaError, run_pinned

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

RENDER_TIMEOUT_SECONDS: Final = 7200


@dataclass(frozen=True, slots=True)
class SpliceInputs:
    """The media paths one splice render consumes and publishes."""

    source: Path
    card_raw: Path
    pcm: Path
    output: Path


@dataclass(frozen=True, slots=True)
class MasterGeometry:
    """Everything the splice render needs besides file paths."""

    width: int
    height: int
    fps: int
    record_frame: int
    card_frames: int
    bitrate: str
    video_timescale: int


def write_card_raw(image: Image.Image, *, frames: int, path: Path) -> None:
    """Write one card frame repeated `frames` times as raw rgb24."""

    if image.mode != "RGB":
        raise ChapterCardMediaError(
            "card-not-rgb", f"card frame mode is {image.mode}, expected RGB"
        )
    if frames <= 0:
        raise ChapterCardMediaError(
            "card-frames-invalid", f"card frame count must be positive: {frames}"
        )
    try:
        with path.open("wb") as handle:
            for _ in range(frames):
                handle.write(image.tobytes())
    except OSError as error:
        raise ChapterCardMediaError(
            "card-write-failed", f"cannot write card frames to {path}: {error}"
        ) from error


def render_master_argv(
    ffmpeg_bin: Path, inputs: SpliceInputs, geometry: MasterGeometry
) -> tuple[str, ...]:
    """The deterministic splice render argv: source pre/card/source-post + exact PCM.

    ``select`` splits the source around the insertion point (``trim`` drops one
    frame in the pinned build); ``concat`` splices the card between.
    """

    source, card_raw, pcm, output = inputs.source, inputs.card_raw, inputs.pcm, inputs.output
    fps = geometry.fps
    record = geometry.record_frame
    graph = (
        f"[0:v]format=yuv420p,split=2[pre][post];"
        f"[pre]select='lt(n\\,{record})',setpts=N/({fps}*TB)[va];"
        f"[post]select='gte(n\\,{record})',setpts=N/({fps}*TB)[vb];"
        f"[1:v]format=yuv420p,fps={fps},settb=AVTB[card];"
        f"[va]settb=AVTB[va2];[vb]settb=AVTB[vb2];"
        f"[va2][card][vb2]concat=n=3:v=1:a=0[vout]"
    )
    return (
        str(ffmpeg_bin),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{geometry.width}x{geometry.height}",
        "-r",
        str(fps),
        "-i",
        str(card_raw),
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-i",
        str(pcm),
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "2:a",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        geometry.bitrate,
        "-r",
        str(fps),
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        str(geometry.video_timescale),
        str(output),
    )


def render_master(
    tools: PinnedTools, inputs: SpliceInputs, geometry: MasterGeometry
) -> tuple[str, ...]:
    """Run the splice render; typed refusal when the pinned ffmpeg fails."""

    argv = render_master_argv(tools.ffmpeg, inputs, geometry)
    run_pinned(tools, argv, timeout=RENDER_TIMEOUT_SECONDS)
    if not inputs.output.is_file():
        raise ChapterCardMediaError(
            "render-no-output", f"render produced no file: {inputs.output}"
        )
    return argv


__all__ = [
    "MasterGeometry",
    "SpliceInputs",
    "render_master",
    "render_master_argv",
    "write_card_raw",
]
