"""Deterministic full-source lead-map window generation (v44 T5).

Lead-map windows tile the ENTIRE Edit Source with overlapping half-open
``[start, end)`` windows so the lead reviewer sees every frame.  All bounds
are integer Edit Source frames — no seconds-derived approximation.  The
defaults are the approved bounds for the ``30000/1001`` Edit Source rate
(3600-frame maximum and 300-frame overlap, snap radius 150 frames);
the frozen ``v44-real-01`` evidence has ``total_frames == 8467``, yielding
baseline windows ``[0,3600)``, ``[3300,6900)``, ``[6600,8467)``.

Internal cuts (the start edge of every window after the first) may snap to
an existing speech-segment boundary within the snap radius; frame 0 and the
final end (``total_frames``) are never snapped.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, StrictModel
from services.media_intelligence.budget import DeepReviewWindow

LEAD_MAP_MAX_WINDOW_FRAMES: Final = 3600
LEAD_MAP_OVERLAP_FRAMES: Final = 300
LEAD_MAP_SNAP_RADIUS_FRAMES: Final = 150
LEAD_MAP_TRIGGER_SOURCE: Final = "policy:lead_map"


class LeadMapWindowError(ValueError):
    """Lead-map window generation failure."""

    LABEL = "lead_map_window_error"


class LeadMapWindowPolicy(StrictModel):
    """Window bounds in exact Edit Source frames.

    Invariants: ``overlap_frames < max_window_frames``,
    ``snap_radius_frames <= overlap_frames``, and
    ``overlap_frames + snap_radius_frames < max_window_frames``.  The last
    is the strict-progress guarantee: the smallest possible snapped next
    start is ``start + max_window_frames - overlap_frames -
    snap_radius_frames``, which must exceed ``start``, so generation always
    moves forward and terminates.  Together the invariants also keep every
    snapped cut inside the previous window's end, so full coverage
    survives every snap.
    """

    max_window_frames: int = Field(default=LEAD_MAP_MAX_WINDOW_FRAMES, ge=1)
    overlap_frames: int = Field(default=LEAD_MAP_OVERLAP_FRAMES, ge=0)
    snap_radius_frames: int = Field(default=LEAD_MAP_SNAP_RADIUS_FRAMES, ge=0)

    @model_validator(mode="after")
    def require_sound_bounds(self) -> LeadMapWindowPolicy:
        if self.overlap_frames >= self.max_window_frames:
            raise PydanticCustomError(
                "overlap_not_smaller",
                "overlap_frames must be < max_window_frames",
            )
        if self.snap_radius_frames > self.overlap_frames:
            raise PydanticCustomError(
                "snap_beyond_overlap",
                "snap_radius_frames must be <= overlap_frames",
            )
        if self.overlap_frames + self.snap_radius_frames >= self.max_window_frames:
            raise PydanticCustomError(
                "no_strict_progress",
                "overlap_frames + snap_radius_frames must be < max_window_frames",
            )
        return self


_DEFAULT_WINDOW_POLICY: Final[LeadMapWindowPolicy] = LeadMapWindowPolicy()


def _nearest_boundary(cut: int, boundaries: tuple[int, ...], radius: int) -> int:
    """Nearest boundary within ``radius`` of ``cut``; ties take the earlier frame."""

    best: int | None = None
    for boundary in boundaries:
        if best is None or abs(boundary - cut) < abs(best - cut):
            best = boundary
    if best is not None and abs(best - cut) <= radius:
        return best
    return cut


def generate_lead_map_windows(
    total_frames: Frame,
    *,
    speech_boundaries: tuple[Frame, ...] = (),
    policy: LeadMapWindowPolicy = _DEFAULT_WINDOW_POLICY,
) -> tuple[DeepReviewWindow, ...]:
    """Generate the deterministic full-source lead-map windows.

    Raises ``LeadMapWindowError`` for a non-positive ``total_frames``.
    Feed the result to ``build_analysis_budget`` with
    ``budget_kind="lead_map"`` to enforce exact full coverage.
    """

    total = int(total_frames)
    if total <= 0:
        raise LeadMapWindowError("total_frames must be > 0 for lead-map windows")
    boundaries = tuple(sorted({int(b) for b in speech_boundaries if 0 < int(b) < total}))

    windows: list[DeepReviewWindow] = []
    start = 0
    while True:
        nominal_end = start + policy.max_window_frames
        end = min(nominal_end, total)
        windows.append(
            DeepReviewWindow(
                start_frame=start,
                end_frame=end,
                trigger_reason="lead_map_window",
                trigger_source=LEAD_MAP_TRIGGER_SOURCE,
            )
        )
        if end >= total:
            return tuple(windows)
        cut = nominal_end - policy.overlap_frames
        start = _nearest_boundary(cut, boundaries, policy.snap_radius_frames)
