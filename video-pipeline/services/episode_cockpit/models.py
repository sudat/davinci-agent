"""Boundary models for the episode cockpit (task 44).

Request models parse untrusted HTTP bodies exactly once (strict, frozen,
extra keys forbidden — the contracts ``StrictModel`` discipline). The
workspace-file models (``BriefDraft``, ``ReviewChatEntry``,
``RebuildRequestEntry``) are the cockpit-owned files under
``episodes_root/<episode_id>/``; job state itself stays in the existing
job-runner StateStore — the cockpit never keeps a second state machine.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from services.contracts.primitives import Identifier, StrictModel

type NonEmpty = Annotated[str, Field(min_length=1, strict=True)]

type Seconds = Annotated[float, Field(ge=0.0, strict=True)]

type SequenceNumber = Annotated[int, Field(ge=1, strict=True)]


class EpisodeCreateRequest(StrictModel):
    """POST /episodes — intake: source folder plus natural-language brief."""

    source_folder: NonEmpty
    brief_text: NonEmpty


class BriefPutRequest(StrictModel):
    """PUT /episodes/{id}/brief — replace the draft brief text."""

    brief_text: NonEmpty


class ReviewChatRequest(StrictModel):
    """POST /episodes/{id}/review-chat — raw review message (NLU is task 47)."""

    text: NonEmpty
    at_seconds: Seconds | None = None


class RebuildRequest(StrictModel):
    """POST /episodes/{id}/rebuild — record rebuild intent (scheduling is task 47)."""

    stage_hint: NonEmpty | None = None


class ApprovalExecuteRequest(StrictModel):
    """POST /episodes/{id}/approvals/{approval_id} — decision on an existing record."""

    decision: Literal["approve", "reject"]
    actor_id: Identifier


class ReferenceRegisterRequest(StrictModel):
    """POST /references — register a local reference file into the library."""

    path: NonEmpty
    source_id: Identifier | None = None


class BriefDraft(StrictModel):
    """Episode brief draft file (episode-brief propose()/draft semantics).

    The full ``EpisodeBriefV1`` requires Editorial-Director fields that do
    not exist before analysis; intake stores the raw natural-language
    draft, and only a draft is editable over HTTP (a non-draft brief is a
    typed conflict, mirroring ``episode_brief.propose`` transitions).
    """

    schema_version: Literal["cockpit-brief-draft-v1"] = "cockpit-brief-draft-v1"
    episode_id: Identifier
    status: Literal["draft"] = "draft"
    brief_text: NonEmpty


class ReviewChatEntry(StrictModel):
    """One append-only raw review message (review-events.jsonl convention)."""

    schema_version: Literal["cockpit-review-chat-v1"] = "cockpit-review-chat-v1"
    sequence: SequenceNumber
    text: NonEmpty
    at_seconds: Seconds | None = None


class RebuildRequestEntry(StrictModel):
    """One append-only rebuild intent record."""

    schema_version: Literal["cockpit-rebuild-request-v1"] = "cockpit-rebuild-request-v1"
    sequence: SequenceNumber
    stage_hint: NonEmpty | None = None


__all__ = [
    "ApprovalExecuteRequest",
    "BriefDraft",
    "BriefPutRequest",
    "EpisodeCreateRequest",
    "RebuildRequest",
    "RebuildRequestEntry",
    "ReferenceRegisterRequest",
    "ReviewChatEntry",
    "ReviewChatRequest",
]
