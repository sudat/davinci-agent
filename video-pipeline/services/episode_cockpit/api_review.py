"""Acceptance-flow routes (task 51) — thin, like api.py, but additive.

Two operator surfaces the Gate V43-4a UX flow needs:

- ``GET /episodes/{id}/approval-sessions`` — pending approvals bundled
  into at most two normal blocking sessions (PRD 13.4) plus the decided
  facts, straight from the append-only ledger;
- ``POST /episodes/{id}/review-chat/apply`` — turn one natural-language
  correction into an applied command and its lineage-scoped partial
  rebuild plan (PRD 13.3).

Kept in a separate router file (registered alongside ``api.router`` in
``app.py``) so the task-44 route file stays untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from services.contracts.primitives import StrictModel
from services.episode_cockpit.backend import CockpitWorkspace

# Runtime imports (NOT TYPE_CHECKING): FastAPI resolves request-model
# annotations at decoration time via get_type_hints (task-44 convention).
from services.episode_cockpit.models import NonEmpty, Seconds  # noqa: TC001

router = APIRouter()


class ReviewChatApplyRequest(StrictModel):
    """POST /episodes/{id}/review-chat/apply — the echoed draft's inputs.

    Carries the same (text, at_seconds) the operator sent, so the
    deterministic re-interpretation reproduces the previewed command
    exactly — the apply path can never diverge from what was echoed.
    """

    text: NonEmpty
    at_seconds: Seconds | None = None


def _workspace(request: Request) -> CockpitWorkspace:
    workspace: CockpitWorkspace = request.app.state.cockpit
    return workspace


Workspace = Annotated[CockpitWorkspace, Depends(_workspace)]


@router.get("/episodes/{episode_id}/approval-sessions")
def approval_sessions(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.approval_sessions(episode_id)


@router.post("/episodes/{episode_id}/review-chat/apply")
def review_chat_apply(
    episode_id: str, request: ReviewChatApplyRequest, workspace: Workspace
) -> dict[str, object]:
    return workspace.apply_review_command(
        episode_id, text=request.text, at_seconds=request.at_seconds
    )


__all__ = ["router"]
