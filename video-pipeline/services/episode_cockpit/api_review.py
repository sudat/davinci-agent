"""Acceptance-flow routes (task 51) — thin, like api.py, but additive.

Two operator surfaces the Gate V43-4a UX flow needs:

- ``GET /episodes/{id}/approval-sessions`` — pending approvals bundled
  into at most two normal blocking sessions (PRD 13.4) plus the decided
  facts, straight from the append-only ledger;
- ``POST /episodes/{id}/review-chat/apply`` — turn one natural-language
  correction into an applied command and its lineage-scoped partial
  rebuild plan (PRD 13.3);
- ``POST /episodes/{id}/review-chat/revert`` — restore the previous plan
  version as a NEW version (UX redesign 工程1 undo).

Kept in a separate router file (registered alongside ``api.router`` in
``app.py``) so the task-44 route file stays untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BeforeValidator, Field

from services.contracts.primitives import StrictModel
from services.episode_cockpit.backend import CockpitWorkspace

# Runtime imports (NOT TYPE_CHECKING): FastAPI resolves request-model
# annotations at decoration time via get_type_hints (task-44 convention).
from services.episode_cockpit.models import (  # noqa: TC001
    NonEmpty,
    Seconds,
    SequenceNumber,
)
from services.episode_cockpit.review_chat import ReviewCommandDraft

router = APIRouter()

type DraftSequence = Annotated[tuple[ReviewCommandDraft, ...], BeforeValidator(tuple)]


class ReviewChatApplyRequest(StrictModel):
    """POST /episodes/{id}/review-chat/apply — adopt a SAVED proposal set.

    Adoption authority is the SERVER-SAVED proposal set persisted at
    review-chat time (brief §5.3) — never the browser's echoed text or a
    recomputed hash. ``sequence`` (the review-chat response's sequence)
    names the saved set directly; ``drafts`` must then equal that set
    field-for-field. Old-client shape (``sequence`` omitted):
    ``drafts`` match an unconsumed saved set by full equality, and
    ``drafts=None`` re-derives deterministically and must match a saved
    unconsumed set — applying a never-previewed text is a typed 422
    ``proposal-not-found``. A plan-head move since the preview is a 409
    ``proposal-stale``; an already-applied set is 422
    ``proposal-consumed``.
    """

    text: NonEmpty
    at_seconds: Seconds | None = None
    drafts: DraftSequence | None = Field(default=None, min_length=1)
    sequence: SequenceNumber | None = None


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
        episode_id,
        text=request.text,
        at_seconds=request.at_seconds,
        drafts=request.drafts,
        sequence=request.sequence,
    )


@router.post("/episodes/{episode_id}/review-chat/revert")
def review_chat_revert(episode_id: str, workspace: Workspace) -> dict[str, object]:
    return workspace.revert_review_plan(episode_id)


__all__ = ["router"]
