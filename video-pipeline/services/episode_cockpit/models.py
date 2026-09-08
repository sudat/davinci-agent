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

type ReviewReactionKind = Literal["choice-a", "choice-b", "both-different", "continuation"]

type ProposalOutcome = Literal["applied", "chosen", "rejected"]

type ProposalKind = Literal["command-bundle", "alternatives"]


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


class IntakeRecordV1(StrictModel):
    """Intake fact record (task 7): what the runner needs from intake.

    Written atomically next to the brief at create time so the detached
    one-shot runner can resolve the ACTUAL camera source folder without
    re-deriving it from the episode id; the BriefDraft schema stays
    untouched (this is a cockpit-owned communication file, never job
    state — the StateStore remains the only authority).
    """

    schema_version: Literal["cockpit-intake-v1"] = "cockpit-intake-v1"
    episode_id: Identifier
    source_folder: NonEmpty
    brief_text: NonEmpty
    created_at: NonEmpty


class FrameMaterial(StrictModel):
    """One checked video STILL (工程2 rework #1, U02 実映像確認): the frame
    file extracted from the episode preview (fallback: intake source) around
    the player position, traceable by 対象 (path) ・時点 (at_seconds) ・元版
    (source).

    Honesty: a still is ONE frame — it is NOT an audio or whole-video
    verification, and no response may claim it is. Frame PIXELS are
    config-gated egress (PRD v4.4 §23): they may be attached to a model
    call only when ``review_frame_materials.enabled`` is true in
    ``config/editorial-runtime.json``; the records themselves (path/
    position/source) stay local traceability data.
    """

    path: NonEmpty
    at_seconds: Seconds
    source: NonEmpty


class ReviewChatEntry(StrictModel):
    """One append-only raw review message (review-events.jsonl convention).

    ``investigated`` is a DATA flag meaning "cause-investigation materials
    were gathered" (transcript/scene text only — 工程1 never watches the
    actual video/audio). It is never a cause-identified claim; the DISPLAY
    contract lives in the review-chat response's ``investigation_state``.

    工程2 (additive, backward compatible): ``reaction`` is the closed-set
    classification of a message that reacts to a proposal set instead of
    describing a new correction; ``in_response_to_set`` names the proposal
    set's ``sequence`` it responds to. A choice records the CHOICE FACT
    only — never a reason (none is asked, none is invented).

    工程2 rework (additive, backward compatible): ``frame_materials``
    records the video STILLS this message's investigation actually checked
    (U02 実映像確認への接続 — stills are never an audio/whole-video
    verification); None on legacy lines = the gated feature was absent.
    """

    schema_version: Literal["cockpit-review-chat-v1"] = "cockpit-review-chat-v1"
    sequence: SequenceNumber
    text: NonEmpty
    at_seconds: Seconds | None = None
    investigated: bool = False
    hypothesis: str | None = None
    reaction: ReviewReactionKind | None = None
    in_response_to_set: SequenceNumber | None = None
    frame_materials: tuple[FrameMaterial, ...] | None = None


class RebuildRequestEntry(StrictModel):
    """One append-only rebuild intent record.

    ``marker``/``spawned`` are the revert-rebuild bookkeeping (UX redesign
    工程1): a revert appends its entry only AFTER the spawn attempt with the
    truthful ``spawned`` value, so a resend can detect an un-launched revert
    and relaunch the SAME step (never a second walk-back). Legacy lines and
    non-revert rebuild intents carry the defaults (``None``/``False``).

    工程2P explicit linkage (additive, backward compatible): the chain
    予約(sequence)→起動(run_id)→成果(target_version) is readable WITHOUT
    inference. ``run_id`` is set ONLY on entries that record a spawn (the
    spawning parent pre-generates the id and passes ``--run-id`` so the
    runner's log events and stage rows name the SAME run).
    ``target_version`` is the plan version the rebuild renders (from the
    applied command's ``result_plan_version``, or the revert's
    ``new_version``); absent when not determinable. A spawn entry's
    ``reserves_sequence`` names the reservation entry it launched.
    A pre-spawn reservation therefore reads ``spawned=False, run_id=None``
    and survives reload for restore.

    Consultation slice 2 (additive, backward compatible): ``judgment_id``
    links a selection rebuild to the adopted consultation judgment that
    requested it, so the consultation view derives the rebuild state
    deterministically per policy. Absent (None) on every legacy line.
    """

    schema_version: Literal["cockpit-rebuild-request-v1"] = "cockpit-rebuild-request-v1"
    sequence: SequenceNumber
    stage_hint: NonEmpty | None = None
    marker: NonEmpty | None = None
    spawned: bool = False
    run_id: NonEmpty | None = None
    target_version: NonEmpty | None = None
    reserves_sequence: SequenceNumber | None = None
    judgment_id: NonEmpty | None = None


class ReviewProposalConsumed(StrictModel):
    """One append-only consumption record: a saved proposal set was applied,
    chosen, or rejected.

    Written after a SUCCESSFUL apply (or, for 工程2 rejections, by the
    reaction route); resending a consumed set is a typed
    ``proposal-consumed`` 422. ``outcome`` is additive: absent (legacy
    lines) means ``applied``. ANY outcome consumes the set — a chosen or
    rejected set can never be applied later (「やっぱり前の案」 goes through
    a NEW preview, never the consumed set).
    """

    schema_version: Literal["cockpit-review-proposal-consumed-v1"] = (
        "cockpit-review-proposal-consumed-v1"
    )
    sequence: SequenceNumber
    set_sequence: SequenceNumber
    created_at: NonEmpty
    outcome: ProposalOutcome | None = None


__all__ = [
    "ApprovalExecuteRequest",
    "BriefDraft",
    "BriefPutRequest",
    "EpisodeCreateRequest",
    "IntakeRecordV1",
    "ProposalKind",
    "ProposalOutcome",
    "RebuildRequest",
    "RebuildRequestEntry",
    "ReferenceRegisterRequest",
    "ReviewChatEntry",
    "ReviewChatRequest",
    "ReviewProposalConsumed",
    "ReviewReactionKind",
]
