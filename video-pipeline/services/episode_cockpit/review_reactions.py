"""Reaction flow (UX redesign 工程2, U02-U05 / V5-RSL-003, brief §3.3/§6.2).

One message that REACTS to the latest unconsumed proposal set is answered
against that set — never against thin air:

- 「Bが好き」/「Aがいい」: the CHOICE FACT only. No reason is asked, none is
  stored; the set stays unconsumed because adoption happens at apply time
  via the single-draft echo (recorded ``outcome=chosen``).
- 「両方違う」: record ``outcome=rejected`` FIRST, then re-run the cause
  investigation with the rejection context. A deterministic guard keeps an
  identical re-presentation honestly flagged instead of repeated (同じ案を
  自動保存/再提示しない, PRD §4.1.2).
- 「前よりいい。でもせわしない」: continuation — the set stays unconsumed
  and the prior drafts + reaction travel to the LLM as DATA so the new
  proposal adjusts the previous one instead of restating it.

Display honesty: ``investigation_state`` separates hypothesis from checked
materials (transcript/scene text — plus, when the ``review_frame_materials``
gate is ON, a few read-only video STILLS; a still is one frame and is never
claimed as an audio or whole-video verification), mirroring the 工程1
feelings contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.review_chat import (
    FrameMaterial,
    PriorProposalContext,
    ReviewChatContext,
    ReviewCommandDraft,
    reaction_no_proposal_draft,
    with_reaction_fallback_reason,
)
from services.episode_cockpit.review_frames import gather_route_frame_materials
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    ReviewLlmCall,
    interpret_message_with_outcome,
    llm_carries_images,
)
from services.episode_cockpit.review_proposals import (
    ReviewProposalSet,
    effective_proposal_kind,
    repeats_rejected_set,
)

if TYPE_CHECKING:
    from services.episode_cockpit.backend import CockpitWorkspace
    from services.episode_cockpit.models import (
        ProposalKind,
        ReviewChatRequest,
        ReviewReactionKind,
    )

_MIN_DRAFTS_FOR_CHOICE = 2

_REACTION_FALLBACK_REASONS: dict[str, str] = {
    "both-different": (
        "両方とも違うとの反応を記録しました。原因の再調査が必要です"
    ),
    "continuation": (
        "前の提案を踏まえた調整の依頼として記録しました。原因の特定には"
        "追加の確認が必要です"
    ),
}


def investigation_state(primary: ReviewCommandDraft, nearby: NearbyContext) -> str:
    """Honest DISPLAY state: hypothesis-proposed / materials-checked /
    unconfirmed. Never a cause-identified claim (「原因を調査しました」 is
    forbidden — 工程2 machinery reads transcript/scene TEXT only)."""

    if primary.hypothesis:
        return "hypothesis-proposed"
    if nearby.transcript_snippet or nearby.shot_description:
        return "materials-checked"
    return "unconfirmed"


def checked_materials(
    nearby: NearbyContext,
    frames: tuple[FrameMaterial, ...] = (),
    *,
    frames_delivery_attempted: bool = False,
    frames_verified: bool = False,
) -> dict[str, object]:
    """U02: what the investigation COULD check, shown separately from any
    hypothesis. Transcript/shot are booleans; ``frames`` — each a traceable
    {path, at_seconds, source} record — appears ONLY when stills were
    extracted (absent = not extracted, never "checked none"). A still is
    ONE frame: never an audio or whole-video verification.

    Rework round 2 P1-2 — three-level honesty; 抽出 / AIに渡した /
    確認結果が返った are DIFFERENT facts and never conflated. The frame keys
    ride only WITH ``frames`` (without them the dict stays today's shape):
    - ``frames``: the extraction FACT (stills exist on disk, traceable).
    - ``frames_delivery_attempted`` (呼出試行; rework round 4 P2): an
      image-capable transport call was ATTEMPTED with these frames
      (invocation fact; pre-spawn failures included) — actual
      transport-level delivery is UNCONFIRMED. codex-exec invoked with
      non-empty images reads True even when the exec fails before spawn;
      openai-api is False ALWAYS — no image input by construction; no LLM
      at all, or a deterministic short-circuit that never calls the
      transport, is False.
    - ``frames_verified``: True ONLY when ``frames_delivery_attempted``
      AND the model call returned usable proposals (the interpreter did
      NOT answer from the deterministic fallback). EVIDENCE-LEVEL proxy:
      it proves the call succeeded and was usable, never what the model
      actually looked at.
    """

    materials: dict[str, object] = {
        "transcript": nearby.transcript_snippet is not None,
        "shot": nearby.shot_description is not None,
    }
    if frames:
        materials["frames"] = [frame.model_dump(mode="json") for frame in frames]
        materials["frames_delivery_attempted"] = frames_delivery_attempted
        # Structural honesty: verified implies attempted, whatever was passed.
        materials["frames_verified"] = bool(
            frames_verified and frames_delivery_attempted
        )
    return materials


def reaction_chat_response(
    episode_id: str,
    request: ReviewChatRequest,
    workspace: CockpitWorkspace,
    reaction: ReviewReactionKind,
    llm: ReviewLlmCall | None,
) -> dict[str, object]:
    """Answer a reaction against the latest unconsumed proposal set.

    ``llm`` is resolved BY THE CALLER so the transport factory stays
    patchable at the api layer. ``checked_materials`` honesty (rework
    round 4 P2 contract): ``frames_delivery_attempted`` is True only when
    stills were extracted AND an image-capable transport call was
    ATTEMPTED with these frames (invocation fact; pre-spawn failures
    included) — actual transport-level delivery is UNCONFIRMED.
    ``frames_verified`` additionally requires the LLM call to have
    returned usable proposals (see ``checked_materials``).
    """

    prior = workspace.latest_unconsumed_proposal_set(episode_id)
    if prior is None:
        raise CockpitUnprocessableError(
            "no-proposal-to-react-to",
            "this message reacts to a proposal, but no unconsumed proposal "
            "set exists; send a review message that produces one first",
        )
    if reaction in ("choice-a", "choice-b"):
        return _choice_response(episode_id, request, workspace, reaction, prior)
    saved_kind: ProposalKind = "alternatives" if reaction == "both-different" else "command-bundle"
    if reaction == "both-different":
        workspace.record_proposal_outcome(
            episode_id, set_sequence=prior.sequence, outcome="rejected"
        )
    nearby = workspace.nearby_context(episode_id, at_seconds=request.at_seconds)
    frames = gather_route_frame_materials(
        workspace,
        episode_id,
        at_seconds=request.at_seconds,
        eligible=reaction in ("both-different", "continuation"),
    )
    context = ReviewChatContext(
        at_seconds=request.at_seconds,
        reaction_kind=reaction,
        prior_set=PriorProposalContext(set_sequence=prior.sequence, drafts=prior.drafts),
        proposal_kind=saved_kind,
        frame_materials=frames,
    )
    drafts, llm_proposals_used, llm_invoked = interpret_message_with_outcome(
        request.text, context, nearby, llm
    )
    if repeats_rejected_set(drafts, prior.drafts):
        drafts = [
            reaction_no_proposal_draft(request.text, _REACTION_FALLBACK_REASONS[reaction])
        ]
    else:
        drafts = with_reaction_fallback_reason(drafts, _REACTION_FALLBACK_REASONS[reaction])
    primary = drafts[0]
    stored = workspace.append_review_chat(
        episode_id,
        text=request.text,
        at_seconds=request.at_seconds,
        reaction=reaction,
        in_response_to_set=prior.sequence,
        hypothesis=primary.hypothesis,
        frame_materials=frames,
    )
    workspace.record_review_proposals(
        episode_id,
        chat_sequence=cast("int", stored["sequence"]),
        drafts=tuple(drafts),
        responds_to_set=prior.sequence,
        reaction_kind=reaction,
        proposal_kind=saved_kind,
    )
    response = stored | {"draft": primary.model_dump(mode="json")}
    if len(drafts) > 1:
        response["drafts"] = [draft.model_dump(mode="json") for draft in drafts]
    response["investigation_state"] = investigation_state(primary, nearby)
    frames_delivery_attempted = bool(frames) and llm_carries_images(llm) and llm_invoked
    response["checked_materials"] = checked_materials(
        nearby,
        frames,
        frames_delivery_attempted=frames_delivery_attempted,
        frames_verified=frames_delivery_attempted and llm_proposals_used,
    )
    response["responds_to_set"] = prior.sequence
    response["proposal_kind"] = saved_kind
    return response


def _choice_response(
    episode_id: str,
    request: ReviewChatRequest,
    workspace: CockpitWorkspace,
    reaction: ReviewReactionKind,
    prior: ReviewProposalSet,
) -> dict[str, object]:
    """Record the choice FACT on the chat log; the set stays unconsumed —
    adoption is the operator's single-draft apply (``outcome=chosen``).

    工程2 rework: a choice only makes sense between MUTUALLY EXCLUSIVE
    alternatives. Against a command bundle the choice is refused (its
    fixes apply together, never one-by-one).
    """

    if effective_proposal_kind(prior) != "alternatives":
        raise CockpitUnprocessableError(
            "choice-requires-alternatives",
            "a choice names one of several ALTERNATIVE proposals; the latest "
            "set is one command bundle whose fixes apply together",
        )
    if len(prior.drafts) < _MIN_DRAFTS_FOR_CHOICE:
        raise CockpitUnprocessableError(
            "choice-requires-multiple-drafts",
            "a choice names one of several drafts; the latest proposal "
            f"set has {len(prior.drafts)} draft(s)",
        )
    stored = workspace.append_review_chat(
        episode_id,
        text=request.text,
        at_seconds=request.at_seconds,
        reaction=reaction,
        in_response_to_set=prior.sequence,
    )
    return stored | {
        "reaction": reaction,
        "in_response_to_set": prior.sequence,
        "chosen_draft_index": 0 if reaction == "choice-a" else 1,
        "proposal_kind": "alternatives",
    }


__all__ = [
    "checked_materials",
    "investigation_state",
    "reaction_chat_response",
]
