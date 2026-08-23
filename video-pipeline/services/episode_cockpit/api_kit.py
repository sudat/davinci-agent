"""Kit preview routes (task 11) — thin, like api_review.py, additive.

Three operator surfaces over the task-11 preview outputs:

- ``GET /episodes/{id}/kit-previews`` — the manifest re-derived from disk;
- ``GET /episodes/{id}/kit-previews/{domain}/{file}`` — one candidate mp4
  (validated names; the compact UI plays these like PreviewPlayer);
- ``POST /episodes/{id}/kit-previews/{domain}/select`` — record the
  operator's A/B/none choice into the runtime kit-selections record.

Kept in a separate router file (registered alongside ``api.router`` in
``app.py``) so the task-44 route file stays untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import model_validator

from services.contracts.primitives import StrictModel
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.models import NonEmpty  # noqa: TC001 (pydantic runtime)

router = APIRouter()


class KitSelectRequest(StrictModel):
    """POST select — ``recipe_id`` null means どちらも不要/現状維持."""

    recipe_id: NonEmpty | None = None
    note: NonEmpty | None = None

    @model_validator(mode="after")
    def require_choice_or_note(self) -> KitSelectRequest:
        if self.recipe_id is None and self.note is None:
            raise ValueError("recipe_id or note must be present (a choice or its reason)")
        return self


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


@router.get("/episodes/{episode_id}/kit-previews")
def kit_previews(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.list_kit_previews(episode_id)


@router.get("/episodes/{episode_id}/kit-previews/{domain}/{file_name}")
def kit_preview_file(
    episode_id: str, domain: str, file_name: str, workspace: Workspace
) -> FileResponse:
    path = workspace.kit_preview_file(episode_id, domain, file_name)
    return FileResponse(path, media_type="video/mp4", filename=file_name)


@router.post("/episodes/{episode_id}/kit-previews/{domain}/select")
def kit_preview_select(
    episode_id: str, domain: str, request: KitSelectRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.record_kit_selection(
        episode_id, domain, recipe_id=request.recipe_id, note=request.note
    )


__all__ = ["router"]
