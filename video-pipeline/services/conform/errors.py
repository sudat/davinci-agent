from __future__ import annotations


class CoordinateError(ValueError):
    """Base class for exact coordinate arithmetic failures.

    Every subclass carries a stable ``LABEL`` so QA probes can report an
    explicit machine-readable error identifier instead of a bare traceback.
    """

    LABEL = "coordinate_error"


class CoordinateOverflowError(CoordinateError):
    """A value exceeded an OverflowGuard threshold (int64 ticks or rate component)."""

    LABEL = "coordinate_overflow"


class NonMonotonicPtsError(CoordinateError):
    """A PTS sequence decreased where non-decreasing order is required."""

    LABEL = "non_monotonic_pts"


class CoordinateRangeError(CoordinateError):
    """A position fell outside the span/placement it must belong to."""

    LABEL = "coordinate_range"


class MalformedCoordinateError(CoordinateError):
    """A raw input could not be parsed into a canonical integer/rational coordinate."""

    LABEL = "malformed_input"
