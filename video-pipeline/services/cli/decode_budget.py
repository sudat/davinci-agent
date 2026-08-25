"""Decode budget scaling for cockpit-spawned real episodes (V44-1 delta).

The analyzer's ``V44_MAX_DECODE_FRAMES`` policy seam stays operator-
overridable (env wins). When the env is unset, this module derives a
conservative ceiling from the SOURCE frame count so a real 4K episode
(8467 frames) does not typed-fail at the frozen 900 default, while
genuinely absurd inputs still refuse at the 24000 hard ceiling.

Pure function for testability; runner + chain call it lazily.
"""

from __future__ import annotations

from math import ceil
from typing import Final

DEFAULT_MAX_DECODE_FRAMES: Final = 900
DECODE_BUDGET_CEILING: Final = 24_000
DECODE_FALLBACK_BUDGET: Final = 12_000
SCALE_FACTOR: Final = 1.2


def scaled_decode_budget(frame_count: int | None) -> int | None:
    """Return the scaled decode ceiling, or ``None`` if no scaling needed.

    - ``None`` frame_count (unknown at runner startup) -> conservative
      fallback 12000 so the chain can still pass analyze.
    - ``<= 900`` -> ``None`` (frozen default unchanged; test seam intact).
    - ``> 900`` -> ``ceil(frame_count * 1.2)`` bounded ``[900, 24000]``.
    - ``> ceiling`` -> ``24000`` (typed refusal for absurd sizes still holds
      at the ceiling; operator override still wins via env).
    """

    if frame_count is None:
        return DECODE_FALLBACK_BUDGET
    if frame_count <= DEFAULT_MAX_DECODE_FRAMES:
        return None
    scaled = ceil(frame_count * SCALE_FACTOR)
    return min(max(scaled, DEFAULT_MAX_DECODE_FRAMES), DECODE_BUDGET_CEILING)


__all__ = [
    "DECODE_BUDGET_CEILING",
    "DECODE_FALLBACK_BUDGET",
    "DEFAULT_MAX_DECODE_FRAMES",
    "SCALE_FACTOR",
    "scaled_decode_budget",
]
