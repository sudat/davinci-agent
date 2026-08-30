"""T6 budgeted specialist-target selection: hard reservation first.

Collects GLM specialist-target candidates from the existing deterministic
progressive trigger windows PLUS the episode reduce's own validated typed
requests, splits them via one explicit typed mapping grounded in the current
trigger vocabulary, and selects under the targeted 25% deep-review budget.

Hard obligations — ``human_request`` (an operator demanded the review),
``editorial_qc_flag`` (a quality flag demands verification), and
``recall_audit_sample`` (the honest-recall audit) — are GLOBALLY RESERVED
FIRST: every hard candidate is selected before any advisory window is
considered, so an early advisory window can never starve a later hard one;
a hard-only union that busts the budget is a typed block, never a silent
drop. Advisory targets then join greedily in deterministic bounds order
while the unique merged union fits; the remainder is deferred (a budget
fact, not a failure). The final selection is validated through the EXISTING
``build_analysis_budget`` targeted path so overlap can never inflate or
hide the coverage truth.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BeforeValidator, Field

from services.contracts.primitives import StrictModel
from services.media_intelligence.budget import (
    AnalysisBudgetV1,
    BudgetRequest,
    DeepReviewWindow,
    PolicyLimits,
    TriggerReason,
    UniversalPassRecord,
    build_analysis_budget,
    merge_window_spans,
)
from services.media_intelligence.video_stage_wire import (
    SpecialistRequestSpan,  # noqa: TC001 (pydantic field)
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.contracts.primitives import Frame, RationalFrameRate

#: The explicit hard/advisory mapping grounded in the current trigger
#: vocabulary. HARD targets are obligations whose omission would lie about
#: coverage: an operator's request, a quality-flag verification, and the
#: honest-recall audit. Machine uncertainty triggers and the reduce's own
#: requests stay advisory — their failure may continue with recorded
#: unresolved uncertainty.
HARD_TRIGGER_REASONS: Final[frozenset[TriggerReason]] = frozenset(
    {"human_request", "editorial_qc_flag", "recall_audit_sample"}
)

#: Trigger identity for a reduce-derived specialist window (provider output).
GEMINI_REQUEST_SOURCE: Final = "gemini:global_reduce"


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class SpecialistSelectionError(ValueError):
    """Specialist-target selection failure."""

    LABEL = "specialist_selection_error"


class TargetSelection(StrictModel):
    """Deterministic specialist-target selection under the targeted budget.

    ``reduce_spans`` is the smallest typed companion metadata: the validated
    reduce requests (rationale/uncertainty intact) whose bounds were SELECTED,
    so the GLM document can cite why Gemini requested that target.
    """

    selected: Annotated[tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    deferred: Annotated[tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    targeted_budget: AnalysisBudgetV1
    reduce_spans: Annotated[
        tuple[SpecialistRequestSpan, ...], BeforeValidator(_to_tuple)
    ] = Field(default_factory=tuple)


def _rank(window: DeepReviewWindow) -> tuple[int, str, str]:
    """Hard windows outrank advisory ones at identical bounds; then sort."""

    hard = 0 if window.trigger_reason in HARD_TRIGGER_REASONS else 1
    return (hard, window.trigger_reason, str(window.trigger_source))


def _merged_frames(windows: tuple[DeepReviewWindow, ...]) -> int:
    return sum(end - start for start, end in merge_window_spans(windows))


def reduce_request_windows(
    source_duration_frames: Frame, requests: Sequence[SpecialistRequestSpan]
) -> tuple[DeepReviewWindow, ...]:
    """Validate + wrap the reduce's own typed specialist requests.

    Raises :class:`SpecialistSelectionError` for a request that is not a
    forward interval inside the source; duplicates collapse by bounds
    deterministically (last-writer-wins is impossible: bounds are the key).
    """

    total = int(source_duration_frames)
    merged: dict[tuple[int, int], DeepReviewWindow] = {}
    for request in requests:
        start, end = int(request.start_frame), int(request.end_frame)
        if end <= start or start < 0 or end > total:
            raise SpecialistSelectionError(
                f"reduce-requested specialist window [{start}, {end}) must be a forward "
                f"interval inside the {total}-frame source"
            )
        merged[(start, end)] = DeepReviewWindow(
            start_frame=start,
            end_frame=end,
            trigger_reason="specialist_target",
            trigger_source=GEMINI_REQUEST_SOURCE,
        )
    return tuple(merged[key] for key in sorted(merged))


def _merged_candidates(
    progressive: tuple[DeepReviewWindow, ...],
    reduce_windows: tuple[DeepReviewWindow, ...],
) -> tuple[DeepReviewWindow, ...]:
    """One window per exact bounds — hard wins duplicates deterministically."""

    merged: dict[tuple[int, int], DeepReviewWindow] = {}
    for window in (*progressive, *reduce_windows):
        key = (int(window.start_frame), int(window.end_frame))
        current = merged.get(key)
        if current is None or _rank(window) < _rank(current):
            merged[key] = window
    return tuple(merged[key] for key in sorted(merged))


def select_specialist_targets(
    source_duration_frames: Frame,
    progressive_windows: Sequence[DeepReviewWindow],
    reduce_requests: Sequence[SpecialistRequestSpan],
    *,
    frame_rate: RationalFrameRate,
    policy_limits: PolicyLimits | None = None,
) -> TargetSelection:
    """Reserve every hard target first, then admit advisory targets greedily.

    Raises :class:`SpecialistSelectionError` for an invalid reduce request
    and for a hard-only union that exceeds the coverage limit (a policy
    conflict that must block, not silently drop obligations).
    """

    total = int(source_duration_frames)
    limits = policy_limits or PolicyLimits()
    limit = Fraction(str(limits.deep_review_coverage_max))
    candidates = _merged_candidates(
        tuple(progressive_windows),
        reduce_request_windows(source_duration_frames, reduce_requests),
    )

    hard = tuple(w for w in candidates if w.trigger_reason in HARD_TRIGGER_REASONS)
    advisory = tuple(w for w in candidates if w.trigger_reason not in HARD_TRIGGER_REASONS)
    hard_frames = _merged_frames(hard)
    if Fraction(hard_frames, total) > limit:
        raise SpecialistSelectionError(
            f"hard specialist obligations alone cover {hard_frames} of {total} frames, "
            f"over the {float(limit):.2f} targeted budget — a policy conflict that "
            "blocks rather than drops obligations"
        )

    selected: list[DeepReviewWindow] = list(hard)
    deferred: list[DeepReviewWindow] = []
    for window in advisory:
        tentative = (*selected, window)
        if Fraction(_merged_frames(tentative), total) > limit:
            deferred.append(window)
            continue
        selected.append(window)

    targeted_budget = build_analysis_budget(
        BudgetRequest(
            source_duration_frames=source_duration_frames,
            frame_rate=frame_rate,
            universal_pass=UniversalPassRecord(wall_clock_seconds=0.0, shots_count=0),
            windows=tuple(selected),
            policy_limits=limits,
            budget_kind="targeted_deep_review",
        )
    )
    selected_bounds = {(int(w.start_frame), int(w.end_frame)) for w in selected}
    reduce_spans = tuple(
        sorted(
            (r for r in reduce_requests
             if (int(r.start_frame), int(r.end_frame)) in selected_bounds),
            key=lambda r: (int(r.start_frame), int(r.end_frame)),
        )
    )
    return TargetSelection(
        selected=tuple(selected),
        deferred=tuple(deferred),
        targeted_budget=targeted_budget,
        reduce_spans=reduce_spans,
    )


__all__ = [
    "GEMINI_REQUEST_SOURCE",
    "HARD_TRIGGER_REASONS",
    "SpecialistSelectionError",
    "TargetSelection",
    "reduce_request_windows",
    "select_specialist_targets",
]
