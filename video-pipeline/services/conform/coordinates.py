"""Value objects for the canonical coordinate systems (PRD 9.2).

Three canonical coordinate systems plus the timeline record axis:

- Original Timestamp: integer ``pts`` ticks + reduced ``RationalTimeBase``.
- Edit Video Position: integer frame + ``RationalFrameRate`` (contracts).
- Edit Audio Position: integer sample + integer sample rate (``SampleSpan``).
- Record Position: integer timeline frame (``RecordFrameSpan`` in contracts).

Every span is half-open ``[start, end)``. Fields are strict integers only —
float seconds are never accepted as canonical storage, and every serialized
field is an int or an int pair.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from math import gcd
from typing import Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.conform.errors import NonMonotonicPtsError
from services.conform.guards import MAX_PTS_TICKS, MAX_RATE_COMPONENT
from services.contracts.primitives import PositiveInteger, StrictModel


class RationalTimeBase(StrictModel):
    """Tick duration as a reduced positive rational (e.g. ffmpeg ``1/90000``)."""

    num: PositiveInteger
    den: PositiveInteger

    @model_validator(mode="before")
    @classmethod
    def reduce_terms(cls, data: object) -> object:
        if isinstance(data, dict):
            num = data.get("num")
            den = data.get("den")
            if isinstance(num, int) and isinstance(den, int) and num > 0 and den > 0:
                divisor = gcd(num, den)
                if divisor > 1:
                    return {"num": num // divisor, "den": den // divisor}
        return data

    @model_validator(mode="after")
    def components_bounded(self) -> Self:
        if self.num > MAX_RATE_COMPONENT or self.den > MAX_RATE_COMPONENT:
            raise PydanticCustomError(
                "coordinate_overflow",
                "time_base component {num}/{den} beyond guard {limit}",
                {"num": self.num, "den": self.den, "limit": MAX_RATE_COMPONENT},
            )
        return self

    @property
    def as_fraction(self) -> Fraction:
        return Fraction(self.num, self.den)


class OriginalTimestamp(StrictModel):
    """A position on an Original stream: int64-bounded integer ``pts`` ticks."""

    pts: int = Field(ge=0)
    time_base: RationalTimeBase

    @model_validator(mode="after")
    def pts_within_int64_guard(self) -> Self:
        if self.pts > MAX_PTS_TICKS:
            raise PydanticCustomError(
                "coordinate_overflow",
                "pts {pts} beyond int64 guard {limit}",
                {"pts": self.pts, "limit": MAX_PTS_TICKS},
            )
        return self

    @property
    def seconds(self) -> Fraction:
        return self.time_base.as_fraction * self.pts


class PtsSpan(StrictModel):
    """Half-open ``[start_pts, end_pts)`` interval on one Original time base."""

    start_pts: int = Field(ge=0)
    end_pts: int = Field(ge=0)
    time_base: RationalTimeBase

    @model_validator(mode="after")
    def require_forward_span(self) -> Self:
        if self.end_pts < self.start_pts:
            raise PydanticCustomError(
                "span_inverted",
                "end_pts {end} before start_pts {start}",
                {"end": self.end_pts, "start": self.start_pts},
            )
        return self

    @property
    def length(self) -> int:
        return self.end_pts - self.start_pts

    @property
    def start(self) -> OriginalTimestamp:
        return OriginalTimestamp(pts=self.start_pts, time_base=self.time_base)

    @property
    def end(self) -> OriginalTimestamp:
        return OriginalTimestamp(pts=self.end_pts, time_base=self.time_base)


class SampleSpan(StrictModel):
    """Half-open ``[start_sample, end_sample)`` interval at an integer sample rate."""

    start_sample: int = Field(ge=0)
    end_sample: int = Field(ge=0)
    sample_rate: PositiveInteger

    @model_validator(mode="after")
    def require_forward_span(self) -> Self:
        if self.end_sample < self.start_sample:
            raise PydanticCustomError(
                "span_inverted",
                "end_sample {end} before start_sample {start}",
                {"end": self.end_sample, "start": self.start_sample},
            )
        return self

    @property
    def length(self) -> int:
        return self.end_sample - self.start_sample


def require_monotonic_pts(timestamps: Sequence[OriginalTimestamp]) -> None:
    """Require a non-decreasing PTS sequence, compared exactly in Fraction seconds.

    Timestamps on different time bases are compared by exact rational position,
    never by raw tick count, so a drifted/stale sequence is always detected.
    """

    for index in range(1, len(timestamps)):
        previous = timestamps[index - 1].seconds
        current = timestamps[index].seconds
        if current < previous:
            raise NonMonotonicPtsError(
                f"pts at index {index} ({current}) precedes index {index - 1} ({previous})"
            )
