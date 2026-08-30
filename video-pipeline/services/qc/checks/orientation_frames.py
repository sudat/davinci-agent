"""Quarter-turn frame-hypothesis math for the orientation QC check.

Pure arithmetic over pinned-ffmpeg grayscale frames: rotate an edit-source
frame by each clockwise quarter turn, aspect-fit it onto the observation
canvas with black padding, and score it against a render frame by
normalized cross-correlation. No image dependencies.
"""

from __future__ import annotations

from typing import Final

#: Grayscale observation canvas (width, height) for the NCC hypotheses.
FRAME_SIZE: Final[tuple[int, int]] = (96, 54)
_QUARTER_TURNS: Final[tuple[int, ...]] = (0, 1, 2, 3)

#: Inverse of a CW quarter-turn ``turn`` on an HxW grid, as
#: row = a*i + b*j + c*(H-1), col = d*i + e*j + f*(W-1) where (i, j) are
#: coordinates in the rotated image.
_INVERSE_CW: Final[dict[int, tuple[int, int, int, int, int, int]]] = {
    0: (1, 0, 0, 0, 1, 0),
    1: (0, -1, 1, 1, 0, 0),
    2: (-1, 0, 1, 0, -1, 1),
    3: (0, 1, 0, -1, 0, 1),
}


def hypothesis_frame(source: list[float], turn: int) -> list[float]:
    """The source frame rotated ``turn`` quarter-turns clockwise and
    aspect-fit with black padding onto the render's canvas."""
    w, h = FRAME_SIZE
    src_h, src_w = FRAME_SIZE[1], FRAME_SIZE[0]
    rot_rows, rot_cols = (src_w, src_h) if turn % 2 else (src_h, src_w)
    scale = min(w / rot_cols, h / rot_rows)
    fit_cols = max(1, round(rot_cols * scale))
    fit_rows = max(1, round(rot_rows * scale))
    y0, x0 = (h - fit_rows) // 2, (w - fit_cols) // 2
    a, b, c, d, e, f = _INVERSE_CW[turn]
    canvas = [0.0] * (w * h)
    for y in range(y0, y0 + fit_rows):
        big_i = (y - y0) * rot_rows // fit_rows
        base = y * w
        for x in range(x0, x0 + fit_cols):
            big_j = (x - x0) * rot_cols // fit_cols
            row = a * big_i + b * big_j + c * (src_h - 1)
            col = d * big_i + e * big_j + f * (src_w - 1)
            canvas[base + x] = source[row * src_w + col]
    return canvas


def ncc(left: list[float], right: list[float]) -> float:
    """Normalized cross-correlation of two equal-length frames (invariant
    to per-frame affine intensity changes, so grades do not skew it)."""
    size = len(left)
    left_mean = sum(left) / size
    right_mean = sum(right) / size
    cross = dot_left = dot_right = 0.0
    for a, b in zip(left, right, strict=True):
        da = a - left_mean
        db = b - right_mean
        cross += da * db
        dot_left += da * da
        dot_right += db * db
    denominator = (dot_left * dot_right) ** 0.5
    return cross / denominator if denominator > 0.0 else 0.0


def quarter_scores(
    render_frame: list[float], source_frame: list[float]
) -> dict[int, float]:
    """NCC of the render frame against each clockwise quarter-turn
    hypothesis of the source frame; the upright hypothesis must dominate."""
    return {
        turn: ncc(render_frame, hypothesis_frame(source_frame, turn))
        for turn in _QUARTER_TURNS
    }


__all__ = ["FRAME_SIZE", "hypothesis_frame", "ncc", "quarter_scores"]
