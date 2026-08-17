"""Deterministic contact sheets for the minimum visual checks (Todo 35).

DETERMINISM CONTRACT (recorded choice): the pinned ffmpeg build ships NO PNG
encoder, so sheets are written by this pure-Python writer — 8-bit grayscale
PNG assembled with ``struct`` + ``zlib`` at a pinned compression level. The
pinned Python's zlib is deterministic for identical input rows, so the same
decoded frames always produce byte-identical sheet bytes (verified by double
generation in the tests). Frames are the frozen-cadence decoded luma frames
themselves (the thumbnails ARE the 64x36 analysis luma), laid out on a fixed
grid; trailing empty grid cells pad to black. Sheets are REBUILDABLE
evidence, never authoritative artifacts.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.analyze.visual_constants import (
    ANALYZER_VERSION,
    DECODE_H,
    DECODE_W,
    SHEET_CADENCE_FRAMES,
    SHEET_COLS,
    SHEET_GENERATOR,
    SHEET_RULE_ID,
    SHEET_ZLIB_LEVEL,
)
from services.analyze.visual_models import (
    DecodeBinding,
    SheetRecord,
    decode_binding_hash,
)
from services.analyze.visual_results import CheckProvenance
from services.foundation_io import atomic_write, sha256_file

if TYPE_CHECKING:
    from services.analyze.visual_decode import LumaFrame

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload))
    )


def encode_gray_png(width: int, height: int, rows: bytes) -> bytes:
    """Minimal deterministic 8-bit grayscale PNG (filter byte 0 per line)."""

    if len(rows) != width * height:
        raise ValueError(f"rows length {len(rows)} != {width}x{height}")
    scanlines = b"".join(b"\x00" + rows[y * width : (y + 1) * width] for y in range(height))
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines, SHEET_ZLIB_LEVEL))
        + _chunk(b"IEND", b"")
    )


def sheet_frame_indexes(frame_count: int) -> tuple[int, ...]:
    return tuple(range(0, frame_count, SHEET_CADENCE_FRAMES))


def build_contact_sheet(
    frames: tuple[LumaFrame, ...],
    binding: DecodeBinding,
    input_hashes: tuple[str, ...],
    *,
    out_path: Path,
) -> SheetRecord:
    """Assemble the frozen-cadence grid sheet and write it atomically."""

    wanted = set(sheet_frame_indexes(len(frames)))
    selected = [frame for frame in frames if frame.frame_index in wanted]
    if not selected:
        raise ValueError("no frames selected for the contact sheet")
    thumb_count = len(selected)
    rows = -(-thumb_count // SHEET_COLS)  # ceil
    sheet_w = SHEET_COLS * DECODE_W
    sheet_h = rows * DECODE_H
    grid = bytearray(sheet_w * sheet_h)  # zero-filled: empty cells pad black
    for slot, frame in enumerate(selected):
        cell_row, cell_col = divmod(slot, SHEET_COLS)
        for line in range(DECODE_H):
            dst = (cell_row * DECODE_H + line) * sheet_w + cell_col * DECODE_W
            src = line * DECODE_W
            grid[dst : dst + DECODE_W] = frame.luma[src : src + DECODE_W]
    payload = encode_gray_png(sheet_w, sheet_h, bytes(grid))
    atomic_write(out_path, payload)
    return SheetRecord(
        path=out_path.name,
        sha256=sha256_file(out_path),
        frame_indexes=tuple(frame.frame_index for frame in selected),
        cols=SHEET_COLS,
        rows=rows,
        thumb_w=DECODE_W,
        thumb_h=DECODE_H,
        generator=SHEET_GENERATOR,
        cadence_frames=SHEET_CADENCE_FRAMES,
        rebuildable=True,
        provenance=CheckProvenance(
            analyzer_version=ANALYZER_VERSION,
            rule_id=SHEET_RULE_ID,
            decode_binding_sha256=decode_binding_hash(binding),
            input_artifact_hashes=input_hashes,
        ),
    )


__all__ = [
    "build_contact_sheet",
    "encode_gray_png",
    "sheet_frame_indexes",
]
