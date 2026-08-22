# allow: SIZE_OK — task 23 pins the commit scope to this single module (no sibling
# files allowed); single responsibility: multimodal evidence-bundle assembly.
"""Evidence v2 — multimodal candidate corroboration over MediaQueryApiV2 (task 23).

v1 corroborated with ``transcript_search``/``silence_overlap`` only (frozen
``services/editorial/evidence.py``); v2 extends to the seven PRD 7.4 methods,
each backed by a distinct api_v2 query: transcript -> ``transcript_range``,
deep_shot_vision -> ``shots`` description rows, exact_frames -> ``shots``
covering row, audio_measurement -> ``audio_energy_ranges`` (pause requires
low energy), similarity -> ``similar_shots``, scene_metadata ->
``scene_summary``, source_quality -> ``visual_quality_ranges``.

Legacy mapping: ``transcript_search`` -> ``transcript``, ``silence_overlap``
-> ``audio_measurement`` (LEGACY_METHOD_ALIASES). NO-INVENTED-IDS preserved:
every bundle ref is an id produced by an api_v2 returned row (or verified via
``shot_detail``); a cited id no query produced raises EvidenceIncompleteV2
naming it. Budget exhaustion raises EvidenceBudgetExceededV2 — never a silent
partial bundle; partial output ONLY under an explicit ``partial_allowance``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Final, Literal, NamedTuple

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.editorial_v2.moment_models import MomentCandidateV2
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

LEGACY_METHOD_ALIASES: Final[dict[str, CorroborationMethod]] = {
    "transcript_search": "transcript",
    "silence_overlap": "audio_measurement",
}

_VISUAL: Final[tuple[CorroborationMethod, ...]] = ("deep_shot_vision", "exact_frames")
_METHOD_CHAINS: Final[dict[str, tuple[CorroborationMethod, ...]]] = {
    "speech": ("transcript", "deep_shot_vision"),
    "pause": ("audio_measurement",),
    "alternate_take": ("similarity", "deep_shot_vision"),
    **dict.fromkeys(
        ("b_roll", "reaction", "action", "insert", "product_demo", "screen_demo"), _VISUAL
    ),
    "establishing": ("scene_metadata", "deep_shot_vision"),
    "ambient": ("audio_measurement", "scene_metadata"),
    "transition": ("exact_frames", "deep_shot_vision"),
    **dict.fromkeys(("graphic", "still"), ("exact_frames", "source_quality")),
}


class EvidenceIncompleteV2(Exception):  # noqa: N818 (outcome category, mirrors v1/vm errors)
    """The multimodal evidence bundle cannot corroborate the declared inputs."""

    code: str = "incomplete"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class EvidenceBudgetExceededV2(EvidenceIncompleteV2):
    """A v2 api budget was exhausted mid-assembly; never silently partial."""

    code = "budget-exceeded"


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


class CandidateEvidenceV2(StrictModel):
    """Per-candidate corroboration record; every ref is a real index id."""

    candidate_id: Identifier
    methods: Annotated[tuple[CorroborationMethod, ...], BeforeValidator(_to_tuple)]
    evidence_refs: Annotated[tuple[Identifier, ...], BeforeValidator(_to_tuple)]
    lineage: Annotated[tuple[Sha256, ...], BeforeValidator(_to_tuple)]


class BudgetUsageV2(StrictModel):
    api_calls: int = Field(ge=0, strict=True)
    rows_returned: int = Field(ge=0, strict=True)
    row_budget: int = Field(gt=0, strict=True)


class EvidenceBundleV2(StrictModel):
    entries: Annotated[tuple[CandidateEvidenceV2, ...], BeforeValidator(_to_tuple)]
    lineage: Annotated[tuple[Sha256, ...], BeforeValidator(_to_tuple)]
    budget: BudgetUsageV2
    partial: bool
    missing: Annotated[tuple[Identifier, ...], BeforeValidator(_to_tuple)]


class _Usage:
    """Counts completed api_v2 calls and the rows they returned."""

    __slots__ = ("calls", "rows")

    def __init__(self) -> None:
        self.calls = 0
        self.rows = 0

    def record(self, response: object) -> None:
        self.calls += 1
        rows = getattr(response, "rows", None)
        self.rows += len(rows) if rows is not None else 1


def _run[T](usage: _Usage, call: Callable[[], T]) -> T:
    try:
        response = call()
    except vm.ApiBudgetExceededV2 as error:
        raise EvidenceBudgetExceededV2(error.detail) from error
    usage.record(response)
    return response


class _Deps:
    """Per-assembly shared state: api, usage, allowance, cached queries."""

    __slots__ = ("allowance", "api", "scene", "shots", "source_id", "usage")

    def __init__(
        self, api: MediaQueryApiV2, source_id: str | None, allowance: frozenset[str] | None
    ) -> None:
        self.api = api
        self.source_id = source_id
        self.allowance = allowance
        self.usage = _Usage()
        self.scene: vm.SceneSummaryResponse | None = None
        self.shots: dict[tuple[int, int], vm.ShotsResponse] = {}

    def scene_rows(self) -> tuple[vm.SceneSummaryRow, ...]:
        if self.scene is None:
            self.scene = _run(self.usage, lambda: self.api.scene_summary(vm.SceneSummaryRequest()))
        return self.scene.rows


def _page() -> vm.V2Pagination:
    return vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=0)


def _shots_page(deps: _Deps, span: vm.FrameSpan) -> vm.ShotsResponse:
    key = (span.start_frame, span.end_frame)
    cached = deps.shots.get(key)
    if cached is None:
        cached = _run(
            deps.usage, lambda: deps.api.shots(vm.ShotsRequest(span=span, pagination=_page()))
        )
        deps.shots[key] = cached
    return cached


class _Hit(NamedTuple):
    ok: bool
    ids: tuple[str, ...] = ()
    shas: tuple[str, ...] = ()


def _shot_hit(
    rows: Sequence[vm.ShotRow] | Sequence[vm.AudioEnergyRow] | Sequence[vm.QualityRangeV2Row],
) -> _Hit:
    return _Hit(
        ok=bool(rows),
        ids=tuple(sorted({row.shot_id for row in rows})),
        shas=tuple(sorted({row.artifact_sha for row in rows})),
    )


def _probe_transcript(deps: _Deps, _candidate: MomentCandidateV2, span: vm.FrameSpan) -> _Hit:
    source = deps.source_id
    if source is None:
        return _Hit(ok=False)
    response = _run(
        deps.usage,
        lambda: deps.api.transcript_range(
            vm.TranscriptRangeRequest(source_id=source, span=span, pagination=_page())
        ),
    )
    return _Hit(
        ok=bool(response.rows),
        ids=tuple(sorted({row.segment_id for row in response.rows})),
        shas=tuple(sorted({row.artifact_sha for row in response.rows})),
    )


def _probe_vision(deps: _Deps, _candidate: MomentCandidateV2, span: vm.FrameSpan) -> _Hit:
    return _shot_hit([r for r in _shots_page(deps, span).rows if r.description])


def _probe_frames(deps: _Deps, _candidate: MomentCandidateV2, span: vm.FrameSpan) -> _Hit:
    rows = _shots_page(deps, span).rows
    covered = [
        r
        for r in rows
        if r.span.start_frame <= span.start_frame and r.span.end_frame >= span.end_frame
    ]
    return _shot_hit(covered)


def _probe_audio(deps: _Deps, candidate: MomentCandidateV2, span: vm.FrameSpan) -> _Hit:
    max_energy = _PAUSE_MAX_ENERGY if candidate.candidate_type == "pause" else None
    response = _run(
        deps.usage,
        lambda: deps.api.audio_energy_ranges(
            vm.AudioEnergyRangesRequest(span=span, max_energy=max_energy, pagination=_page())
        ),
    )
    return _shot_hit(response.rows)


def _probe_similarity(deps: _Deps, candidate: MomentCandidateV2, _span: vm.FrameSpan) -> _Hit:
    anchor = candidate.evidence_refs[0]
    try:
        response = _run(
            deps.usage, lambda: deps.api.similar_shots(vm.SimilarShotsRequest(shot_id=anchor))
        )
    except vm.ApiShotNotFoundV2:
        return _Hit(ok=False)
    ids = {anchor, *(row.ref_shot_id for row in response.rows)}
    shas = {row.artifact_sha for row in response.rows}
    return _Hit(ok=response.total > 0, ids=tuple(sorted(ids)), shas=tuple(sorted(shas)))


def _probe_scene(deps: _Deps, _candidate: MomentCandidateV2, _span: vm.FrameSpan) -> _Hit:
    rows = deps.scene_rows()
    ids = tuple(sorted(row.episode_id for row in rows if row.shot_count > 0))
    shas = tuple(sorted({sha for row in rows for sha in row.artifact_shas}))
    return _Hit(ok=bool(ids), ids=ids, shas=shas)


def _probe_quality(deps: _Deps, _candidate: MomentCandidateV2, span: vm.FrameSpan) -> _Hit:
    response = _run(
        deps.usage,
        lambda: deps.api.visual_quality_ranges(
            vm.VisualQualityRangesRequest(span=span, pagination=_page())
        ),
    )
    return _shot_hit(response.rows)


_ProbeFn = Callable[[_Deps, MomentCandidateV2, vm.FrameSpan], _Hit]
_PROBES: Final[dict[CorroborationMethod, _ProbeFn]] = {
    "transcript": _probe_transcript,
    "deep_shot_vision": _probe_vision,
    "exact_frames": _probe_frames,
    "audio_measurement": _probe_audio,
    "similarity": _probe_similarity,
    "scene_metadata": _probe_scene,
    "source_quality": _probe_quality,
}


def _verify_cited_refs(
    deps: _Deps, candidate: MomentCandidateV2, seen: set[str]
) -> tuple[str, ...]:
    """Every cited ref must be a real id; invented ids are refused by name."""

    shas: list[str] = []
    for ref in candidate.evidence_refs:
        if ref in seen:
            continue
        try:
            detail = _run(
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


def _corroborate(deps: _Deps, candidate: MomentCandidateV2) -> CandidateEvidenceV2 | None:
    span = vm.FrameSpan(
        start_frame=candidate.source_span.start_frame, end_frame=candidate.source_span.end_frame
    )
    chain = _METHOD_CHAINS[candidate.candidate_type]
    seen: set[str] = set()
    shas: set[str] = set()
    methods: list[CorroborationMethod] = []
    for method in chain:
        hit = _PROBES[method](deps, candidate, span)
        if hit.ok:
            methods.append(method)
            seen.update(hit.ids)
            shas.update(hit.shas)
    shas.update(_verify_cited_refs(deps, candidate, seen))
    if methods:
        return CandidateEvidenceV2(
            candidate_id=candidate.candidate_id,
            methods=tuple(methods),
            evidence_refs=tuple(sorted(seen)),
            lineage=tuple(sorted(shas)),
        )
    if deps.allowance is None or candidate.candidate_id not in deps.allowance:
        raise EvidenceIncompleteV2(
            f"candidate {candidate.candidate_id} ({candidate.candidate_type}) could not "
            f"be corroborated by any configured method {chain}"
        )
    return None


def assemble_evidence_v2(
    api: MediaQueryApiV2,
    candidates: Sequence[MomentCandidateV2],
    *,
    source_id: str | None = None,
    partial_allowance: frozenset[str] | None = None,
) -> EvidenceBundleV2:
    """Corroborate each MomentCandidateV2 against the v2 media-query index.

    ``source_id`` enables transcript corroboration (the v2 api cannot discover
    source ids itself); without it the transcript method is unavailable and
    chains fall through to their next method.
    """

    deps = _Deps(api, source_id, partial_allowance)
    entries: list[CandidateEvidenceV2] = []
    missing: list[str] = []
    lineage: set[str] = set()
    for candidate in candidates:
        entry = _corroborate(deps, candidate)
        if entry is None:
            missing.append(candidate.candidate_id)
            continue
        entries.append(entry)
        lineage.update(entry.lineage)
    return EvidenceBundleV2(
        entries=tuple(entries),
        lineage=tuple(sorted(lineage)),
        budget=BudgetUsageV2(
            api_calls=deps.usage.calls, rows_returned=deps.usage.rows, row_budget=vm.V2_ROW_BUDGET
        ),
        partial=bool(missing),
        missing=tuple(missing),
    )


__all__ = [
    "LEGACY_METHOD_ALIASES",
    "BudgetUsageV2",
    "CandidateEvidenceV2",
    "CorroborationMethod",
    "EvidenceBudgetExceededV2",
    "EvidenceBundleV2",
    "EvidenceIncompleteV2",
    "assemble_evidence_v2",
]
