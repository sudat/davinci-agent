"""Frozen constants for the minimum visual checks (Todo 35).

Every threshold, decode parameter, contact-sheet layout value, rule id, and
confidence used by the visual analyzers is declared here exactly once and
hashed into the analyzer output, so any change to a frozen value changes the
artifact content hash instead of silently mixing results.

Scale conventions (no floats anywhere in canonical output):

- ``_m`` suffixed luma fields are integer permille of the 8-bit full scale
  255 (``sum * 1000 // (255 * pixels)``);
- Laplacian energy is the integer mean of squared integer Laplacian values
  over interior pixels (``lap2``); flat frames are exactly 0;
- confidences are integer permille 0-1000; spans are half-open frame spans.

Determinism choice (recorded): the pinned ffmpeg build has NO PNG encoder,
so contact sheets are written by the pure-Python zlib+struct PNG writer
(``contact_sheet.py``) — same frames in, byte-identical PNG out.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Final

ANALYZER_NAME: Final = "analyze-visual"
ANALYZER_VERSION: Final = "todo35-v1"

# bounded decode (frozen): raw gray luma at a fixed analysis size.
#
# DECODE_TIMEOUT_SEC is the FLOOR of a per-frame-scaled budget (see
# visual_decode.decode_budget_seconds) — MEASURED 2026-08-24 on the real
# v44-real-01 mezzanine class (4K h264_videotoolbox, 64x36 gray decode):
# 1.24 s / 300 frames = 4.1 ms/frame; the full 8468-frame mezzanine
# projects to ~35 s, comfortably inside the 120 s floor, and the scaled
# formula keeps longer real episodes bounded by ~10 ms/frame.
DECODE_W: Final = 64
DECODE_H: Final = 36
DECODE_FILTER: Final = f"scale={DECODE_W}:{DECODE_H},format=gray"
DECODE_TIMEOUT_SEC: Final = 120

# MAX_DECODE_FRAMES is a decode-BUDGET guard, not a correctness limit
# (PRD §2.5 measured blocker: 900 was sized for <=30 s fixtures; the
# representative 282 s episode's mezzanine carries 8468 CFR30 frames and
# the analyzer decodes EVERY frame — scene/black/blur/exposure checks are
# consecutive-frame pairs, so sampling would change semantics). It is a
# POLICY input: the default stays small for tests; a validated real run
# raises it explicitly through the ``V44_MAX_DECODE_FRAMES`` env var
# (e.g. 12000 for v44-real-01), and the effective value flows into
# FROZEN_CONSTANTS_PAYLOAD so the artifact hash records which policy
# produced it.
DEFAULT_MAX_DECODE_FRAMES: Final = 900
_MAX_DECODE_FRAMES_ENV: Final = "V44_MAX_DECODE_FRAMES"


def _resolve_max_decode_frames() -> int:
    raw = os.environ.get(_MAX_DECODE_FRAMES_ENV)
    if raw is None:
        return DEFAULT_MAX_DECODE_FRAMES
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            f"{_MAX_DECODE_FRAMES_ENV}={raw!r} must be an integer frame ceiling"
        ) from error
    if value < 1:
        raise ValueError(f"{_MAX_DECODE_FRAMES_ENV}={raw!r} must be >= 1")
    return value


MAX_DECODE_FRAMES: Final = _resolve_max_decode_frames()

# metadata-only ffprobe budget (no decode; the decode probe in
# services/normalize/probe.py carries its own measured scaling)
PROBE_TIMEOUT_SEC: Final = 120

# scene change (frozen): mean-abs-luma-diff between consecutive decoded
# frames, integer permille of full scale; the SAME-mean boundary of a
# blur transition sits far below this and deliberately does not fire.
SCENE_MIN_MEAN_DIFF_M: Final = 100
SCENE_RULE_ID: Final = "scene-mean-diff-v1"
CONFIDENCE_SCENE_CHANGE: Final = 800

# black (frozen): mean luma permille AND the near-black pixel fraction
BLACK_MAX_MEAN_M: Final = 50
BLACK_PIXEL_MAX_SAMPLE: Final = 32
BLACK_MIN_PIXEL_FRACTION_M: Final = 990
BLACK_RULE_ID: Final = "black-mean-fraction-v1"
CONFIDENCE_BLACK_SPAN: Final = 900

# blur (frozen): integer mean-square Laplacian over interior pixels;
# evaluated only on frames that are neither black nor out-of-bounds
# exposure (flat black/white frames cannot evidence blur).
BLUR_MAX_LAPLACIAN_MEAN_SQUARE: Final = 500
BLUR_RULE_ID: Final = "blur-laplacian-energy-v1"
CONFIDENCE_BLUR_SPAN: Final = 700

# exposure (frozen): luma mean bounds + clipped-high fraction
EXPOSURE_LOW_MEAN_M: Final = 30
EXPOSURE_HIGH_MEAN_M: Final = 900
CLIPPED_HIGH_SAMPLE: Final = 250
OVER_MIN_CLIPPED_FRACTION_M: Final = 900
EXPOSURE_RULE_ID: Final = "exposure-mean-bounds-v1"
CONFIDENCE_EXPOSURE_SPAN: Final = 850

# contact sheet (frozen): every Nth decoded frame, fixed grid, pure-Python
# zlib PNG (the pinned ffmpeg has no PNG encoder; zlib level is pinned so
# the bytes are reproducible). The sheet is REBUILDABLE evidence.
SHEET_CADENCE_FRAMES: Final = 10
SHEET_COLS: Final = 5
SHEET_ZLIB_LEVEL: Final = 9
SHEET_GENERATOR: Final = "python-zlib-png-v1"
SHEET_RULE_ID: Final = "contact-sheet-png-v1"

ARTIFACT_TYPE: Final = "analysis_visual_minimum"
SCHEMA_VERSION: Final = "visual-analysis-v1"
ARTIFACT_NAME: Final = "visual-analysis-artifact.json"
SHEET_NAME: Final = "contact-sheet.png"

FROZEN_CONSTANTS_PAYLOAD: Final = {
    "analyzer_version": ANALYZER_VERSION,
    "decode_w": DECODE_W,
    "decode_h": DECODE_H,
    "decode_filter": DECODE_FILTER,
    "max_decode_frames": MAX_DECODE_FRAMES,
    "decode_timeout_sec": DECODE_TIMEOUT_SEC,
    "scene_min_mean_diff_m": SCENE_MIN_MEAN_DIFF_M,
    "black_max_mean_m": BLACK_MAX_MEAN_M,
    "black_pixel_max_sample": BLACK_PIXEL_MAX_SAMPLE,
    "black_min_pixel_fraction_m": BLACK_MIN_PIXEL_FRACTION_M,
    "blur_max_laplacian_mean_square": BLUR_MAX_LAPLACIAN_MEAN_SQUARE,
    "exposure_low_mean_m": EXPOSURE_LOW_MEAN_M,
    "exposure_high_mean_m": EXPOSURE_HIGH_MEAN_M,
    "clipped_high_sample": CLIPPED_HIGH_SAMPLE,
    "over_min_clipped_fraction_m": OVER_MIN_CLIPPED_FRACTION_M,
    "confidence_scene_change": CONFIDENCE_SCENE_CHANGE,
    "confidence_black_span": CONFIDENCE_BLACK_SPAN,
    "confidence_blur_span": CONFIDENCE_BLUR_SPAN,
    "confidence_exposure_span": CONFIDENCE_EXPOSURE_SPAN,
    "sheet_cadence_frames": SHEET_CADENCE_FRAMES,
    "sheet_cols": SHEET_COLS,
    "sheet_zlib_level": SHEET_ZLIB_LEVEL,
    "sheet_generator": SHEET_GENERATOR,
    "scene_rule_id": SCENE_RULE_ID,
    "black_rule_id": BLACK_RULE_ID,
    "blur_rule_id": BLUR_RULE_ID,
    "exposure_rule_id": EXPOSURE_RULE_ID,
    "sheet_rule_id": SHEET_RULE_ID,
}


def frozen_constants_hash() -> str:
    canonical = json.dumps(
        FROZEN_CONSTANTS_PAYLOAD, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
