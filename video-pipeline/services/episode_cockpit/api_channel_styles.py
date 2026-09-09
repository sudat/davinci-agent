"""Channel style routes (工程3) — thin: every route delegates to ChannelStyleOps.

The ONLY writers of ``channel-styles.json`` are the two explicit POST
routes below (save + restore); reads never mutate. Version restores
are operator-only by construction: the cockpit binds loopback only and
no model/director path calls these endpoints.

Runtime imports (NOT TYPE_CHECKING): FastAPI resolves parameter
annotations at decoration time via get_type_hints, so the save-request
model import stays top-level (the api.py precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from services.contracts.primitives import StrictModel
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.channel_styles import ChannelStyleSaveRequest  # noqa: TC001
from services.episode_cockpit.errors import CockpitUnprocessableError

if TYPE_CHECKING:
    from services.episode_cockpit.channel_styles import ChannelStyleRecordV1

router = APIRouter()


class ChannelStyleRestoreRequest(StrictModel):
    """POST restore body: the earlier version to re-commit forward."""

    target_version: int


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


def _summaries(record: ChannelStyleRecordV1) -> list[dict[str, object]]:
    return [
        {"version": item.version, "name": item.entry.name, "saved_at": item.saved_at}
        for item in record.versions
    ]


@router.get("/channels")
def list_channels(workspace: Workspace) -> dict[str, object]:
    return workspace.list_channels()


@router.get("/channels/{channel_id}/style")
def get_channel_style(
    channel_id: str, workspace: Workspace, version: int | None = None
) -> dict[str, object]:
    record = workspace.get_channel_style(channel_id)
    payload: dict[str, object] = {
        "channel_id": record.channel_id,
        "available": bool(record.versions),
        "current": record.current,
        "versions": _summaries(record),
        "entry": None,
    }
    wanted = record.current if version is None else version
    if wanted is not None:
        match = next((item for item in record.versions if item.version == wanted), None)
        if match is None:
            raise CockpitUnprocessableError(
                "style-version-unknown",
                f"channel {record.channel_id} has no style version {wanted}",
            )
        payload["entry"] = match.model_dump(mode="json")
    return payload


@router.post("/channels/{channel_id}/style/save", status_code=status.HTTP_201_CREATED)
def save_channel_style(
    channel_id: str, request: ChannelStyleSaveRequest, workspace: Workspace
) -> JSONResponse:
    version, idempotent = workspace.save_channel_style(channel_id, request)
    body = {"channel_id": channel_id, "version": version, "idempotent": idempotent}
    return JSONResponse(
        status_code=status.HTTP_200_OK if idempotent else status.HTTP_201_CREATED,
        content=body,
    )


@router.post("/channels/{channel_id}/style/restore", status_code=status.HTTP_201_CREATED)
def restore_channel_style(
    channel_id: str, request: ChannelStyleRestoreRequest, workspace: Workspace
) -> dict[str, object]:
    version = workspace.restore_channel_style(channel_id, target_version=request.target_version)
    return {
        "channel_id": channel_id,
        "version": version,
        "restored_from": request.target_version,
    }


__all__ = ["router"]
