"""Deterministic heuristic planner — the ``llm_call=None`` path (task 28).

Same discipline as T17's synthetic providers: the heuristic satisfies the
acceptance tests WITHOUT any LLM; the ``llm_call`` seam swaps in later.
Pure over its request: no clock, no randomness, no I/O — identical inputs
produce byte-identical drafts, and without a taste profile the output
never varies. Judgment primitives live in ``heuristic_kernel``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.editorial_v2.heuristic_kernel import (
    Facts,
    decide,
    dimension_scores,
    low_energy_role,
    redundancy_groups,
    semantic_b_roll_matches,
    tokens,
)
from services.editorial_v2.moment_models import (
    MomentCandidateType,
    MomentCandidateV2,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.editorial_v2.prompt_v2 import (
    CreativeEditDraft,
    CreativeIntentKind,
    CreativeIntentV2,
    DimensionNoteV2,
    MomentSelectionDraft,
    PassARequest,
    PassBRequest,
    PassCRequest,
    StoryPlanDraft,
)
from services.editorial_v2.story_plan import (
    StoryBlock,
    StoryBriefRef,
    StoryPlanV1,
    validate_story_plan,
)
from services.reference_learning.models import PreferenceDomain

if TYPE_CHECKING:
    from services.editorial_v2.taste_retrieval import TasteCitation
    from services.media_query import v2_models as vm

_CONFIDENCE: Final[dict[str, float]] = {"high": 0.9, "medium": 0.7, "low": 0.5}
_PROTOTYPE_BY_ROLE: Final[tuple[tuple[str, MomentCandidateType], ...]] = (
    ("b_roll", "b_roll"), ("broll", "b_roll"), ("talk", "speech"),
    ("interview", "speech"), ("reaction", "reaction"), ("action", "action"),
    ("establish", "establishing"), ("ambient", "ambient"), ("pause", "pause"),
    ("graphic", "graphic"), ("still", "still"), ("transition", "transition"),
)


def candidate_from_shot(shot: vm.ShotRow) -> MomentCandidateV2:
    """Candidate generation from an api_v2 row (edit_engine selects, when
    present, surface as rows and are consumed here as evidence ONLY)."""

    candidate_type: MomentCandidateType = "insert"
    for needle, mapped in _PROTOTYPE_BY_ROLE:
        if needle in shot.role.casefold():
            candidate_type = mapped
            break
    return MomentCandidateV2(
        candidate_id=f"cand-{shot.shot_id}",
        candidate_type=candidate_type,
        source_span=MomentSourceSpan(
            start_frame=shot.span.start_frame, end_frame=shot.span.end_frame
        ),
        intent="keep",  # provisional; plan_selection decides
        rationale="candidate generated from an indexed api_v2 shot row",
        evidence_refs=(shot.shot_id,),
        confidence=_CONFIDENCE.get(shot.select_potential, 0.6),
        provenance=MomentProvenance(producer="director-v2-heuristic", version="1"),
    )


def plan_story(request: PassARequest) -> StoryPlanDraft:
    """Pass A: structure before cuts — blocks follow the brief, kinds free-form."""

    brief = request.brief
    shots = request.evidence_digest.shots
    hook = next((s for s in shots if s.select_potential == "high"), shots[0])
    taken = {hook.shot_id}
    blocks = [
        StoryBlock(
            block_id="blk-00", purpose=brief.viewer_promise, block_kind="hook",
            order=0, source_refs=(hook.shot_id,),
        )
    ]
    order = 1
    for entry in brief.must_include:
        idea = tokens(entry.idea_or_moment)
        matched = tuple(
            s.shot_id for s in shots if s.shot_id not in taken and idea & tokens(s.description)
        )
        if not matched:
            continue
        blocks.append(
            StoryBlock(
                block_id=f"blk-{order:02d}", purpose=entry.idea_or_moment,
                block_kind="body", order=order, source_refs=matched,
            )
        )
        taken.update(matched)
        order += 1
    leftover = tuple(s.shot_id for s in shots if s.shot_id not in taken)
    if leftover:
        blocks.append(
            StoryBlock(
                block_id=f"blk-{order:02d}", purpose=brief.episode_objective,
                block_kind="body", order=order, source_refs=leftover,
            )
        )
        order += 1
    if brief.cta is not None:
        blocks.append(
            StoryBlock(
                block_id=f"blk-{order:02d}", purpose=brief.cta, block_kind="cta", order=order
            )
        )
    plan = StoryPlanV1(
        episode_id=brief.episode_id,
        brief_ref=StoryBriefRef(episode_id=brief.episode_id),
        blocks=tuple(blocks),
    )
    validate_story_plan(plan, {s.shot_id for s in shots})
    return StoryPlanDraft(story_plan=plan)


def plan_selection(
    request: PassBRequest, b_roll_citations: tuple[TasteCitation, ...]
) -> MomentSelectionDraft:
    """Pass B: keep valuable non-speech shots, remove boring sections for
    reasons beyond silence/filler, and match B-roll semantically."""

    brief = request.brief
    digest = request.evidence_digest
    descriptions = {d.shot_id: d.description for d in digest.shots}
    potential_of = {d.shot_id: d.select_potential for d in digest.shots}
    block_of: dict[str, str] = {}
    for block in request.story_plan.blocks:
        for ref in block.source_refs:
            block_of.setdefault(ref, block.block_id)
    methods_of = {e.candidate_id: e.methods for e in request.evidence.entries}
    must_ideas = [tokens(entry.idea_or_moment) for entry in brief.must_include]
    likes = tuple(c for c in b_roll_citations if c.polarity == "like")
    like_refs = likes[0].entry_refs if likes else ()
    groups = redundancy_groups(request.candidates, descriptions)
    ordered = sorted(
        request.candidates, key=lambda c: (c.source_span.start_frame, c.candidate_id)
    )
    decided: list[MomentCandidateV2] = []
    notes: list[DimensionNoteV2] = []
    for candidate in ordered:
        shot_id = candidate.evidence_refs[0]
        description = descriptions.get(shot_id, "")
        taste_refs = (
            like_refs
            if candidate.candidate_type == "b_roll"
            and like_refs
            and potential_of.get(shot_id) == "low"
            else ()
        )
        facts = Facts(
            candidate_type=candidate.candidate_type,
            potential=potential_of.get(shot_id, "medium"),
            quiet=low_energy_role(description),
            must=any(idea & tokens(description) for idea in must_ideas),
            methods=len(methods_of.get(candidate.candidate_id, ())),
            grouped=candidate.candidate_id in groups,
            assigned=shot_id in block_of,
            taste_refs=taste_refs,
        )
        intent, rationale = decide(facts)
        decided.append(
            candidate.model_copy(
                update={
                    "intent": intent,
                    "rationale": rationale,
                    "story_block_ref": block_of.get(shot_id),
                    "redundancy_group": groups.get(candidate.candidate_id),
                }
            )
        )
        notes.append(
            DimensionNoteV2(
                candidate_id=candidate.candidate_id,
                scores=dimension_scores(facts),
                low_energy_role=facts.quiet,
                taste_entry_refs=facts.taste_refs,
            )
        )
    speech = tuple(
        c for c in decided if c.candidate_type == "speech" and c.intent in ("keep", "optional")
    )
    b_rolls = tuple(
        c for c in decided if c.candidate_type == "b_roll" and c.intent in ("keep", "optional")
    )
    return MomentSelectionDraft(
        proposal=MomentSelectionProposalV2(
            proposal_id=f"msel-{brief.episode_id}",
            episode_id=brief.episode_id,
            candidates=tuple(decided),
        ),
        dimension_notes=tuple(notes),
        b_roll_matches=semantic_b_roll_matches(speech, b_rolls, descriptions),
        taste_citations=b_roll_citations if likes else (),
    )


def plan_creative(request: PassCRequest) -> CreativeEditDraft:
    """Pass C lite (task 31 owns the full CreativeEditPlanProposalV2):
    semantic intents + taste citations, never raw Resolve calls."""

    selection = request.selection
    subtitle_cites = [
        c for c in request.taste_citations if c.domain == PreferenceDomain.subtitle
    ]
    suppress_subtitles = any(c.polarity == "dislike" for c in subtitle_cites)
    subtitle_refs = next((c.entry_refs for c in subtitle_cites if c.polarity == "like"), ())
    keeps = [c for c in selection.proposal.candidates if c.intent == "keep"]
    intents: list[CreativeIntentV2] = []

    def add(
        kind: CreativeIntentKind, target: str | None, note: str, refs: tuple[str, ...] = ()
    ) -> None:
        intents.append(
            CreativeIntentV2(
                intent_id=f"ci-{len(intents):03d}", kind=kind,
                target_candidate_id=target, note=note, taste_entry_refs=refs,
            )
        )

    if keeps:
        add("title_lower_third", keeps[0].candidate_id, "title/lower-third over the hook block")
    if not suppress_subtitles:
        for talker in [c for c in keeps if c.candidate_type == "speech"]:
            add(
                "subtitle_track", talker.candidate_id,
                "burned subtitles on kept speech", subtitle_refs,
            )
    for match in selection.b_roll_matches:
        add(
            "b_roll_insert", match.speech_candidate_id,
            f"overlay B-roll {match.b_roll_candidate_id} sharing "
            f"{','.join(match.shared_terms[:3])}",
        )
    if keeps:
        add("music_cue", None, "background music bed under body blocks")
    return CreativeEditDraft(intents=tuple(intents), taste_citations=request.taste_citations)


__all__ = [
    "candidate_from_shot",
    "plan_creative",
    "plan_selection",
    "plan_story",
]
