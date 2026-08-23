"""Cockpit HTTP routes (task 44) — thin: every route delegates to CockpitWorkspace.

Responses are plain JSON-compatible dicts built from validated store models;
untrusted input is parsed exactly once by the strict request models. Every
failure surfaces as a structured ``{error: {code, detail}}`` envelope via the
typed cockpit errors (handlers registered in ``app.py``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse

from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.backend import CockpitWorkspace

# Runtime imports (NOT TYPE_CHECKING): FastAPI resolves parameter annotations
# at decoration time via get_type_hints, so these model imports stay top-level.
from services.episode_cockpit.models import (
    ApprovalExecuteRequest,
    BriefPutRequest,
    EpisodeCreateRequest,
    NonEmpty,
    RebuildRequest,
    ReferenceRegisterRequest,
    ReviewChatRequest,
    Seconds,
)
from services.episode_cockpit.review_chat import ReviewChatContext
from services.episode_cockpit.review_interpreter import build_review_llm_call, interpret
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
    """POST /episodes/{id}/rebuild — additive optional applied_command ref (task 47).

    Subclasses the task-44 request inline (same precedent as above) so the
    rebuild-requests model stays untouched while the route can carry the
    applied review-command reference whose lineage derives the stage hint.
    """

    applied_command: Identifier | None = None


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


@router.post("/episodes")
def create_episode(request: EpisodeCreateRequest, workspace: Workspace) -> dict[str, object]:
    return workspace.create_episode(
        source_folder=request.source_folder, brief_text=request.brief_text
    )


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


@router.get("/episodes/{episode_id}/preview")
def episode_preview(episode_id: str, workspace: Workspace) -> FileResponse:
    return FileResponse(
        workspace.preview_path(episode_id), media_type="video/mp4", filename="preview.mp4"
    )


@router.get("/episodes/{episode_id}/flags")
def episode_flags(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.review_flags(episode_id)


@router.post("/episodes/{episode_id}/review-chat")
def review_chat(
    episode_id: str, request: ReviewChatRequest, workspace: Workspace
) -> dict[str, object]:
    stored = workspace.append_review_chat(
        episode_id, text=request.text, at_seconds=request.at_seconds
    )
    nearby = workspace.nearby_context(episode_id, at_seconds=request.at_seconds)
    draft = interpret(
        request.text,
        ReviewChatContext(at_seconds=request.at_seconds),
        nearby,
        build_review_llm_call(),
    )
    return stored | {"draft": draft.model_dump(mode="json")}


@router.post("/episodes/{episode_id}/rebuild", status_code=status.HTTP_202_ACCEPTED)
def rebuild(
    episode_id: str, request: RebuildRequestWithCommand, workspace: Workspace
) -> dict[str, object]:
    if request.applied_command is None:
        return workspace.record_rebuild(episode_id, stage_hint=request.stage_hint)
    plan = workspace.resolve_rebuild_stages(episode_id, request.applied_command)
    stage_hint = request.stage_hint if request.stage_hint is not None else ",".join(plan.stages)
    recorded = workspace.record_rebuild(episode_id, stage_hint=stage_hint)
    return recorded | {
        "applied_command": request.applied_command,
        "rebuild_stages": list(plan.stages),
    }


@router.get("/episodes/{episode_id}/approvals")
def list_approvals(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.list_approvals(episode_id)


@router.post("/episodes/{episode_id}/approvals/{approval_id}")
def execute_approval(
    episode_id: str,
    approval_id: str,
    request: ApprovalExecuteRequest,
    workspace: Workspace,
) -> dict[str, object]:
    return workspace.execute_approval(
        episode_id,
        approval_id,
        decision=request.decision,
        actor_id=request.actor_id,
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
