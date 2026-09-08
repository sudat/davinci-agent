"""Pure Review Event reducer for the Phase-0C Spike.

``reduce`` folds a validated event stream over a base plan by replaying each
applied operator decision through the frozen Phase-0C compiler. It performs
zero file IO: the versioned-file writer owns persistence. Phase 1 will wrap
this same function with Production commit authority.
"""

# allow: SIZE_OK — the fold must validate every event kind (chain,
# sequence, version order) in ONE pass over the sealed stream; the
# plan_restored and policy_applied branches share that pass's version-chain
# invariants with decision_applied, and splitting any branch out would
# duplicate the checks.

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from services.compile.classify import ClassifyError
from services.compile.phase0c import CompileError, apply_command, build_ir
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    ItemIdSelector0C,
    ReviewCommand0C,
    TargetSelector0C,
)
from services.contracts.primitives import ArtifactRef, SourceFrameSpan
from services.foundation_io import canonical_model_bytes
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    POLICY_EVENT_KIND,
    RESTORED_EVENT_KIND,
    ReviewEvent0C,
    compute_event_id,
    event_policy_plan,
    event_proposal,
    event_restored_plan,
)
from services.review_command.models import (
    AdjustSourceSpanProposal0C,
    ApproveEditorialPlanProposal0C,
    ApproveRemainingProposal0C,
    RemoveSegmentProposal0C,
)
from services.review_command.validate import (
    ProposalValidationError,
    validate_proposal,
)

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C
    from services.review_command.models import EditCommandProposal0C

type ReduceErrorCode = Literal[
    "duplicate_event_conflict",
    "event_hash_mismatch",
    "sequence_conflict",
    "chain_conflict",
    "stale_version",
    "version_chain",
    "model_authored_decision",
    "decision_not_applicable",
    "apply_failed",
]

BASE_VERSION = "v1"


class ReduceConflictError(Exception):
    def __init__(self, code: ReduceErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReduceResult:
    plan: EditPlan0C
    ir: TimelineIr0C
    versions_applied: tuple[str, ...]
    deterministic_hash: str


def to_review_command(
    plan: EditPlan0C,
    proposal: EditCommandProposal0C,
    candidate_item_ids: tuple[str, ...],
) -> ReviewCommand0C:
    """Map a validated proposal onto the compiler's ReviewCommand0C shape."""

    target: TargetSelector0C
    new_span: SourceFrameSpan | None = None
    new_text: str | None = None
    language: Literal["ja", "en"] = "ja"
    if isinstance(proposal, RemoveSegmentProposal0C):
        target = ItemIdSelector0C(kind="item_id", item_id=candidate_item_ids[0])
    elif isinstance(proposal, AdjustSourceSpanProposal0C):
        target = proposal.target
        new_span = SourceFrameSpan(
            start_frame=proposal.new_span.start_frame,
            end_frame=proposal.new_span.end_frame,
            rate=plan.frame_rate,
        )
    else:
        target = proposal.target
        new_text = proposal.new_text
        language = proposal.language
    return ReviewCommand0C(
        command_id=proposal.proposal_id,
        language=language,
        instruction=f"proposal:{proposal.proposal_id}",
        operation=proposal.command_kind,
        base_plan_version=proposal.base_plan_version,
        target=target,
        new_span=new_span,
        new_text=new_text,
    )


def base_artifact_ref(base_plan: EditPlan0C) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=base_plan.artifact_id,
        sha256=hashlib.sha256(canonical_model_bytes(base_plan)).hexdigest(),
    )


def version_ir(base_plan: EditPlan0C, plan: EditPlan0C, version: int) -> TimelineIr0C:
    """Recompile the IR for a store version; shared by reduce and the writer."""

    return build_ir(
        plan,
        f"{base_plan.artifact_id}-ir-v{version}",
        (base_artifact_ref(base_plan),),
    )


def _require(condition: object, code: ReduceErrorCode, detail: str) -> None:
    if not condition:
        raise ReduceConflictError(code, detail)


def _next_version(event: ReviewEvent0C, applied_count: int) -> str:
    """The version an applied event must produce (shared version-chain rule)."""

    expected = f"v{applied_count + 2}"
    result = event.result_plan_version
    if result is None or result != expected:
        raise ReduceConflictError(
            "version_chain",
            f"event at sequence {event.sequence} must produce {expected}, "
            f"not {event.result_plan_version}",
        )
    return result


def _apply_decision(plan: EditPlan0C, event: ReviewEvent0C) -> EditPlan0C:
    proposal = event_proposal(event)
    try:
        outcome = validate_proposal(plan, proposal)
    except ProposalValidationError as error:
        code = "stale_version" if error.code == "stale_plan_version" else "decision_not_applicable"
        raise ReduceConflictError(code, str(error)) from error
    except ClassifyError as error:
        raise ReduceConflictError("decision_not_applicable", str(error)) from error
    _require(
        outcome.required_action == "apply" and outcome.classification == "clear",
        "decision_not_applicable",
        f"event {event.event_id} applies a proposal that now classifies "
        f"{outcome.classification}/{outcome.required_action}",
    )
    if isinstance(proposal, ApproveRemainingProposal0C | ApproveEditorialPlanProposal0C):
        return plan
    command = to_review_command(plan, proposal, outcome.candidate_item_ids)
    try:
        return apply_command(plan, command)
    except (CompileError, ValueError) as error:
        raise ReduceConflictError("apply_failed", str(error)) from error


def reduce(events: Sequence[ReviewEvent0C], base_plan: EditPlan0C) -> ReduceResult:
    """Deterministically fold an event stream over ``base_plan`` (pure)."""

    plan = base_plan
    current_version = BASE_VERSION
    seen: dict[str, bytes] = {}
    applied_versions: list[str] = []
    last_sequence = 0
    previous_hash: str | None = None
    for event in events:
        event_bytes = canonical_model_bytes(event)
        prior = seen.get(event.event_id)
        if prior is not None:
            _require(
                prior == event_bytes,
                "duplicate_event_conflict",
                f"event {event.event_id} reappears with different content",
            )
            continue
        _require(
            event.event_id == compute_event_id(event),
            "event_hash_mismatch",
            f"event id does not match canonical content at sequence {event.sequence}",
        )
        _require(
            event.sequence == last_sequence + 1,
            "sequence_conflict",
            f"expected sequence {last_sequence + 1}, got {event.sequence}",
        )
        expected_hash = GENESIS_EVENT_HASH if previous_hash is None else previous_hash
        _require(
            event.previous_event_hash == expected_hash,
            "chain_conflict",
            f"broken hash chain at sequence {event.sequence}",
        )
        seen[event.event_id] = event_bytes
        last_sequence = event.sequence
        previous_hash = event.event_id
        _require(
            event.base_plan_version == current_version,
            "stale_version",
            f"event at sequence {event.sequence} references base "
            f"{event.base_plan_version} after {current_version} was committed",
        )
        if event.kind == "decision_applied":
            result = _next_version(event, len(applied_versions))
            _require(
                event.actor_intent == "operator",
                "model_authored_decision",
                "models propose; only operator decisions may be applied",
            )
            plan = _apply_decision(plan, event)
            current_version = result
            applied_versions.append(result)
        elif event.kind == RESTORED_EVENT_KIND:
            result = _next_version(event, len(applied_versions))
            _require(
                event.actor_intent == "operator",
                "model_authored_decision",
                "models propose; only operator decisions may restore a plan version",
            )
            plan = event_restored_plan(event)
            current_version = result
            applied_versions.append(result)
        elif event.kind == POLICY_EVENT_KIND:
            result = _next_version(event, len(applied_versions))
            _require(
                event.actor_intent == "operator",
                "model_authored_decision",
                "models propose; only operator decisions may commit a policy plan",
            )
            plan = event_policy_plan(event)
            current_version = result
            applied_versions.append(result)
        elif event.kind == "command_deferred":
            # Deferred decisions must parse and provably never mutate the plan.
            event_proposal(event)
    final_version = len(applied_versions) + 1
    ir = version_ir(base_plan, plan, final_version)
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(plan))
    digest.update(canonical_model_bytes(ir))
    digest.update(",".join(applied_versions).encode())
    return ReduceResult(
        plan=plan,
        ir=ir,
        versions_applied=tuple(applied_versions),
        deterministic_hash=digest.hexdigest(),
    )
