"""Bounded pre-model candidate-integrity checks for the V44-0 arm pipeline.

Task-2 correction for the measured v44-real-01 failure: sparse summed
coverage (8,005 frames) ended before the authoritative source end (8,467
frames), so 4 of 99 tail speech candidates never reached the Director — and
no seam compared the expected, indexed, and proposed ID sets, so a renamed
or dropped candidate passed silently. Both refusals here are typed
``candidate-integrity-failed`` and carry the sorted missing/extra ID names:

1. ``require_candidate_integrity`` — run immediately after ``build_index``
   and before any paid call (video understanding / Director llm seam): the
   indexed shot IDs over the authoritative half-open ``[0, total_frames)``
   window must EQUAL the expected speech segment IDs exactly, and the
   derivable ``cand-{segment_id}`` candidate IDs must match likewise. Equal
   counts never substitute for set equality: a substituted ID is refused
   with both its missing and its extra name.
2. ``require_proposal_completeness`` — run after Pass B and before
   validation/commit: the proposal's candidate IDs must EQUAL the expected
   candidate IDs; a missing proposal is a refusal, never an implicit drop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.cli.v44_arm_stages import ArmPipelineError
from services.media_query import v2_models as vm

if TYPE_CHECKING:
    from services.cli.v44_arm_stages import ArmPipelineData
    from services.editorial_v2.prompt_v2 import MomentSelectionProposalV2
    from services.media_query.query_v2 import MediaQueryApiV2

#: The candidate-ID convention owned by
#: ``services.editorial_v2.heuristic_planner.candidate_from_shot``.
_CANDIDATE_PREFIX: Final = "cand-"


def expected_candidate_ids(data: ArmPipelineData) -> frozenset[str]:
    """The candidate IDs the Director must deliver: one per speech segment."""
    return frozenset(
        f"{_CANDIDATE_PREFIX}{segment.segment_id}" for segment in data.speech
    )


def _indexed_shot_ids(api: MediaQueryApiV2, total_frames: int) -> frozenset[str]:
    if total_frames < 1:
        raise ArmPipelineError(
            "candidate-integrity-failed",
            f"authoritative total_frames must be >= 1; [0, {total_frames}) "
            "is empty or inverted",
        )
    span = vm.FrameSpan(start_frame=0, end_frame=total_frames)
    pagination = vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=0)
    ids: set[str] = set()
    while True:
        page = api.shots(vm.ShotsRequest(span=span, pagination=pagination))
        ids.update(row.shot_id for row in page.rows)
        if len(ids) >= page.total or not page.rows:
            break
        pagination = vm.V2Pagination(
            limit=pagination.limit, offset=pagination.offset + pagination.limit
        )
    return frozenset(ids)


def require_candidate_integrity(api: MediaQueryApiV2, data: ArmPipelineData) -> None:
    """Exact-set integrity of the freshly built index BEFORE paid calls."""
    expected_shots = frozenset(segment.segment_id for segment in data.speech)
    indexed_shots = _indexed_shot_ids(api, data.total_frames)
    expected_candidates = frozenset(
        f"{_CANDIDATE_PREFIX}{shot_id}" for shot_id in expected_shots
    )
    indexed_candidates = frozenset(
        f"{_CANDIDATE_PREFIX}{shot_id}" for shot_id in indexed_shots
    )
    missing_shots = sorted(expected_shots - indexed_shots)
    extra_shots = sorted(indexed_shots - expected_shots)
    missing_candidates = sorted(expected_candidates - indexed_candidates)
    extra_candidates = sorted(indexed_candidates - expected_candidates)
    if missing_shots or extra_shots or missing_candidates or extra_candidates:
        raise ArmPipelineError(
            "candidate-integrity-failed",
            f"indexed shots over [0, {data.total_frames}) do not exactly match the "
            f"expected speech segments: missing_shot_ids={missing_shots} "
            f"extra_shot_ids={extra_shots} missing_candidate_ids={missing_candidates} "
            f"extra_candidate_ids={extra_candidates} "
            f"(expected {len(expected_candidates)} candidates, indexed "
            f"{len(indexed_candidates)}; equal counts never substitute for "
            "exact-set equality)",
        )


def require_proposal_completeness(
    proposal: MomentSelectionProposalV2, expected: frozenset[str]
) -> None:
    """Exact-set completeness of the Pass B proposal BEFORE commit."""
    proposed = frozenset(str(candidate.candidate_id) for candidate in proposal.candidates)
    missing = sorted(expected - proposed)
    extra = sorted(proposed - expected)
    if missing or extra:
        raise ArmPipelineError(
            "candidate-integrity-failed",
            "proposal candidate IDs differ from the discovered candidate set: "
            f"missing_candidate_ids={missing} extra_candidate_ids={extra} "
            f"(expected {len(expected)}, proposed {len(proposed)}); a missing "
            "proposal is a refusal, never an implicit drop",
        )


__all__ = [
    "expected_candidate_ids",
    "require_candidate_integrity",
    "require_proposal_completeness",
]
