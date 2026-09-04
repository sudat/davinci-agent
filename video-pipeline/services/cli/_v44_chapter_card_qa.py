"""Card-span visual QA: streamed per-frame checks over a raw decode dump."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkstemp
from typing import TYPE_CHECKING, Final, TypedDict

from PIL import Image

from services.cli._v44_chapter_card_media import (
    ChapterCardMediaError,
    FrameGeometry,
    FrameWindow,
    dump_frames_raw,
    frame_diff,
)
from services.presentation.chapter_card import card_structural_report

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

CANVAS_W: Final = 1920
CANVAS_H: Final = 1080
FRAME_BYTES: Final = CANVAS_W * CANVAS_H * 3
DECODED_INK_THRESHOLD: Final = 64
DECODED_CENTERING_ERROR: Final = 24
CARD_PNG_SAMPLES: Final = (1632, 1654, 1676)
MIN_WHITE_LEVEL: Final = 200
MAX_BACKGROUND_LEVEL: Final = 24
BACKGROUND_MARGIN_PX: Final = 16
BOUND_VALUES: Final = 4
MAX_NEIGHBOR_MAX_ABS: Final = 96
MAX_NEIGHBOR_MEAN_ABS: Final = 0.05


class ChapterCardQAError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class CardSpanReport(TypedDict):
    card_frames: int
    structural_ok: bool
    per_frame_white_min: int
    background_max: int
    background_margin_px: int
    max_neighbor_diff: int
    max_neighbor_mean: float
    ink_bounds: list[int]
    ink_fraction: float
    sampled_pngs: list[str]


def save_png(frame: bytes | memoryview, path: Path) -> None:
    try:
        Image.frombytes("RGB", (CANVAS_W, CANVAS_H), frame).save(
            path, format="PNG", compress_level=1
        )
    except OSError as error:
        raise ChapterCardQAError("qa-file-failed", f"cannot write {path}: {error}") from error


def require_qa_dir(qa_dir: Path) -> None:
    try:
        qa_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ChapterCardQAError(
            "qa-file-failed", f"cannot create QA directory {qa_dir}: {error}"
        ) from error


def _peak(hist: list[int]) -> int:
    return max((index for index, count in enumerate(hist) if count), default=0)


def _background_peak(frame: bytes | memoryview, bounds: tuple[int, int, int, int]) -> int:
    """Peak luma outside the ink bounding box dilated by the anti-alias margin."""

    left, top, right, bottom = bounds
    x0, y0 = max(0, left - BACKGROUND_MARGIN_PX), max(0, top - BACKGROUND_MARGIN_PX)
    x1 = min(CANVAS_W, right + BACKGROUND_MARGIN_PX)
    y1 = min(CANVAS_H, bottom + BACKGROUND_MARGIN_PX)
    gray = Image.frombytes("RGB", (CANVAS_W, CANVAS_H), frame).convert("L")
    strips = (
        gray.crop((0, 0, CANVAS_W, y0)),
        gray.crop((0, y1, CANVAS_W, CANVAS_H)),
        gray.crop((0, y0, x0, y1)),
        gray.crop((x1, y0, CANVAS_W, y1)),
    )
    return max(_peak(strip.histogram()) for strip in strips if strip.size[0] and strip.size[1])


def _enforce_static_card(
    whites: list[int], backgrounds: list[int], neighbor_max: int, neighbor_mean: float
) -> None:
    """Refuse dim glyphs, non-black background, or codec-scale frame changes."""

    dim = [index for index, white in enumerate(whites) if white < MIN_WHITE_LEVEL]
    if dim:
        raise ChapterCardQAError(
            "card-frame-dim",
            f"card frames {dim} never reach white ink level {MIN_WHITE_LEVEL}",
        )
    gray_like = [
        index for index, background in enumerate(backgrounds) if background > MAX_BACKGROUND_LEVEL
    ]
    if gray_like:
        raise ChapterCardQAError(
            "card-background-not-black",
            f"card frames {gray_like} carry non-black background outside the glyph "
            f"(peak {max(backgrounds[index] for index in gray_like)} > {MAX_BACKGROUND_LEVEL})",
        )
    if neighbor_max > MAX_NEIGHBOR_MAX_ABS or neighbor_mean > MAX_NEIGHBOR_MEAN_ABS:
        raise ChapterCardQAError(
            "card-frame-changing",
            f"card frames change across the span: max {neighbor_max} (limit "
            f"{MAX_NEIGHBOR_MAX_ABS}), mean {neighbor_mean:.4f} (limit {MAX_NEIGHBOR_MEAN_ABS})",
        )


def _decoded_bounds(
    frame: bytes | memoryview, index: int
) -> tuple[tuple[int, int, int, int], float]:
    """Structural report for one card frame; typed refusal when it fails."""

    report = card_structural_report(
        frame,
        ink_threshold=DECODED_INK_THRESHOLD,
        centering_error=DECODED_CENTERING_ERROR,
    )
    raw_bounds = report["bounds"]
    raw_fraction = report["ink_fraction"]
    if (
        report["ok"] is not True
        or not isinstance(raw_bounds, list)
        or len(raw_bounds) != BOUND_VALUES
        or not isinstance(raw_fraction, float)
    ):
        raise ChapterCardQAError(
            "card-structure-failed",
            f"card frame {index} failed the decoded structural check: {report['reason']}",
        )
    return (raw_bounds[0], raw_bounds[1], raw_bounds[2], raw_bounds[3]), raw_fraction


def _exclusive_raw_span(qa_dir: Path) -> Path:
    """Reserve the raw span dump exclusively; ffmpeg overwrites it in place."""

    try:
        descriptor, name = mkstemp(prefix=".v44-span-", suffix=".raw", dir=qa_dir)
    except OSError as error:
        raise ChapterCardQAError(
            "qa-file-failed", f"cannot reserve a raw span file in {qa_dir}: {error}"
        ) from error
    os.close(descriptor)
    return Path(name)


def _iter_raw_frames(raw_path: Path, count: int) -> Iterator[bytes]:
    """Yield one frame at a time; the span never materializes as one object."""

    try:
        with raw_path.open("rb") as handle:
            for index in range(count):
                frame = handle.read(FRAME_BYTES)
                if len(frame) != FRAME_BYTES:
                    raise ChapterCardQAError(
                        "card-extract-failed",
                        f"raw span file ended early at frame {index} of {count}",
                    )
                yield frame
    except OSError as error:
        raise ChapterCardQAError(
            "qa-file-failed", f"cannot stream raw span file {raw_path}: {error}"
        ) from error


@dataclass(slots=True)
class _SpanScan:
    whites: list[int] = field(default_factory=list)
    backgrounds: list[int] = field(default_factory=list)
    neighbor_max: int = 0
    neighbor_mean: float = 0.0
    first_bounds: tuple[int, int, int, int] | None = None
    first_fraction: float = 0.0
    png_frames: dict[int, bytes] = field(default_factory=dict)


def _canvas() -> FrameGeometry:
    return FrameGeometry(CANVAS_W, CANVAS_H)


def _scan_span(
    tools: PinnedTools,
    master: Path,
    raw_path: Path,
    *,
    record_frame: int,
    card_frames: int,
) -> _SpanScan:
    """Dump the span once, then stream every per-frame check from the file."""

    window = FrameWindow(_canvas(), record_frame, card_frames)
    try:
        dump_frames_raw(tools, master, window, raw_path)
    except ChapterCardMediaError as error:
        raise ChapterCardQAError("card-extract-failed", str(error)) from error
    wanted_png = frozenset({0} | {sample - record_frame for sample in CARD_PNG_SAMPLES})
    scan = _SpanScan()
    previous: bytes | None = None
    for index, frame in enumerate(_iter_raw_frames(raw_path, card_frames)):
        bounds, fraction = _decoded_bounds(frame, index)
        scan.whites.append(max(frame))
        scan.backgrounds.append(_background_peak(frame, bounds))
        if scan.first_bounds is None:
            scan.first_bounds, scan.first_fraction = bounds, fraction
        if previous is not None:
            diff = frame_diff(previous, frame, _canvas())
            scan.neighbor_max = max(scan.neighbor_max, diff.max_abs)
            scan.neighbor_mean = max(scan.neighbor_mean, diff.mean_abs)
        previous = frame
        if index in wanted_png:
            scan.png_frames[index] = frame
    _enforce_static_card(scan.whites, scan.backgrounds, scan.neighbor_max, scan.neighbor_mean)
    return scan


def _span_report(scan: _SpanScan, card_frames: int) -> CardSpanReport:
    if scan.first_bounds is None:
        raise ChapterCardQAError("card-structure-failed", "no card frames were extracted")
    return {
        "card_frames": card_frames,
        "structural_ok": True,
        "per_frame_white_min": min(scan.whites),
        "background_max": max(scan.backgrounds),
        "background_margin_px": BACKGROUND_MARGIN_PX,
        "max_neighbor_diff": scan.neighbor_max,
        "max_neighbor_mean": round(scan.neighbor_mean, 6),
        "ink_bounds": list(scan.first_bounds),
        "ink_fraction": scan.first_fraction,
        "sampled_pngs": [f"qa/frame-{frame:06d}.png" for frame in CARD_PNG_SAMPLES],
    }


def verify_card_span(
    tools: PinnedTools,
    master: Path,
    *,
    record_frame: int,
    card_frames: int,
    qa_dir: Path,
) -> CardSpanReport:
    """Stream all card frames from a raw dump: white glyphs, black field, static.

    Frames are read one at a time from the dump file, so peak memory holds a
    few frames (samples + neighbor) instead of the whole ~267 MiB span.
    """

    require_qa_dir(qa_dir)
    raw_path = _exclusive_raw_span(qa_dir)
    try:
        scan = _scan_span(
            tools, master, raw_path, record_frame=record_frame, card_frames=card_frames
        )
    finally:
        with suppress(OSError):
            raw_path.unlink(missing_ok=True)
    for output_frame in CARD_PNG_SAMPLES:
        save_png(
            scan.png_frames[output_frame - record_frame], qa_dir / f"frame-{output_frame:06d}.png"
        )
    save_png(scan.png_frames[0], qa_dir / "card-first.png")
    return _span_report(scan, card_frames)


__all__ = [
    "CANVAS_H",
    "CANVAS_W",
    "FRAME_BYTES",
    "CardSpanReport",
    "ChapterCardQAError",
    "require_qa_dir",
    "save_png",
    "verify_card_span",
]
