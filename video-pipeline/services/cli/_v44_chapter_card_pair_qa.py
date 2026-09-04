"""Sparse sampled-pair QA: one pinned extraction per media file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_chapter_card_media import (
    ChapterCardMediaError,
    FrameGeometry,
    extract_selected_frames,
    frame_diff,
)
from services.cli._v44_chapter_card_qa import (
    CANVAS_H,
    CANVAS_W,
    ChapterCardQAError,
    require_qa_dir,
    save_png,
)

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

MAX_MEAN_ABS: Final = 2.0
MAX_MISMATCHED_FRACTION: Final = 0.02
# output frames whose decoded pixels are compared with the mapped source frame
SAMPLED_PAIRS: Final = (
    (0, 0),
    (89, 89),
    (90, 90),
    (1631, 1631),
    (1677, 1632),
    (3045, 3000),
    (6045, 6000),
    (7836, 7791),
    (1800, 1755),
    (4000, 3955),
    (7000, 6955),
)


@dataclass(frozen=True, slots=True)
class FramePairResult:
    output_frame: int
    source_frame: int | None
    png: str
    max_abs: int
    mean_abs: float
    mismatched_fraction: float


def verify_source_pairs(
    tools: PinnedTools,
    source: Path,
    master: Path,
    *,
    samples: tuple[tuple[int, int], ...] | None = None,
    qa_dir: Path = Path("qa"),
) -> tuple[FramePairResult, ...]:
    """Compare decoded output frames against their mapped decoded source frames.

    All sampled indices are collected into one pinned extraction per media
    file, so the default table costs exactly two decode subprocesses.
    """

    selected = SAMPLED_PAIRS if samples is None else samples
    require_qa_dir(qa_dir)
    geometry = FrameGeometry(CANVAS_W, CANVAS_H)
    try:
        output_frames = extract_selected_frames(
            tools, master, geometry, tuple(sorted({output for output, _ in selected}))
        )
        source_frames = extract_selected_frames(
            tools, source, geometry, tuple(sorted({source for _, source in selected}))
        )
    except ChapterCardMediaError as error:
        raise ChapterCardQAError("frame-extract-failed", str(error)) from error
    results: list[FramePairResult] = []
    for output_frame, source_frame in selected:
        out_frame = output_frames[output_frame]
        src_frame = source_frames[source_frame]
        save_png(out_frame, qa_dir / f"frame-{output_frame:06d}.png")
        save_png(src_frame, qa_dir / f"source-{source_frame:06d}.png")
        diff = frame_diff(out_frame, src_frame, geometry)
        if diff.mean_abs > MAX_MEAN_ABS or diff.mismatched_fraction > MAX_MISMATCHED_FRACTION:
            raise ChapterCardQAError(
                "frame-pair-drift",
                f"output frame {output_frame} vs source {source_frame}: mean {diff.mean_abs:.3f} "
                f"mismatched {diff.mismatched_fraction:.4f}",
            )
        results.append(
            FramePairResult(output_frame, source_frame, f"qa/frame-{output_frame:06d}.png",
                            diff.max_abs, diff.mean_abs, diff.mismatched_fraction)
        )
    return tuple(results)


__all__ = [
    "MAX_MEAN_ABS",
    "MAX_MISMATCHED_FRACTION",
    "SAMPLED_PAIRS",
    "FramePairResult",
    "verify_source_pairs",
]
