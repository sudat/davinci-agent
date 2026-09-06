"""Per-operation correct-readback table (safeguard E, suda order 2026-09-06).

Kills cause 3 (Opus 2026-09-06T08:30:40Z: 原因3 見るべき値を間違えた):
today's costliest error — the speed workflow watched ``duration`` while
retime (ripple-off, timing-kept) leaves the record length UNCHANGED and
shrinks the SOURCE extent instead (run5b: 226→114 = 451x0.25 while
duration sat at 451). Three operations were scored "failed" while
correctly applied, because the verdict read the wrong value.

The rule: when a verdict-C (coordinate+Direct) operation runs, the table
drives which readback executes — :func:`readback_for` returns the spec
for the operation, and an operation with NO table row is refused with
:class:`~services.cu_client.errors.ReadbackUndefinedError` (fail-closed:
an unknown operation must gain an explicit row with a measured readback,
never a guessed one). Rows are verbatim Opus 2026-09-06T08:30:40Z:

  速度      → source extent (duration は盲目) + render cadence
  音量      → render RMS (API の read route は壊れている)
  カラー    → render pixel diff (GetCDL が存在しない)
  字幕本文  → get_transcript
  Transform → get_transform
"""

from __future__ import annotations

from typing import Final

from services.contracts.primitives import StrictModel
from services.cu_client.errors import ReadbackUndefinedError


class ReadbackSpec(StrictModel):
    """Where the verdict for one operation lives, and what is blind."""

    operation: str
    primary: str
    secondary: str | None = None
    blind: str | None = None
    note: str = ""


READBACKS: Final[tuple[ReadbackSpec, ...]] = (
    ReadbackSpec(
        operation="speed",
        primary="source extent (source_start/source_end via source_range_report)",
        secondary="render cadence (rec N = src N*x-ratio, SSIM; competing ratios rejected)",
        blind="duration (record length unchanged by ripple-off retime)",
        note="run5b 2026-09-06: duration 451 flat while extent 226→114",
    ),
    ReadbackSpec(
        operation="volume",
        primary="render RMS (dB delta exact, e.g. -6.0dB 2026-09-06)",
        blind="API read route (GetProperty returns null on this clip shape)",
        note="keyboard ±dB commands measured inert; Inspector field route applies",
    ),
    ReadbackSpec(
        operation="color_lift",
        primary="render pixel diff (changed-pixel count + mean luma delta)",
        blind="GetCDL (no such API; probe_node_graph is structure-only)",
        note="item11 2026-09-06: 2,073,597/2,073,600 px changed, luma +21.94",
    ),
    ReadbackSpec(
        operation="subtitle_text",
        primary="get_transcript (queue text exact match, others unchanged)",
        note="only C-route item: no API setter, no keymap entry (617 lines, 0 hits)",
    ),
    ReadbackSpec(
        operation="transform",
        primary="get_transform (Zoom/Pan/Tilt/Rotation readback)",
        note="API route (verdict A); the table row keeps C-runs honest too",
    ),
)

_BY_OPERATION: Final = {spec.operation: spec for spec in READBACKS}


def readback_for(operation: str) -> ReadbackSpec:
    """Return the readback spec driving the verdict for ``operation``.

    Unknown operations raise instead of guessing — add a measured row
    first (Opus ban: no generic failure-detection frameworks; this table
    is the explicit list, not a pattern matcher).
    """
    try:
        return _BY_OPERATION[operation]
    except KeyError:
        raise ReadbackUndefinedError(
            f"no readback row for {operation!r}; known: {sorted(_BY_OPERATION)}"
        ) from None


__all__ = ["READBACKS", "ReadbackSpec", "readback_for"]
