# allow: SIZE_OK — plan task 19 pins the deliverable to this single module
# (benchmark models + W3 chain driver + reanalysis + aggregation), mirroring
# the moment_review.py SIZE_OK precedent where the model surface dominates.
"""TTFRP/cost benchmark harness over the W3 media-intelligence chain.

``run_chain`` drives one canonical ``MediaIntelligenceArtifact`` through the
full Wave-3 pipeline — reconcile (T14, with deterministic synthetic local
evidence) → v2 DuckDB index (T15) → progressive plan+budget (T16) → moment
deep review via the synthetic providers (T17) → recall audit (T18) — and
``run_benchmark`` additionally measures stage wall clocks (injectable
monotonic) and the re-analysis cost of a one-shot correction: reviews whose
review window does not overlap the mutated shot are REUSED (cache hits,
PRD SLO "reprocess only affected regions"), overlapping windows recompute.

Determinism contract: every counter (frames, windows, cache hits, tokens)
is a pure function of ``(artifact, policy)`` — byte-identical canonical
output across runs under an injected monotonic.  Wall clocks come from the
monotonic seam only.  These are SYNTHETIC mechanism-verification numbers:
real 30/90/180-minute TTFRP measurement happens at tasks 30/43/52 (see
``aggregate_runs`` notes).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import canonical_model_bytes
from services.media_intelligence.models import (
    AudioMeasurements,
    MediaIntelligenceArtifact,
    SilenceSegment,
)
from services.media_intelligence.moment_review import (
    MomentDeepReviewV1,
    NeighboringContext,
    ReviewExecutionContext,
    ReviewLineage,
    ReviewProviders,
    ReviewWindow,
    SyntheticAudioContext,
    SyntheticFrameExtractor,
    SyntheticTranscriptLookup,
    TranscriptRef,
    execute_review,
)
from services.media_intelligence.progressive import (
    ProgressivePlan,
    ProgressivePolicy,
    plan_progressive_analysis,
)
from services.media_intelligence.recall_audit import (
    AuditReviewResult,
    RecallAuditPolicy,
    RecallAuditReportV1,
    RecallAuditSample,
    draw_audit_sample,
    evaluate_recall,
)
from services.media_intelligence.reconcile import (
    ConformContext,
    LocalEvidenceBatch,
    LocalSourceEvidence,
    reconcile,
)
from services.media_query.index_v2 import build_index

Monotonic = Callable[[], float]
_SYNTHETIC_LINEAGE: Final = ReviewLineage(
    provider="synthetic", provider_version="1.0.0", tool="dense-frame-sampler", cost=0.0
)
_REAL_DURATION_NOTE: Final = (
    "synthetic fixtures verify the mechanism only; real 30/90/180-minute TTFRP "
    "and cost measurement happens at tasks 30/43/52"
)


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


class BenchmarkPolicy(StrictModel):
    """Benchmark knobs: the progressive routing policy + audit sampling."""

    progressive: ProgressivePolicy
    recall_audit: RecallAuditPolicy = Field(default_factory=RecallAuditPolicy)


# ---------------------------------------------------------------------------
# BenchmarkRunV1 artifact
# ---------------------------------------------------------------------------


class UniversalStage(StrictModel):
    """Stage 1 — reconcile + index over ALL material (universal pass)."""

    wall_clock_seconds: float = Field(ge=0)
    shots_indexed: int = Field(ge=0)
    boundary_conflicts: int = Field(ge=0)


class TriageStage(StrictModel):
    """Stage 2 — coarse scoring + window planning (plan time)."""

    wall_clock_seconds: float = Field(ge=0)
    triaged_shots: int = Field(ge=0)
    windows_planned: int = Field(ge=0)


class DeepReviewStage(StrictModel):
    """Stage 3 — synthetic deep reviews over the unique window spans."""

    wall_clock_seconds: float = Field(ge=0)
    reviews_executed: int = Field(ge=0)
    frames_reviewed: int = Field(ge=0)
    frame_samples: int = Field(ge=0)


class BenchmarkStages(StrictModel):
    universal: UniversalStage
    triage: TriageStage
    deep_review: DeepReviewStage


class RecallSummary(StrictModel):
    sampled: int = Field(ge=0)
    reviewed: int = Field(ge=0)
    miss_rate: float = Field(ge=0, le=1)
    signal: Literal["ok", "degraded"]


class ReanalysisRun(StrictModel):
    """Second run after one shot correction — dependent-only or full."""

    mutated_shot_id: str
    mode: Literal["dependent_only", "full_recompute"]
    recomputed_windows: int = Field(ge=0)
    reused_reviews: int = Field(ge=0)
    recomputed_frames: int = Field(ge=0)
    wall_clock_seconds: float = Field(ge=0)


class CostCounters(StrictModel):
    """Token/frame cost counters — deterministic, zero tokens by design."""

    tokens: int = Field(default=0, ge=0)
    frames_deep_reviewed: int = Field(ge=0)
    frame_samples: int = Field(ge=0)
    cost_estimate: float = Field(default=0.0, ge=0)


class BenchmarkRunV1(StrictModel):
    """One benchmark run over one fixture (``benchmark-run-v1``)."""

    schema_version: Literal["benchmark-run-v1"] = "benchmark-run-v1"
    fixture_id: str
    episode_id: Identifier
    source_duration_seconds: float = Field(ge=0)
    deep_review_ratio: float = Field(ge=0, le=1)
    stages: BenchmarkStages
    cache_hits: int = Field(ge=0)
    token_or_frame_counters: CostCounters
    recall_audit: RecallSummary
    reanalysis_after_correction: ReanalysisRun
    ttfrp_wall_clock_seconds: float = Field(ge=0)
    plan_unique_spans: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    notes: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(default_factory=tuple)


# ---------------------------------------------------------------------------
# W3 chain driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainResult:
    """Deterministic outputs of one full W3 chain run."""

    reconciled: MediaIntelligenceArtifact
    plan: ProgressivePlan
    reviews: tuple[MomentDeepReviewV1, ...]
    recall_sample: RecallAuditSample
    recall_report: RecallAuditReportV1
    index_path: Path
    boundary_conflicts: int


def _synthetic_local_evidence(
    artifact: MediaIntelligenceArtifact,
) -> LocalEvidenceBatch:
    """Deterministic local analyzer facts: boundaries aligned to shot starts
    and one silence tail in the last shot (no blur/quality spans — a quality
    flag would add an editorial_qc_flag window on the whole first shot and
    break the fixtures' ≤25% coverage arithmetic)."""
    shots = sorted(artifact.shots, key=lambda s: int(s.source_span.start_frame))
    boundaries = tuple(int(s.source_span.start_frame) for s in shots[1:])
    last = shots[-1].source_span
    return LocalEvidenceBatch(
        sources=(
            LocalSourceEvidence(
                source_id=artifact.sources[0].source_id,
                scene_boundaries=boundaries,
                silence_segments=(
                    SilenceSegment(
                        start_frame=max(int(last.start_frame), int(last.end_frame) - 90),
                        end_frame=int(last.end_frame),
                        kind="tail",
                    ),
                ),
                audio_measurements=AudioMeasurements(
                    loudness_db=-14.5, energy=0.4, ambient_type="room_tone"
                ),
            ),
        )
    )


def _conform_context(artifact: MediaIntelligenceArtifact) -> ConformContext:
    return ConformContext(source_ids=tuple(str(src.source_id) for src in artifact.sources))


def _unique_spans(plan: ProgressivePlan) -> tuple[tuple[int, int], ...]:
    """Execution dedupe: one deep review per unique window span (review ids
    are window-derived, so duplicate spans would collide in the index)."""
    return tuple(sorted({(int(w.start_frame), int(w.end_frame)) for w in plan.deep_review_windows}))


def _owning_shot_index(ordered: Sequence[tuple[int, int, str]], span: tuple[int, int]) -> int:
    """Index of the shot a window belongs to: exact span match first, then
    containment (human-request windows may sit inside a longer shot)."""
    for index, (start, end, _shot_id) in enumerate(ordered):
        if (start, end) == span:
            return index
    for index, (start, end, _shot_id) in enumerate(ordered):
        if start <= span[0] and span[1] <= end:
            return index
    raise ValueError(f"no shot matches window span {span}")


def _execute_reviews(
    reconciled: MediaIntelligenceArtifact,
    spans: Sequence[tuple[int, int]],
    duration: int,
) -> tuple[MomentDeepReviewV1, ...]:
    ordered = sorted(
        (
            (int(s.source_span.start_frame), int(s.source_span.end_frame), str(s.shot_id))
            for s in reconciled.shots
        ),
    )
    ids = [shot_id for _start, _end, shot_id in ordered]
    transcripts = tuple(
        TranscriptRef(
            segment_id=str(seg.segment_id),
            start_frame=int(seg.start_frame),
            end_frame=int(seg.end_frame),
        )
        for shot in reconciled.shots
        for seg in shot.transcript_segments or ()
    )
    providers = ReviewProviders(
        frames=SyntheticFrameExtractor(density=4),
        transcripts=SyntheticTranscriptLookup(segments=transcripts),
        audio=SyntheticAudioContext(),
        lineage=_SYNTHETIC_LINEAGE,
    )
    reviews = []
    for start, end in spans:
        owning = _owning_shot_index(ordered, (start, end))
        prev_id = ids[owning - 1] if owning > 0 else None
        next_id = ids[owning + 1] if owning + 1 < len(ids) else None
        reviews.append(
            execute_review(
                ReviewWindow(start_frame=start, end_frame=end),
                providers,
                context=ReviewExecutionContext(
                    episode_id=reconciled.episode_id,
                    source_duration_frames=duration,
                    known_shot_ids=frozenset(ids),
                    neighbors=NeighboringContext(prev_shot_id=prev_id, next_shot_id=next_id),
                ),
            )
        )
    return tuple(reviews)


def _shot_id_for_span(reconciled: MediaIntelligenceArtifact, span: tuple[int, int]) -> str:
    for shot in reconciled.shots:
        if (
            int(shot.source_span.start_frame) == span[0]
            and int(shot.source_span.end_frame) == span[1]
        ):
            return str(shot.shot_id)
    raise ValueError(f"no shot matches window span {span}")


def _recall(
    reconciled: MediaIntelligenceArtifact,
    plan: ProgressivePlan,
    reviews: Sequence[MomentDeepReviewV1],
    policy: RecallAuditPolicy,
) -> tuple[RecallAuditSample, RecallAuditReportV1]:
    sample = draw_audit_sample(plan.triage, policy=policy)
    sampled_ids = {str(shot.shot_id) for shot in sample.sampled}
    missed: dict[str, bool] = {}
    for review in reviews:
        span = (int(review.source_window.start_frame), int(review.source_window.end_frame))
        shot_id = _shot_id_for_span(reconciled, span)
        if shot_id in sampled_ids:
            result = AuditReviewResult.from_moment_review(shot_id, review.assessment)
            missed[shot_id] = missed.get(shot_id, False) or result.missed_value
    report = evaluate_recall(
        [
            AuditReviewResult(shot_id=shot_id, missed_value=value)
            for shot_id, value in sorted(missed.items())
        ],
        sample=sample,
        policy=policy,
    )
    return sample, report


def _duration_frames(artifact: MediaIntelligenceArtifact) -> int:
    return max(
        [int(src.duration_frames or 0) for src in artifact.sources]
        + [int(s.source_span.end_frame) for s in artifact.shots]
    )


def _boundary_conflicts(reconciled: MediaIntelligenceArtifact) -> int:
    return sum(
        1
        for shot in reconciled.shots
        for record in shot.provenance or ()
        if record.field == "boundary_conflict"
    )


def run_chain(
    artifact: MediaIntelligenceArtifact,
    *,
    policy: BenchmarkPolicy,
    index_path: Path,
) -> ChainResult:
    """Drive the full W3 chain deterministically; no clocks, no randomness.

    The DuckDB index is REBUILDABLE evidence only — the canonical artifact
    bytes remain the single authority (Gate V43-1 criterion c).
    """
    reconciled = reconcile(
        _synthetic_local_evidence(artifact), artifact, _conform_context(artifact)
    )
    plan = plan_progressive_analysis(reconciled, policy=policy.progressive)
    duration = _duration_frames(reconciled)
    spans = _unique_spans(plan)
    reviews = _execute_reviews(reconciled, spans, duration)
    build_index(reconciled, index_path, reviews=reviews)
    sample, report = _recall(reconciled, plan, reviews, policy.recall_audit)
    return ChainResult(
        reconciled=reconciled,
        plan=plan,
        reviews=reviews,
        recall_sample=sample,
        recall_report=report,
        index_path=index_path,
        boundary_conflicts=_boundary_conflicts(reconciled),
    )


# ---------------------------------------------------------------------------
# run_benchmark
# ---------------------------------------------------------------------------


def _union_frames(spans: Sequence[tuple[int, int]]) -> int:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def _overlaps(span: tuple[int, int], other: tuple[int, int]) -> bool:
    return span[0] < other[1] and other[0] < span[1]


def _mutate_one_shot(
    artifact: MediaIntelligenceArtifact,
    reconciled: MediaIntelligenceArtifact,
    plan: ProgressivePlan,
) -> tuple[MediaIntelligenceArtifact, tuple[int, int]]:
    """Deterministic correction target: the shot triggering the most deep
    review windows (tie → lowest span start, then smallest shot id).

    ``plan`` trigger sources carry the RECONCILED (hash) shot ids; the
    mutation applies to the fixture artifact's own shots — the half-open
    span is the join key between the two id domains.
    """
    counts: dict[str, int] = {}
    spans_by_id: dict[str, tuple[int, int]] = {}
    for window in plan.deep_review_windows:
        source = str(window.trigger_source)
        counts[source] = counts.get(source, 0) + 1
    for shot in reconciled.shots:
        spans_by_id[str(shot.shot_id)] = (
            int(shot.source_span.start_frame),
            int(shot.source_span.end_frame),
        )
    target = min(counts, key=lambda sid: (-counts[sid], spans_by_id[sid][0], sid))
    target_span = spans_by_id[target]
    mutated = tuple(
        shot.model_copy(update={"description": f"{shot.description} (corrected)"})
        if (
            int(shot.source_span.start_frame),
            int(shot.source_span.end_frame),
        )
        == target_span
        else shot
        for shot in artifact.shots
    )
    return artifact.model_copy(update={"shots": mutated}), target_span


def run_benchmark(
    artifact: MediaIntelligenceArtifact,
    *,
    policy: BenchmarkPolicy,
    fixture_id: str | None = None,
    index_path: Path | None = None,
    monotonic: Monotonic = time.monotonic,
) -> BenchmarkRunV1:
    """Benchmark one fixture: stage wall clocks + deterministic counters.

    ``index_path`` defaults to a throwaway temp file (the index is never
    canonical); ``monotonic`` is injectable for deterministic wall clocks.
    """
    tmp: TemporaryDirectory[str] | None = None
    if index_path is None:
        tmp = TemporaryDirectory()
        index_path = Path(tmp.name) / "index.duckdb"
    try:
        return _run_benchmark_measured(
            artifact,
            policy=policy,
            fixture_id=fixture_id,
            index_path=index_path,
            monotonic=monotonic,
        )
    finally:
        if tmp is not None:
            tmp.cleanup()


def _run_benchmark_measured(
    artifact: MediaIntelligenceArtifact,
    *,
    policy: BenchmarkPolicy,
    fixture_id: str | None,
    index_path: Path,
    monotonic: Monotonic,
) -> BenchmarkRunV1:
    t0 = monotonic()
    reconciled = reconcile(
        _synthetic_local_evidence(artifact), artifact, _conform_context(artifact)
    )
    build_index(reconciled, index_path)
    t1 = monotonic()
    plan = plan_progressive_analysis(reconciled, policy=policy.progressive)
    t2 = monotonic()
    duration = _duration_frames(reconciled)
    spans = _unique_spans(plan)
    reviews = _execute_reviews(reconciled, spans, duration)
    build_index(reconciled, index_path, reviews=reviews)
    t3 = monotonic()
    _sample, report = _recall(reconciled, plan, reviews, policy.recall_audit)

    mutated, target_span = _mutate_one_shot(artifact, reconciled, plan)
    reconciled2 = reconcile(_synthetic_local_evidence(mutated), mutated, _conform_context(mutated))
    plan2 = plan_progressive_analysis(reconciled2, policy=policy.progressive)
    known = set(spans)
    spans2 = _unique_spans(plan2)
    recompute = [sp for sp in spans2 if _overlaps(sp, target_span) or sp not in known]
    recompute_set = set(recompute)
    reused = len([sp for sp in spans2 if sp not in recompute_set])
    t4 = monotonic()
    _execute_reviews(reconciled2, recompute, duration)
    t5 = monotonic()
    mode: Literal["dependent_only", "full_recompute"] = (
        "dependent_only" if reused > 0 and len(recompute) < len(spans2) else "full_recompute"
    )

    frames_reviewed = _union_frames(spans)
    samples = sum(len(review.evidence.frame_bundle) for review in reviews)
    rate = policy.progressive.frame_rate.as_fraction
    return BenchmarkRunV1(
        fixture_id=fixture_id or str(artifact.episode_id),
        episode_id=artifact.episode_id,
        source_duration_seconds=float(duration / rate),
        deep_review_ratio=frames_reviewed / duration,
        stages=BenchmarkStages(
            universal=UniversalStage(
                wall_clock_seconds=t1 - t0,
                shots_indexed=len(reconciled.shots),
                boundary_conflicts=_boundary_conflicts(reconciled),
            ),
            triage=TriageStage(
                wall_clock_seconds=t2 - t1,
                triaged_shots=len(plan.triage),
                windows_planned=len(plan.deep_review_windows),
            ),
            deep_review=DeepReviewStage(
                wall_clock_seconds=t3 - t2,
                reviews_executed=len(reviews),
                frames_reviewed=frames_reviewed,
                frame_samples=samples,
            ),
        ),
        cache_hits=reused,
        token_or_frame_counters=CostCounters(
            tokens=0, frames_deep_reviewed=frames_reviewed, frame_samples=samples
        ),
        recall_audit=RecallSummary(
            sampled=report.sampled_count,
            reviewed=report.reviewed_count,
            miss_rate=report.miss_rate,
            signal=report.degradation_signal,
        ),
        reanalysis_after_correction=ReanalysisRun(
            mutated_shot_id=_shot_id_for_span(reconciled, target_span),
            mode=mode,
            recomputed_windows=len(recompute),
            reused_reviews=reused,
            recomputed_frames=_union_frames(recompute),
            wall_clock_seconds=t5 - t4,
        ),
        ttfrp_wall_clock_seconds=t3 - t0,
        plan_unique_spans=tuple(f"{start}-{end}" for start, end in spans),
        notes=(
            _REAL_DURATION_NOTE,
            "tokens are zero: synthetic providers burn no tokens",
        ),
    )


# ---------------------------------------------------------------------------
# Aggregation (evidence reports)
# ---------------------------------------------------------------------------


def deterministic_summary(runs: Sequence[BenchmarkRunV1]) -> dict[str, object]:
    """Clock-free per-fixture summary (drift-guarded by the gate test)."""
    return {
        run.fixture_id: {
            "episode_id": str(run.episode_id),
            "source_duration_seconds": run.source_duration_seconds,
            "deep_review_ratio": run.deep_review_ratio,
            "shots": run.stages.universal.shots_indexed,
            "windows_planned": run.stages.triage.windows_planned,
            "reviews": run.stages.deep_review.reviews_executed,
            "frames_deep_reviewed": run.token_or_frame_counters.frames_deep_reviewed,
            "frame_samples": run.token_or_frame_counters.frame_samples,
            "tokens": run.token_or_frame_counters.tokens,
            "cache_hits": run.cache_hits,
            "reanalysis_mode": run.reanalysis_after_correction.mode,
            "recomputed_windows": run.reanalysis_after_correction.recomputed_windows,
            "recall": {
                "sampled": run.recall_audit.sampled,
                "reviewed": run.recall_audit.reviewed,
                "miss_rate": run.recall_audit.miss_rate,
                "signal": run.recall_audit.signal,
            },
        }
        for run in sorted(runs, key=lambda r: r.fixture_id)
    }


def aggregate_runs(runs: Sequence[BenchmarkRunV1]) -> dict[str, object]:
    """Aggregate benchmark runs into the evidence report payload."""
    return {
        "schema_version": "benchmark-report-v1",
        "fixtures": deterministic_summary(runs),
        "ttfrp_wall_clock_seconds": {
            run.fixture_id: run.ttfrp_wall_clock_seconds
            for run in sorted(runs, key=lambda r: r.fixture_id)
        },
        "notes": (
            _REAL_DURATION_NOTE,
            (
                "wall clocks measured via the monotonic seam (default time.monotonic); "
                "counters are deterministic"
            ),
        ),
    }


def canonical_run_bytes(run: BenchmarkRunV1) -> bytes:
    """Canonical bytes helper for evidence writers."""
    return canonical_model_bytes(run)


__all__ = [
    "BenchmarkPolicy",
    "BenchmarkRunV1",
    "BenchmarkStages",
    "ChainResult",
    "aggregate_runs",
    "canonical_run_bytes",
    "deterministic_summary",
    "run_benchmark",
    "run_chain",
]
