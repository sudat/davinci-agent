"""Multi-command apply: echoed drafts → applied commands → ONE union rebuild.

V44-1 operator fix: one message may yield SEVERAL drafts, and the apply
route applies them ALL in order. The operator may only echo drafts the
interpreter produced: each echoed draft's ``command_id`` is re-derived
from its own fields (kind/target/delta/text) and must match — the client
can mint no command the pipeline never previewed. Every draft then goes
through the SAME ``apply_command`` (sealed 0C event + immutable plan
version per command), and the response's rebuild plan carries the UNION
of the per-command lineage stage sets, ordered by ``PIPELINE_STAGES``,
so the rebuild runner is spawned ONCE for the whole batch.
"""

from __future__ import annotations

from pathlib import Path

from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    PIPELINE_STAGES,
    AppliedCommand,
    RebuildPlan,
    ReviewChatError,
    ReviewCommandDraft,
    ReviewStoreLocation,
    _command_id,
    apply_command,
    plan_rebuild,
    record_applied_command,
)


def echoed_draft(draft: ReviewCommandDraft, text: str) -> ReviewCommandDraft:
    """Verify one client-echoed draft against the interpreter's id contract."""

    if draft.text != text:
        raise ReviewChatError(
            "draft-echo-mismatch",
            "echoed draft text differs from the request text; re-send the message",
        )
    expected = _command_id(
        draft.command_kind, draft.target_seconds, draft.seconds_delta, text
    )
    if draft.command_id != expected:
        raise ReviewChatError(
            "draft-echo-mismatch",
            f"command_id {draft.command_id!r} does not match the echoed command "
            f"fields (expected {expected!r}); only previewed drafts may be applied",
        )
    return draft


def ordered_stage_union(plans: list[RebuildPlan]) -> tuple[str, ...]:
    """Union of stage sets in ``PIPELINE_STAGES`` order (deterministic)."""

    union = set[str]().union(*(set(plan.stages) for plan in plans))
    return tuple(stage for stage in PIPELINE_STAGES if stage in union)


def union_rebuild_plan(plans: list[RebuildPlan]) -> RebuildPlan:
    """One plan covering every applied command; the first command is the
    named primary (the per-command plans ride in the applied log)."""

    primary = plans[0]
    stages = ordered_stage_union(plans)
    return primary.model_copy(
        update={
            "stages": stages,
            "excluded_stages": tuple(
                stage for stage in PIPELINE_STAGES if stage not in stages
            ),
        }
    )


def apply_drafts(
    drafts: list[ReviewCommandDraft], *, episode_dir: Path, store: ReviewStoreLocation
) -> tuple[list[AppliedCommand], RebuildPlan]:
    """Apply each confirmed draft in order; ONE union rebuild plan back."""

    if not drafts:
        raise ReviewChatError("drafts-empty", "at least one draft is required")
    applied: list[AppliedCommand] = []
    for draft in drafts:
        command = apply_command(draft, store=store)
        record_applied_command(episode_dir, command)
        applied.append(command)
    plans = [plan_rebuild(command, DEFAULT_LINEAGE) for command in applied]
    return applied, union_rebuild_plan(plans)


__all__ = [
    "apply_drafts",
    "echoed_draft",
    "ordered_stage_union",
    "union_rebuild_plan",
]
