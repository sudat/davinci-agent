"""Cockpit HTTP routes (task 44) — thin: every route delegates to CockpitWorkspace.

Responses are plain JSON-compatible dicts built from validated store models;
untrusted input is parsed exactly once by the strict request models. Every
failure surfaces as a structured ``{error: {code, detail}}`` envelope via the
typed cockpit errors (handlers registered in ``app.py``).
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse
from pydantic import BeforeValidator

from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.errors import CockpitConflictError, CockpitUnprocessableError

# Runtime imports (NOT TYPE_CHECKING): FastAPI resolves parameter annotations
# at decoration time via get_type_hints, so these model imports stay top-level.
from services.episode_cockpit.models import (
    ApprovalExecuteRequest,
    BriefPutRequest,
    EditorialGrantRequest,
    EpisodeCreateRequest,
    NonEmpty,
    RebuildRequest,
    ReferenceRegisterRequest,
    ReviewChatRequest,
    Seconds,
    SelfCheckRequest,
)
from services.episode_cockpit.preview_binding import binding_headers
from services.episode_cockpit.review_chat import (
    _FEELINGS_REASON,
    ReviewChatContext,
    classify_reaction,
    interpret_command,
)
from services.episode_cockpit.review_frames import gather_route_frame_materials
from services.episode_cockpit.review_interpreter import (
    build_review_llm_call,
    interpret_message_with_outcome,
    llm_carries_images,
)
from services.episode_cockpit.review_reactions import (
    checked_materials,
    investigation_state,
    reaction_chat_response,
)
from services.episode_cockpit.workspace_context import validated_episode_id
from services.outputs.geometry import normalize_output_id, preview_relatives
from services.reference_learning.domain_extract import extract_domains_seeded

router = APIRouter()


class ReferenceParsePreviewRequest(StrictModel):
    """POST /references/parse-preview — annotate-this-moment draft (task 50).

    Defined inline (not in ``models.py``) so the task-50 backend change stays
    confined to this one additive route file alongside the parallel task-49
    worker; move next to the other request models when the reference surface
    grows (task 51 wiring).
    """

    text: NonEmpty
    ts_seconds: Seconds | None = None


class RebuildRequestWithCommand(RebuildRequest):
    """POST /episodes/{id}/rebuild — additive optional applied-command refs.

    Subclasses the task-44 request inline (same precedent as above) so the
    rebuild-requests model stays untouched while the route can carry the
    applied review-command reference whose lineage derives the stage hint.
    ``applied_commands`` (V44-1) names a BATCH of applied commands whose
    lineage stage sets are unioned into ONE rebuild. ``output_id``
    (工程5) selects the output chain the rebuild renders (default
    landscape, byte-identical).
    """

    applied_command: Identifier | None = None
    applied_commands: (
        Annotated[tuple[Identifier, ...], BeforeValidator(tuple)] | None
    ) = None
    output_id: str | None = None


class OutputRegisterRequest(StrictModel):
    """POST /episodes/{id}/outputs — register the vertical output."""

    output_id: str


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


@router.post("/episodes")
def create_episode(request: EpisodeCreateRequest, workspace: Workspace) -> dict[str, object]:
    return workspace.create_episode(
        source_folder=request.source_folder,
        brief_text=request.brief_text,
        channel=request.channel,
        style_version=request.style_version,
        editorial_grant=request.editorial_grant,
    )


@router.post("/episodes/{episode_id}/editorial-grant")
def set_editorial_grant(
    episode_id: str, request: EditorialGrantRequest, workspace: Workspace
) -> dict[str, object]:
    """Declare (or revoke with granted:false) the transcript→editorial_direct grant.

    Scope wider than the single grantable pair is a typed 422 at the
    request-model boundary — never a silent widen.
    """

    return workspace.set_editorial_grant(episode_id, request)


@router.get("/episodes/{episode_id}/editorial-grant")
def get_editorial_grant(episode_id: str, workspace: Workspace) -> dict[str, object]:
    """Read the persisted grant; never-declared reads granted:null (local_only)."""

    validated = validated_episode_id(episode_id)
    workspace.episode_status(validated)
    grant = workspace.load_editorial_grant(validated)
    if grant is None:
        return {"episode_id": validated, "granted": None}
    return {"episode_id": validated, **grant.model_dump(mode="json")}


@router.get("/episodes")
def list_episodes(workspace: Workspace) -> dict[str, object]:
    return workspace.list_episodes()


@router.get("/episodes/{episode_id}")
def episode_status(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.episode_status(episode_id)


@router.get("/episodes/{episode_id}/brief")
def get_brief(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.get_brief(episode_id)


@router.put("/episodes/{episode_id}/brief")
def put_brief(
    episode_id: str, request: BriefPutRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.put_brief(episode_id, brief_text=request.brief_text)


@router.post("/episodes/{episode_id}/self-check")
def post_self_check(
    episode_id: str, request: SelfCheckRequest, workspace: Workspace
) -> dict[str, object]:
    """Record one structured 本人確認 answer (operator-supplied only).

    Strict validation at the request-model boundary (non-bool answers
    are 422, never coerced); answers persist exactly as sent with null
    = 未回答. Never auto-created — absent file reads as null.
    """

    return workspace.record_self_check(episode_id, request)


@router.get("/episodes/{episode_id}/self-check")
def get_self_check(episode_id: str, workspace: Workspace) -> dict[str, object]:
    """Read the latest 本人確認 record; never-answered reads null."""

    return workspace.load_self_check(episode_id)


@router.get("/episodes/{episode_id}/preview")
def episode_preview(
    episode_id: str,
    workspace: Workspace,
    content_hash: str | None = None,
    output: str | None = None,
) -> FileResponse:
    """Serve the preview video; ``X-Cockpit-Preview-*`` headers ONLY on a
    full cross-check (publish record + succeeded preview stage row + review
    bundle all agree). Header absence means unknown — the video contract
    (200/206, 404 when absent) is unchanged and never partial-headed. A
    ``content_hash`` query differing from the bound record's is a 409
    without the video; an unknown binding cannot differ, so it serves.
    ``output=vertical`` serves the vertical canvas (landscape default).
    """

    try:
        output_id = normalize_output_id(output)
    except ValueError as error:
        raise CockpitUnprocessableError("unknown-output", str(error)) from error
    path = workspace.preview_path(episode_id, output_id)
    binding = workspace.preview_binding(episode_id, output_id)
    if (
        content_hash is not None
        and binding is not None
        and binding.content_hash != content_hash
    ):
        raise CockpitConflictError(
            "preview-content-hash-mismatch",
            "the requested content_hash does not match the current bound preview",
        )
    headers = binding_headers(binding) if binding is not None else None
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=preview_relatives(output_id)[-1],
        headers=headers,
    )


@router.get("/episodes/{episode_id}/flags")
def episode_flags(
    episode_id: str, workspace: Workspace, output: str | None = None
) -> dict[str, object]:
    try:
        output_id = normalize_output_id(output)
    except ValueError as error:
        raise CockpitUnprocessableError("unknown-output", str(error)) from error
    return workspace.review_flags(episode_id, output_id)


@router.post("/episodes/{episode_id}/review-chat")
def review_chat(
    episode_id: str, request: ReviewChatRequest, workspace: Workspace
) -> dict[str, object]:
    """Review chat entry (the UI-facing contract). Reaction messages answer
    the latest saved proposal set; feelings-class messages get a cause
    investigation. ``checked_materials`` (when present) carries THREE
    separate honesty levels — ``frames`` (extraction fact),
    ``frames_delivery_attempted`` (an image-capable transport call was
    ATTEMPTED with these frames — invocation fact; pre-spawn failures
    included; actual transport-level delivery is UNCONFIRMED),
    ``frames_verified`` (attempted AND the model returned usable proposals
    — evidence-level, see ``checked_materials``). Stills are never an
    audio or whole-video verification."""

    reaction = classify_reaction(request.text)
    if reaction is not None:
        return reaction_chat_response(
            episode_id,
            request,
            workspace,
            reaction,
            build_review_llm_call(),
        )
    return _plain_review_chat(episode_id, request, workspace)


def _plain_review_chat(
    episode_id: str,
    request: ReviewChatRequest,
    workspace: CockpitWorkspace,
) -> dict[str, object]:
    nearby = workspace.nearby_context(episode_id, at_seconds=request.at_seconds)
    context = ReviewChatContext(at_seconds=request.at_seconds)
    # 工程2 rework #1: gated read-only stills for feelings-class
    # investigations (gate OFF → () and today's behavior, byte for byte).
    frames = gather_route_frame_materials(
        workspace,
        episode_id,
        at_seconds=request.at_seconds,
        eligible=(
            interpret_command(request.text, context).confirmation_reason
            == _FEELINGS_REASON
        ),
    )
    if frames:
        context = context.model_copy(update={"frame_materials": frames})
    llm = build_review_llm_call()
    drafts, llm_proposals_used, llm_invoked = interpret_message_with_outcome(
        request.text, context, nearby, llm
    )
    primary = drafts[0]
    # The interpreter classifies (UX redesign P1a): a feeling riding an
    # explicit command (「この区間を削除して。退屈から」) is a direct command —
    # ``investigated`` marks materials-gathered ONLY for feelings-class
    # interpretations, and is never a cause-identified claim.
    investigated = primary.investigated
    stored = workspace.append_review_chat(
        episode_id,
        text=request.text,
        at_seconds=request.at_seconds,
        investigated=investigated,
        hypothesis=primary.hypothesis if investigated else None,
        frame_materials=frames,
    )
    # Persist the SERVER-SAVED proposal set (brief §5.3): the echoed browser
    # draft is never the adoption authority — the apply route matches the
    # request against THIS saved set. ``sequence`` in the response names the
    # chat entry whose set the apply request may reference.
    workspace.record_review_proposals(
        episode_id,
        chat_sequence=cast("int", stored["sequence"]),
        drafts=tuple(drafts),
        proposal_kind="command-bundle",
    )
    # ``draft`` stays the primary (first) command for compatibility;
    # ``drafts`` rides along only when the interpreter proposed SEVERAL.
    response = stored | {"draft": primary.model_dump(mode="json")}
    if len(drafts) > 1:
        response["drafts"] = [draft.model_dump(mode="json") for draft in drafts]
    response["proposal_kind"] = "command-bundle"
    if investigated:
        response["investigation_state"] = investigation_state(primary, nearby)
        frames_delivery_attempted = (
            bool(frames) and llm_carries_images(llm) and llm_invoked
        )
        response["checked_materials"] = checked_materials(
            nearby,
            frames,
            frames_delivery_attempted=frames_delivery_attempted,
            frames_verified=frames_delivery_attempted and llm_proposals_used,
        )
    return response


@router.post("/episodes/{episode_id}/rebuild", status_code=status.HTTP_202_ACCEPTED)
def rebuild(
    episode_id: str, request: RebuildRequestWithCommand, workspace: Workspace
) -> dict[str, object]:
    try:
        output_id = normalize_output_id(request.output_id)
    except ValueError as error:
        raise CockpitUnprocessableError("unknown-output", str(error)) from error
    return workspace.record_rebuild(
        episode_id,
        stage_hint=request.stage_hint,
        applied_command=request.applied_command,
        applied_commands=request.applied_commands,
        output_id=output_id,
    )


@router.get("/episodes/{episode_id}/outputs")
def list_outputs(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.list_outputs(episode_id)


@router.post("/episodes/{episode_id}/outputs")
def register_output(
    episode_id: str, request: OutputRegisterRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.register_output(episode_id, request.output_id)


@router.get("/episodes/{episode_id}/approvals")
def list_approvals(
    episode_id: str, workspace: Workspace, output: str | None = None
) -> dict[str, object]:
    try:
        output_id = normalize_output_id(output)
    except ValueError as error:
        raise CockpitUnprocessableError("unknown-output", str(error)) from error
    return workspace.list_approvals(episode_id, output_id)


@router.post("/episodes/{episode_id}/approvals/{approval_id}")
def execute_approval(
    episode_id: str,
    approval_id: str,
    request: ApprovalExecuteRequest,
    workspace: Workspace,
    output: str | None = None,
) -> dict[str, object]:
    try:
        output_id = normalize_output_id(output)
    except ValueError as error:
        raise CockpitUnprocessableError("unknown-output", str(error)) from error
    return workspace.execute_approval(
        episode_id,
        approval_id,
        decision=request.decision,
        actor_id=request.actor_id,
        output_id=output_id,
    )


@router.get("/episodes/{episode_id}/publish-status")
def publish_status(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.publish_status(episode_id)


@router.post("/references")
def register_reference(
    request: ReferenceRegisterRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.register_reference(path=request.path, source_id=request.source_id)


@router.get("/references")
def list_references(workspace: Workspace) -> dict[str, object]:
    return workspace.list_references()


@router.post("/references/parse-preview")
def parse_reference_preview(
    request: ReferenceParsePreviewRequest,
) -> dict[str, object]:
    draft = extract_domains_seeded(request.text)
    payload: dict[str, object] = dict(draft.model_dump(mode="json"))
    payload["ts_seconds"] = request.ts_seconds
    return payload


__all__ = ["router"]
