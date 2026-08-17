"""The four minimum visual checks over the per-frame luma facts.

All results are half-open frame spans with frozen confidence permille and
full rule provenance bound to the decode evidence. Frozen classification
precedence: a frame is claimed by at most one per-frame check — black
first, then out-of-bounds exposure, then blur (flat black/clipped frames
cannot evidence blur). Scene change is a pairwise diff metric and is
independent of that ordering.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from services.analyze.visual_constants import (
    ANALYZER_VERSION,
    BLACK_RULE_ID,
    BLUR_MAX_LAPLACIAN_MEAN_SQUARE,
    BLUR_RULE_ID,
    CONFIDENCE_BLACK_SPAN,
    CONFIDENCE_BLUR_SPAN,
    CONFIDENCE_EXPOSURE_SPAN,
    CONFIDENCE_SCENE_CHANGE,
    EXPOSURE_RULE_ID,
    SCENE_MIN_MEAN_DIFF_M,
    SCENE_RULE_ID,
)
from services.analyze.visual_metrics import (
    DIRECTIONS,
    Direction,
    compute_frame_facts,
    exposure_direction,
    is_black,
)
from services.analyze.visual_models import (
    DecodeBinding,
    FrameFact,
    decode_binding_hash,
)
from services.analyze.visual_results import (
    BlackSpanResult,
    BlurSpanResult,
    CheckProvenance,
    ExposureSpanResult,
    FrameSpan,
    SceneChangeResult,
)

__all__ = [
    "compute_frame_facts",
    "detect_black_spans",
    "detect_blur_spans",
    "detect_exposure_spans",
    "detect_scene_changes",
]


def _provenance(
    binding: DecodeBinding, rule_id: str, input_hashes: tuple[str, ...]
) -> CheckProvenance:
    return CheckProvenance(
        analyzer_version=ANALYZER_VERSION,
        rule_id=rule_id,
        decode_binding_sha256=decode_binding_hash(binding),
        input_artifact_hashes=input_hashes,
    )


def _runs(
    facts: Sequence[FrameFact], predicate: Callable[[FrameFact], bool]
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for fact in facts:
        if predicate(fact):
            if start is None:
                start = fact.frame_index
        elif start is not None:
            spans.append((start, fact.frame_index))
            start = None
    if start is not None:
        spans.append((start, len(facts)))
    return spans


def detect_scene_changes(
    facts: Sequence[FrameFact],
    binding: DecodeBinding,
    input_hashes: tuple[str, ...],
) -> tuple[SceneChangeResult, ...]:
    provenance = _provenance(binding, SCENE_RULE_ID, input_hashes)
    return tuple(
        SceneChangeResult(
            boundary_frame=fact.frame_index,
            span=FrameSpan(start_frame=fact.frame_index, end_frame=fact.frame_index + 1),
            mean_diff_m=fact.diff_prev_m,
            confidence=CONFIDENCE_SCENE_CHANGE,
            provenance=provenance,
            decode_frame_count=binding.facts.frame_count,
        )
        for fact in facts
        if fact.frame_index > 0 and fact.diff_prev_m >= SCENE_MIN_MEAN_DIFF_M
    )


def detect_black_spans(
    facts: Sequence[FrameFact],
    binding: DecodeBinding,
    input_hashes: tuple[str, ...],
) -> tuple[BlackSpanResult, ...]:
    provenance = _provenance(binding, BLACK_RULE_ID, input_hashes)
    results: list[BlackSpanResult] = []
    for start, end in _runs(facts, is_black):
        covered = facts[start:end]
        results.append(
            BlackSpanResult(
                span=FrameSpan(start_frame=start, end_frame=end),
                span_mean_luma_m=sum(fact.mean_luma_m for fact in covered) // len(covered),
                min_black_pixel_fraction_m=min(fact.black_pixel_fraction_m for fact in covered),
                confidence=CONFIDENCE_BLACK_SPAN,
                provenance=provenance,
                decode_frame_count=binding.facts.frame_count,
            )
        )
    return tuple(results)


def detect_blur_spans(
    facts: Sequence[FrameFact],
    binding: DecodeBinding,
    input_hashes: tuple[str, ...],
) -> tuple[BlurSpanResult, ...]:
    provenance = _provenance(binding, BLUR_RULE_ID, input_hashes)

    def blur_evaluable(fact: FrameFact) -> bool:
        if is_black(fact) or exposure_direction(fact) is not None:
            return False
        return fact.laplacian_mean_square <= BLUR_MAX_LAPLACIAN_MEAN_SQUARE

    results: list[BlurSpanResult] = []
    for start, end in _runs(facts, blur_evaluable):
        covered = facts[start:end]
        results.append(
            BlurSpanResult(
                span=FrameSpan(start_frame=start, end_frame=end),
                max_laplacian_mean_square=max(fact.laplacian_mean_square for fact in covered),
                confidence=CONFIDENCE_BLUR_SPAN,
                provenance=provenance,
                decode_frame_count=binding.facts.frame_count,
            )
        )
    return tuple(results)


def detect_exposure_spans(
    facts: Sequence[FrameFact],
    binding: DecodeBinding,
    input_hashes: tuple[str, ...],
) -> tuple[ExposureSpanResult, ...]:
    provenance = _provenance(binding, EXPOSURE_RULE_ID, input_hashes)
    results: list[ExposureSpanResult] = []
    for direction in DIRECTIONS:

        def matches(fact: FrameFact, wanted: Direction = direction) -> bool:
            return exposure_direction(fact) == wanted

        for start, end in _runs(facts, matches):
            covered = facts[start:end]
            results.append(
                ExposureSpanResult(
                    direction=direction,
                    span=FrameSpan(start_frame=start, end_frame=end),
                    span_mean_luma_m=sum(fact.mean_luma_m for fact in covered) // len(covered),
                    max_clipped_high_fraction_m=max(
                        fact.clipped_high_fraction_m for fact in covered
                    ),
                    confidence=CONFIDENCE_EXPOSURE_SPAN,
                    provenance=provenance,
                    decode_frame_count=binding.facts.frame_count,
                )
            )
    return tuple(results)
