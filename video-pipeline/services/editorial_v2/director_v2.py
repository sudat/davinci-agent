"""DirectorV2 — three-pass multimodal editorial planning (task 28).

Propose-only, identical in kind to v1 ``services/editorial/director.py``:
this orchestrator never writes plans or job state, never commits
artifacts, never controls Resolve, and holds no DB/shell/file-write/
network handle — commit authority belongs to task 29's validation path.
v1 remains frozen for regression.

Passes: A StoryPlanDraft → B MomentSelectionDraft → C CreativeEditDraft.
``llm_call=None`` (default) runs the deterministic heuristic planner
(``heuristic_planner``); when an ``llm_call`` is injected, every payload is
model-validated and cross-checked against the api_v2-derived candidates and
evidence bundle — unknown candidate ids and uncorroborated keeps are
refused (NO-INVENTED-IDS discipline).

MCP edit_engine note (PRD 7.7): selects/tighten/swap output surfaces as
api_v2 rows when present and is consumed as candidate-generation evidence
ONLY — no MCP calls are made here; final decisions stay channel-aware.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from services.contracts.primitives import Identifier, StrictModel
from services.editorial_v2.episode_brief import require_approved
from services.editorial_v2.evidence_v2 import assemble_evidence_v2
from services.editorial_v2.heuristic_planner import (
    candidate_from_shot,
    plan_creative,
    plan_selection,
    plan_story,
)
from services.editorial_v2.prompt_v2 import (
    CreativeEditDraft,
    EvidenceDigestV2,
    MomentSelectionDraft,
    PassARequest,
    PassBRequest,
    PassCRequest,
    PassName,
    ShotDigestV2,
    StoryPlanDraft,
)
from services.editorial_v2.story_plan import StoryPlanV1, validate_story_plan
from services.editorial_v2.taste_retrieval import cited_taste_entries
from services.media_query import v2_models as vm
from services.reference_learning.models import PreferenceDomain

if TYPE_CHECKING:
    from services.editorial_v2.episode_brief import EpisodeBriefV1
    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.taste_retrieval import TasteCitation
    from services.media_query.query_v2 import MediaQueryApiV2
    from services.reference_learning.models import DerivedTasteProfileV1

type LlmCallV2 = Callable[[PassName, StrictModel], object]


class DirectorV2Error(ValueError):
    """Structured refusal from the director seam (never a silent partial)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ThreePassResult(StrictModel):
    """The three draft artifacts; nothing here is committed."""

    story_plan: StoryPlanV1
    moment_selection: MomentSelectionDraft
    creative_edit: CreativeEditDraft


class DirectorV2:
    """Stateless propose-only orchestrator over one episode's api_v2 index."""

    __slots__ = ()

    def run_three_pass(  # noqa: PLR0913 (pass surface: brief + api + the five keyword-only seams)
        self,
        brief: EpisodeBriefV1,
        api_v2: MediaQueryApiV2,
        *,
        taste_profile: DerivedTasteProfileV1 | None = None,
        llm_call: LlmCallV2 | None = None,
        source_id: str | None = None,
        require_deep_review_keeps: bool = False,
        source_total_frames: int | None = None,
    ) -> ThreePassResult:
        """Three propose-only passes; ``source_id`` enables transcript
        corroboration in the Pass B evidence bundle (T7: with no fused
        reviews indexed, speech corroborates via transcript — never via
        descriptions relabeled as deep vision).
        ``require_deep_review_keeps`` (T7) adds one gate on top: every keep
        must cite overlapping fused ``MomentDeepReviewV1`` evidence (bundle
        ``moment_reviews``), so a keep whose required fused evidence is
        missing, synthetic-filtered, or non-overlapping is refused as
        uncorroborated BEFORE any commit. The gate never mutates the
        selection — GPT-5.6 Sol (or the heuristic in diagnostics) stays the
        only chooser of intent.
        ``source_total_frames`` (T2) is the AUTHORITATIVE source extent: when
        supplied, shot discovery queries exactly the half-open
        ``[0, source_total_frames)`` instead of inferring a window from the
        coverage-duration ``scene_summary`` — sparse episodes whose summed
        coverage ends before the real source must not lose tail candidates.
        Callers that omit it keep the historical inferred-window behavior."""

        require_approved(brief)
        shots = _discover_shots(api_v2, brief.episode_id, source_total_frames)
        digest = EvidenceDigestV2(
            episode_id=brief.episode_id,
            shot_count=len(shots),
            covered_frames=max((s.span.end_frame for s in shots), default=0),
            source_total_frames=source_total_frames,
            shots=tuple(
                ShotDigestV2(
                    shot_id=s.shot_id,
                    start_frame=s.span.start_frame,
                    end_frame=s.span.end_frame,
                    role=s.role,
                    select_potential=s.select_potential,
                    description=s.description,
                )
                for s in shots
            ),
        )
        known_shots = frozenset(s.shot_id for s in shots)

        request_a = PassARequest(brief=brief, evidence_digest=digest)
        if llm_call is None:
            story = plan_story(request_a)
        else:
            story = StoryPlanDraft.model_validate(llm_call("pass_a", request_a))
            validate_story_plan(story.story_plan, set(known_shots))

        candidates = tuple(candidate_from_shot(shot) for shot in shots)
        evidence = assemble_evidence_v2(api_v2, candidates, source_id=source_id)
        known_candidates = frozenset(c.candidate_id for c in candidates)
        b_roll_cites, subtitle_cites = _taste_for_planning(taste_profile)
        request_b = PassBRequest(
            brief=brief,
            story_plan=story.story_plan,
            candidates=candidates,
            evidence=evidence,
            evidence_digest=digest,
        )
        if llm_call is None:
            selection = plan_selection(request_b, b_roll_cites)
        else:
            selection = _validated_selection(
                llm_call("pass_b", request_b), known_candidates, evidence
            )
        if require_deep_review_keeps:
            _require_fused_keeps(selection, evidence)

        request_c = PassCRequest(
            selection=selection, taste_citations=b_roll_cites + subtitle_cites
        )
        if llm_call is None:
            creative = plan_creative(request_c)
        else:
            creative = CreativeEditDraft.model_validate(llm_call("pass_c", request_c))
            _require_known_targets(creative, known_candidates)
        return ThreePassResult(
            story_plan=story.story_plan,
            moment_selection=selection,
            creative_edit=creative,
        )


def _discover_shots(
    api: MediaQueryApiV2, episode_id: Identifier, source_total_frames: int | None
) -> tuple[vm.ShotRow, ...]:
    if source_total_frames is None:
        summary = api.scene_summary(vm.SceneSummaryRequest())
        row = next((r for r in summary.rows if r.episode_id == episode_id), None)
        if row is None:
            raise DirectorV2Error(
                "episode-not-indexed", f"no api_v2 scene_summary row for {episode_id}"
            )
        end_frame = max(row.covered_frames, 1)
    else:
        if source_total_frames < 1:
            raise DirectorV2Error(
                "invalid-source-extent",
                f"source_total_frames must be >= 1; [0, {source_total_frames}) "
                "is empty or inverted",
            )
        end_frame = source_total_frames
    span = vm.FrameSpan(start_frame=0, end_frame=end_frame)
    pagination = vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=0)
    rows: list[vm.ShotRow] = []
    while True:
        page = api.shots(vm.ShotsRequest(span=span, pagination=pagination))
        rows.extend(page.rows)
        if len(rows) >= page.total or not page.rows:
            break
        pagination = vm.V2Pagination(
            limit=pagination.limit, offset=pagination.offset + pagination.limit
        )
    return tuple(sorted(rows, key=lambda r: (r.span.start_frame, r.shot_id)))


def _taste_for_planning(
    profile: DerivedTasteProfileV1 | None,
) -> tuple[tuple[TasteCitation, ...], tuple[TasteCitation, ...]]:
    if profile is None:
        return (), ()
    return (
        cited_taste_entries(profile, PreferenceDomain.b_roll),
        cited_taste_entries(profile, PreferenceDomain.subtitle),
    )


def _validated_selection(
    payload: object,
    known_candidates: frozenset[str],
    evidence: EvidenceBundleV2,
) -> MomentSelectionDraft:
    draft = MomentSelectionDraft.model_validate(payload)
    unknown = {c.candidate_id for c in draft.proposal.candidates} - known_candidates
    if unknown:
        raise DirectorV2Error(
            "unknown-candidate",
            f"selection cites candidates no api_v2 row produced: {sorted(unknown)}",
        )
    corroborated = {entry.candidate_id for entry in evidence.entries}
    unbacked = {
        c.candidate_id for c in draft.proposal.candidates
        if c.intent != "remove" and c.candidate_id not in corroborated
    }
    if unbacked:
        raise DirectorV2Error(
            "uncorroborated-keep",
            f"kept candidates lack evidence-bundle corroboration: {sorted(unbacked)}",
        )
    return draft


def _require_fused_keeps(
    selection: MomentSelectionDraft, evidence: EvidenceBundleV2
) -> None:
    """Refuse keeps without overlapping fused moment-review citations."""

    cited = {entry.candidate_id: entry.moment_reviews for entry in evidence.entries}
    unbacked = sorted(
        candidate.candidate_id
        for candidate in selection.proposal.candidates
        if candidate.intent == "keep" and not cited.get(candidate.candidate_id)
    )
    if unbacked:
        raise DirectorV2Error(
            "uncorroborated-keep",
            f"kept candidates lack fused moment-review evidence (no overlapping "
            f"MomentDeepReviewV1 in the index): {unbacked}",
        )


def _require_known_targets(
    creative: CreativeEditDraft, known_candidates: frozenset[str]
) -> None:
    targets = {
        i.target_candidate_id
        for i in creative.intents
        if i.target_candidate_id is not None
    }
    unknown = targets - known_candidates
    if unknown:
        raise DirectorV2Error(
            "unknown-candidate", f"intents target unknown candidates: {sorted(unknown)}"
        )


__all__ = ["DirectorV2", "DirectorV2Error", "LlmCallV2", "ThreePassResult"]
