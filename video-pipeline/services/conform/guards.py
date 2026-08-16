"""OverflowGuard thresholds for exact coordinate arithmetic.

All canonical coordinates are integers or integer pairs. Python ints are
arbitrary precision, so the guard exists for artifact/interchange safety, not
to prevent memory faults:

- ``MAX_PTS_TICKS`` keeps every stored tick inside the signed int64 range that
  interchange formats (Parquet conform maps, ffprobe JSON, Resolve API) use.
- ``MAX_RATE_COMPONENT`` rejects pathological rate/time_base numerators and
  denominators. Real media rates live far below this bound (the largest tick
  denominator in practical use is 90000), and bounding components keeps every
  cross product of two canonical values comfortably inside int64.
"""

from __future__ import annotations

from services.conform.errors import CoordinateOverflowError

MAX_PTS_TICKS = (1 << 63) - 1
MAX_RATE_COMPONENT = (1 << 31) - 1


def guard_int64(value: int, what: str) -> int:
    """Return ``value`` when it lies in ``[0, MAX_PTS_TICKS]``; raise otherwise."""

    if value < 0 or value > MAX_PTS_TICKS:
        raise CoordinateOverflowError(f"{what} {value} outside [0, {MAX_PTS_TICKS}]")
    return value


def validate_rate_component(value: int, what: str) -> None:
    """Reject zero/negative or beyond-guard rate components."""

    if value <= 0 or value > MAX_RATE_COMPONENT:
        raise CoordinateOverflowError(f"{what} {value} outside (0, {MAX_RATE_COMPONENT}]")


def validate_rate(num: int, den: int, what: str) -> None:
    """Validate both components of a rational rate or time base."""

    validate_rate_component(num, f"{what}.num")
    validate_rate_component(den, f"{what}.den")
