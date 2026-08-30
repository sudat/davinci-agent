"""Evidence v2 corroboration probes: the typed failures, the per-assembly
query state, and one probe per PRD 7.4 corroboration method.

Split from ``evidence_v2.py`` (T7) so the assembly module stays within the
250 pure-LOC ceiling. This module owns ``ProbeDeps`` (api handle, usage
counting, span-cached queries) and the probe family; ``evidence_v2`` keeps
the bundle models and assembly, re-exporting the public names.

T7 correction: ``deep_shot_vision`` corroborates ONLY through overlapping
fused ``MomentDeepReviewV1`` rows (``moment_reviews``, task 17) and cites
their content hash, exact half-open span, and overall confidence — an
ordinary shot description is NEVER relabeled as deep vision.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.editorial_v2.moment_models import MomentCandidateV2
from services.media_intelligence.moment_review_real import is_synthetic_provider
from services.media_intelligence.video_understanding_models import VIDEO_UNDERSTANDING_TOOL
from services.media_query import v2_models as vm

if TYPE_CHECKING:
    from services.media_query.query_v2 import MediaQueryApiV2

_PAUSE_MAX_ENERGY: Final = 0.5

CorroborationMethod = Literal[
    "transcript",
    "deep_shot_vision",
    "exact_frames",
    "audio_measurement",
    "similarity",
    "scene_metadata",
    "source_quality",
]


class EvidenceIncompleteV2(Exception):  # noqa: N818 (outcome category, mirrors v1/vm errors)
    """The multimodal evidence bundle cannot corroborate the declared inputs."""

    code: str = "incomplete"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class EvidenceBudgetExceededV2(EvidenceIncompleteV2):
    """A v2 api budget was exhausted mid-assembly; never silently partial."""

    code = "budget-exceeded"


class MomentReviewCitationV2(StrictModel):
    """One cited fused moment review: hash, exact span, and confidence."""

    review_id: str = Field(min_length=1, strict=True)
    artifact_sha: Sha256
    span: vm.FrameSpan
    overall_confidence: float = Field(ge=0.0, le=1.0)


class Usage:
    """Counts completed api_v2 calls and the rows they returned."""

    __slots__ = ("calls", "rows")

    def __init__(self) -> None:
        self.calls = 0
        self.rows = 0

    def record(self, response: object) -> None:
        self.calls += 1
        rows = getattr(response, "rows", None)
        self.rows += len(rows) if rows is not None else 1


def run_counted[T](usage: Usage, call: Callable[[], T]) -> T:
    try:
        response = call()
    except vm.ApiBudgetExceededV2 as error:
        raise EvidenceBudgetExceededV2(error.detail) from error
    usage.record(response)
    return response


class ProbeDeps:
    """Per-assembly shared state: api, usage, allowance, cached queries."""

    __slots__ = ("allowance", "api", "reviews", "scene", "shots", "source_id", "usage")

    def __init__(
        self, api: MediaQueryApiV2, source_id: str | None, allowance: frozenset[str] | None
    ) -> None:
        self.api = api
        self.source_id = source_id
        self.allowance = allowance
        self.usage = Usage()
        self.scene: vm.SceneSummaryResponse | None = None
        self.shots: dict[tuple[int, int], vm.ShotsResponse] = {}
        self.reviews: dict[tuple[int, int], vm.MomentReviewsResponse] = {}


def _page() -> vm.V2Pagination:
    return vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=0)


def _shots_page(deps: ProbeDeps, span: vm.FrameSpan) -> vm.ShotsResponse:
    key = (span.start_frame, span.end_frame)
    if key not in deps.shots:
        deps.shots[key] = run_counted(
            deps.usage,
            lambda: deps.api.shots(vm.ShotsRequest(span=span, pagination=_page())),
        )
    return deps.shots[key]


def _reviews_page(deps: ProbeDeps, span: vm.FrameSpan) -> vm.MomentReviewsResponse:
    key = (span.start_frame, span.end_frame)
    if key not in deps.reviews:
        deps.reviews[key] = run_counted(
            deps.usage,
            lambda: deps.api.moment_reviews(
                vm.MomentReviewsRequest(span=span, pagination=_page())
            ),
        )
    return deps.reviews[key]


@dataclass(frozen=True, slots=True)
class ProbeHit:
    """One probe's outcome: corroborated or not, plus citable ids/shas."""

    ok: bool
    ids: tuple[str, ...] = ()
    shas: tuple[str, ...] = ()
    citations: tuple[MomentReviewCitationV2, ...] = ()


def _shot_hit(
    rows: Sequence[vm.ShotRow] | Sequence[vm.AudioEnergyRow] | Sequence[vm.QualityRangeV2Row],
) -> ProbeHit:
    return ProbeHit(
        ok=bool(rows),
        ids=tuple(sorted({row.shot_id for row in rows})),
        shas=tuple(sorted({row.artifact_sha for row in rows})),
    )


def _probe_transcript(
    deps: ProbeDeps, _candidate: MomentCandidateV2, span: vm.FrameSpan
) -> ProbeHit:
    source = deps.source_id
    if source is None:
        return ProbeHit(ok=False)
    response = run_counted(
        deps.usage,
        lambda: deps.api.transcript_range(
            vm.TranscriptRangeRequest(source_id=source, span=span, pagination=_page())
        ),
    )
    return ProbeHit(
        ok=bool(response.rows),
        ids=tuple(sorted({row.segment_id for row in response.rows})),
        shas=tuple(sorted({row.artifact_sha for row in response.rows})),
    )


def _citation(row: vm.MomentReviewRow) -> MomentReviewCitationV2:
    return MomentReviewCitationV2(
        review_id=row.review_id,
        artifact_sha=row.artifact_sha,
        span=row.span,
        overall_confidence=row.overall_confidence,
    )


def _probe_vision(
    deps: ProbeDeps, _candidate: MomentCandidateV2, span: vm.FrameSpan
) -> ProbeHit:
    rows = tuple(
        r for r in _reviews_page(deps, span).rows
        if not is_synthetic_provider(r.provider) and r.tool == VIDEO_UNDERSTANDING_TOOL
    )
    return ProbeHit(
        ok=bool(rows),
        ids=tuple(sorted({r.review_id for r in rows})),
        shas=tuple(sorted({r.artifact_sha for r in rows})),
        citations=tuple(_citation(r) for r in rows),
    )


def _probe_frames(
    deps: ProbeDeps, _candidate: MomentCandidateV2, span: vm.FrameSpan
) -> ProbeHit:
    rows = _shots_page(deps, span).rows
    covered = [
        r
        for r in rows
        if r.span.start_frame <= span.start_frame and r.span.end_frame >= span.end_frame
    ]
    return _shot_hit(covered)


def _probe_audio(
    deps: ProbeDeps, candidate: MomentCandidateV2, span: vm.FrameSpan
) -> ProbeHit:
    max_energy = _PAUSE_MAX_ENERGY if candidate.candidate_type == "pause" else None
    response = run_counted(
        deps.usage,
        lambda: deps.api.audio_energy_ranges(
            vm.AudioEnergyRangesRequest(span=span, max_energy=max_energy, pagination=_page())
        ),
    )
    return _shot_hit(response.rows)


def _probe_similarity(
    deps: ProbeDeps, candidate: MomentCandidateV2, _span: vm.FrameSpan
) -> ProbeHit:
    anchor = candidate.evidence_refs[0]
    try:
        response = run_counted(
            deps.usage,
            lambda: deps.api.similar_shots(vm.SimilarShotsRequest(shot_id=anchor)),
        )
    except vm.ApiShotNotFoundV2:
        return ProbeHit(ok=False)
    ids = {anchor, *(row.ref_shot_id for row in response.rows)}
    shas = {row.artifact_sha for row in response.rows}
    return ProbeHit(ok=response.total > 0, ids=tuple(sorted(ids)), shas=tuple(sorted(shas)))


def _probe_scene(
    deps: ProbeDeps, _candidate: MomentCandidateV2, _span: vm.FrameSpan
) -> ProbeHit:
    if deps.scene is None:
        deps.scene = run_counted(
            deps.usage, lambda: deps.api.scene_summary(vm.SceneSummaryRequest())
        )
    rows = deps.scene.rows
    ids = tuple(sorted(row.episode_id for row in rows if row.shot_count > 0))
    shas = tuple(sorted({sha for row in rows for sha in row.artifact_shas}))
    return ProbeHit(ok=bool(ids), ids=ids, shas=shas)


def _probe_quality(
    deps: ProbeDeps, _candidate: MomentCandidateV2, span: vm.FrameSpan
) -> ProbeHit:
    response = run_counted(
        deps.usage,
        lambda: deps.api.visual_quality_ranges(
            vm.VisualQualityRangesRequest(span=span, pagination=_page())
        ),
    )
    return _shot_hit(response.rows)


type ProbeFn = Callable[[ProbeDeps, MomentCandidateV2, vm.FrameSpan], ProbeHit]
PROBES: Final[dict[CorroborationMethod, ProbeFn]] = {
    "transcript": _probe_transcript,
    "deep_shot_vision": _probe_vision,
    "exact_frames": _probe_frames,
    "audio_measurement": _probe_audio,
    "similarity": _probe_similarity,
    "scene_metadata": _probe_scene,
    "source_quality": _probe_quality,
}


def verify_cited_refs(
    deps: ProbeDeps, candidate: MomentCandidateV2, seen: set[str]
) -> tuple[str, ...]:
    """Every cited ref must be a real id; invented ids are refused by name."""

    shas: list[str] = []
    for ref in candidate.evidence_refs:
        if ref in seen:
            continue
        try:
            detail = run_counted(
                deps.usage,
                lambda shot_id=ref: deps.api.shot_detail(vm.ShotDetailRequest(shot_id=shot_id)),
            )
        except vm.ApiShotNotFoundV2 as error:
            raise EvidenceIncompleteV2(
                f"candidate {candidate.candidate_id} cites evidence ref {ref!r} that no "
                "api_v2 row produced; invented ids/spans are refused"
            ) from error
        seen.add(ref)
        shas.append(detail.artifact_sha)
    return tuple(shas)


__all__ = [
    "PROBES",
    "CorroborationMethod",
    "EvidenceBudgetExceededV2",
    "EvidenceIncompleteV2",
    "MomentReviewCitationV2",
    "ProbeDeps",
    "ProbeFn",
    "ProbeHit",
    "Usage",
    "run_counted",
    "verify_cited_refs",
]
