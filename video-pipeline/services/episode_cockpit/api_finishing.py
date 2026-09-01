"""Finishing domains status route — read-only, additive.

One operator surface over the T13 finishing harness output:

- ``GET /episodes/{id}/finishing-status`` — the seven domain statuses +
  their justifications as recorded in ``finishing/finishing-run.json``.

Absent file → honest ``{available: false}`` stub (never an error dump);
unknown episode → typed 404 via ``_require_snapshot``; malformed file →
typed 422 ``finishing-run-invalid``. No state mutation, no new artifact.

Kept in a separate router file (registered alongside ``api.router`` and
``kit_router`` in ``app.py``) so the task-44 route file stays untouched —
same precedent as ``api_kit.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from services.episode_cockpit.backend import CockpitWorkspace

router = APIRouter()


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


@router.get("/episodes/{episode_id}/finishing-status")
def finishing_status(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.finishing_status(episode_id)


__all__ = ["router"]
