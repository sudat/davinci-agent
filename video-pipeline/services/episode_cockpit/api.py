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

from services.episode_cockpit.backend import CockpitWorkspace

# Runtime imports (NOT TYPE_CHECKING): FastAPI resolves parameter annotations
# at decoration time via get_type_hints, so each noqa: TC001 below is load-bearing.
from services.episode_cockpit.models import (  # noqa: TC001
    ApprovalExecuteRequest,
    BriefPutRequest,
    EpisodeCreateRequest,
    RebuildRequest,
    ReferenceRegisterRequest,
    ReviewChatRequest,
)

router = APIRouter()


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
    return workspace.append_review_chat(
        episode_id, text=request.text, at_seconds=request.at_seconds
    )


@router.post("/episodes/{episode_id}/rebuild", status_code=status.HTTP_202_ACCEPTED)
def rebuild(
    episode_id: str, request: RebuildRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.record_rebuild(episode_id, stage_hint=request.stage_hint)


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


__all__ = ["router"]
