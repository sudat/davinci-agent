"""Consultation journal store — pre-plan consultations (UX phase 2.5 slice-1).

Append-only jsonl journals under ``episodes/<id>/consultation/`` following
the review-store record discipline (``review_proposals.py``): StrictModel
records, canonical bytes, one append-only line per event, and a full
reload that reconstructs state from the journal alone. This is runtime
episode-dir state, NEVER a new authoritative artifact family.

Budget accounting is cumulative for the episode's consultation phase and
is NEVER reset by a retry/regeneration: every generation appends one
budget-event line and ``budget_used`` sums the WHOLE journal. Limits come
from ``config/consultation.json`` with the defaults (LLM calls 6 /
intervals 3 / wall seconds 600) as fallback; ``cost_display`` stays
"unmeasured" (slice 1 measures no cost).
"""

# allow: SIZE_OK — one consultation-journal concern per file (the record
# family + its append/reload + the budget accounting + the pinned view
# builder), the review_proposals.py shape; splitting the models from their
# loaders would fork the same cohesive concern the task pinned to THIS
# module, so the ceiling is declared instead.


from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.editorial.models import AdoptedPolicyScopeV1, AdoptedPolicySummaryV1
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import RebuildRequestEntry
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.job_runner.state_models import JobSnapshot, StageRunRow

# allow: SIZE_OK — one storage concern per file (the five journal record
# models, the append/reload journal primitives, and the cumulative budget
# accounting over those SAME journals); the record classes and their
# journals stay together exactly like ``review_proposals.py``.

CONSULTATION_DIR_NAME = "consultation"
CONSULTATIONS_NAME = "consultations.jsonl"
PROPOSALS_NAME = "proposals.jsonl"
JUDGMENTS_NAME = "judgments.jsonl"
BUDGET_NAME = "budget.jsonl"
OUTCOMES_NAME = "policy-outcomes.jsonl"
REBUILD_LOG_NAME = "rebuild-requests.jsonl"

CONFIG_RELATIVE = Path("config") / "consultation.json"
_CONFIG_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LLM_CALLS_LIMIT = 6
DEFAULT_INTERVALS_LIMIT = 3
DEFAULT_WALL_SECONDS_LIMIT = 600.0

type ConsultationDecision = Literal["adopt", "revise", "reject", "both_wrong", "delegate"]

# Strict models never coerce list→tuple; pin the review_proposals BeforeValidator convention.
type StringSequence = Annotated[tuple[str, ...], BeforeValidator(tuple)]


class ConsultationScope(StrictModel):
    """Partial-adoption record: 構成だけ採用/見た目未確認 is expressible.

    Adoption here is NOT plan approval, NOT publish approval, and NOT a
    permanent style save — the three booleans record WHICH aspects the
    operator actually adopted, everything unchecked stays unconfirmed.
    """

    composition: bool = False
    appearance: bool = False
    audio: bool = False


class ConsultationProposalDetails(StrictModel):
    """The fixed ten-field proposal body (pinned consultation contract)."""

    audience_message: str
    structure: str
    duration_estimate: str
    candidate_scenes: StringSequence = ()
    subtitle_policy: str
    audio_policy: str
    tempo_policy: str
    reference_mapping: str
    unused_reasons: str
    unconfirmed: StringSequence = ()


class ConsultationProposalV1(StrictModel):
    """One proposal: ``proposal_id`` is server-assigned (prop-1, prop-2)."""

    proposal_id: str
    title: str
    summary: str
    details: ConsultationProposalDetails


class ConsultationRecordV1(StrictModel):
    """One consultation: the operator message a proposal set answers."""

    schema_version: Literal["cockpit-consultation-v1"] = "cockpit-consultation-v1"
    consultation_id: str
    created_at: str
    message: str


class ConsultationProposalSetV1(StrictModel):
    """The proposal set currently on the table for one consultation.

    Regeneration appends a NEW set (append-only); readers take the LATEST
    set per consultation. At most TWO proposals — ONE normally, TWO only
    when the direction genuinely splits.
    """

    schema_version: Literal["cockpit-consultation-proposals-v1"] = (
        "cockpit-consultation-proposals-v1"
    )
    consultation_id: str
    created_at: str
    proposals: tuple[ConsultationProposalV1, ...] = Field(min_length=1, max_length=2)


class ConsultationJudgmentV1(StrictModel):
    """One append-only judgment on a consultation (or one of its proposals).

    ``proposal_id=None`` judges the consultation as a whole
    (``both_wrong``/``delegate``). Judgments are never rewritten.
    """

    schema_version: Literal["cockpit-consultation-judgment-v1"] = (
        "cockpit-consultation-judgment-v1"
    )
    judgment_id: str
    consultation_id: str
    proposal_id: str | None = None
    decision: ConsultationDecision
    scope: ConsultationScope
    note: str | None = None
    created_at: str


class AdoptedPolicyV1(StrictModel):
    """The latest adoptable operator decision joined with its proposal.

    Deterministic extraction (no LLM): the LATEST judgment with decision
    adopt|revise and at least one scope flag, joined with its proposal's
    ten text fields plus the judgment note. A NEWER reject/both_wrong (or
    an empty scope, or a missing proposal) yields NO policy — the operator
    withdrew, and the planning path must not reuse the older案.
    """

    schema_version: Literal["cockpit-consultation-policy-v1"] = (
        "cockpit-consultation-policy-v1"
    )
    consultation_id: str
    judgment_id: str
    proposal_id: str
    decision: Literal["adopt", "revise"]
    scope: ConsultationScope
    audience_message: str
    structure: str
    duration_estimate: str
    candidate_scenes: StringSequence = ()
    subtitle_policy: str
    audio_policy: str
    tempo_policy: str
    reference_mapping: str
    unused_reasons: str
    unconfirmed: StringSequence = ()
    note: str | None = None


type PolicyOutcomeStatus = Literal["honored", "failed"]


class ConsultationPolicyOutcomeV1(StrictModel):
    """One selection-rebuild verdict for an adopted policy (append-only).

    ``honored`` carries the committed plan version; ``failed`` carries no
    version and the honest reason (never a silently dropped policy). The
    ``note`` records what was actually verified (never a claim beyond it).
    """

    schema_version: Literal["cockpit-consultation-policy-outcome-v1"] = (
        "cockpit-consultation-policy-outcome-v1"
    )
    outcome_id: str
    consultation_id: str
    judgment_id: str
    proposal_id: str | None = None
    plan_version: str | None = None
    status: PolicyOutcomeStatus
    reasons: StringSequence = ()
    note: str | None = None
    created_at: str


class ConsultationBudgetEventV1(StrictModel):
    """One generation's consumption; cumulative state = the journal sum."""

    schema_version: Literal["cockpit-consultation-budget-v1"] = (
        "cockpit-consultation-budget-v1"
    )
    consultation_id: str
    llm_calls: int = Field(ge=0)
    intervals: int = Field(ge=0)
    wall_seconds: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    created_at: str


class ConsultationBudgetLimits(StrictModel):
    """Limits loaded from ``config/consultation.json`` (defaults fallback).

    ``schema_version`` is part of the model so the shipped config file
    (which carries it, following the config convention) actually
    validates — an unlisted extra key would fall back to the defaults
    silently and make the file dead.
    """

    schema_version: Literal["cockpit-consultation-config-v1"] = (
        "cockpit-consultation-config-v1"
    )
    llm_calls_limit: int = DEFAULT_LLM_CALLS_LIMIT
    intervals_limit: int = DEFAULT_INTERVALS_LIMIT
    wall_seconds_limit: Annotated[float, BeforeValidator(float)] = (
        DEFAULT_WALL_SECONDS_LIMIT
    )


class ConsultationBudgetUsed(NamedTuple):
    llm_calls: int
    intervals: int
    wall_seconds: float


def now_stamp() -> str:
    return datetime.now(UTC).isoformat()


def _consultation_dir(episode_dir: Path) -> Path:
    return episode_dir / CONSULTATION_DIR_NAME


def _append(path: Path, record: StrictModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(record) + b"\n")


def _load_jsonl(path: Path, model: type, code: str) -> list:
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(model.model_validate_json(line))
        except ValidationError as error:
            raise CockpitUnprocessableError(code, f"unparsable line in {path}") from error
    return entries


def append_consultation(episode_dir: Path, record: ConsultationRecordV1) -> None:
    _append(_consultation_dir(episode_dir) / CONSULTATIONS_NAME, record)


def load_consultations(episode_dir: Path) -> list[ConsultationRecordV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / CONSULTATIONS_NAME,
        ConsultationRecordV1,
        "consultation-log-corrupt",
    )


def append_proposal_set(episode_dir: Path, proposal_set: ConsultationProposalSetV1) -> None:
    _append(_consultation_dir(episode_dir) / PROPOSALS_NAME, proposal_set)


def load_latest_proposal_sets(episode_dir: Path) -> dict[str, ConsultationProposalSetV1]:
    """Latest proposal set per consultation (regeneration supersedes)."""

    sets: dict[str, ConsultationProposalSetV1] = {}
    for saved in _load_jsonl(
        _consultation_dir(episode_dir) / PROPOSALS_NAME,
        ConsultationProposalSetV1,
        "consultation-log-corrupt",
    ):
        sets[saved.consultation_id] = saved
    return sets


def append_judgment(episode_dir: Path, judgment: ConsultationJudgmentV1) -> None:
    _append(_consultation_dir(episode_dir) / JUDGMENTS_NAME, judgment)


def load_judgments(episode_dir: Path) -> list[ConsultationJudgmentV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / JUDGMENTS_NAME,
        ConsultationJudgmentV1,
        "consultation-log-corrupt",
    )


def latest_adopted_policy(episode_dir: Path) -> AdoptedPolicyV1 | None:
    """The latest adoptable judgment joined with its proposal (None = none).

    Latest judgment wins: adopt|revise with a non-empty scope and a
    resolvable proposal yields a policy; a newer reject/both_wrong, an
    empty scope, a whole-consultation judgment (no proposal), or a missing
    proposal yields None — the operator withdrew and nothing is reused.
    Never raises for journal states (corrupt lines still raise, like every
    other reader here).
    """

    judgments = load_judgments(episode_dir)
    if not judgments:
        return None
    latest = judgments[-1]
    if latest.decision not in ("adopt", "revise"):
        return None
    if not (
        latest.scope.composition or latest.scope.appearance or latest.scope.audio
    ):
        return None
    if latest.proposal_id is None:
        return None
    proposal_set = load_latest_proposal_sets(episode_dir).get(latest.consultation_id)
    proposal = next(
        (
            candidate
            for candidate in (proposal_set.proposals if proposal_set is not None else ())
            if candidate.proposal_id == latest.proposal_id
        ),
        None,
    )
    if proposal is None:
        return None
    details = proposal.details
    return AdoptedPolicyV1(
        consultation_id=latest.consultation_id,
        judgment_id=latest.judgment_id,
        proposal_id=proposal.proposal_id,
        decision=latest.decision,
        scope=latest.scope,
        audience_message=details.audience_message,
        structure=details.structure,
        duration_estimate=details.duration_estimate,
        candidate_scenes=details.candidate_scenes,
        subtitle_policy=details.subtitle_policy,
        audio_policy=details.audio_policy,
        tempo_policy=details.tempo_policy,
        reference_mapping=details.reference_mapping,
        unused_reasons=details.unused_reasons,
        unconfirmed=details.unconfirmed,
        note=latest.note,
    )


def policy_summary(policy: AdoptedPolicyV1) -> AdoptedPolicySummaryV1:
    """The compact director-input summary of an adopted policy."""

    return AdoptedPolicySummaryV1(
        decision=policy.decision,
        scope=AdoptedPolicyScopeV1(
            composition=policy.scope.composition,
            appearance=policy.scope.appearance,
            audio=policy.scope.audio,
        ),
        structure=policy.structure,
        candidate_scenes=policy.candidate_scenes,
        subtitle_policy=policy.subtitle_policy,
        audio_policy=policy.audio_policy,
        tempo_policy=policy.tempo_policy,
        unused_reasons=policy.unused_reasons,
        unconfirmed=policy.unconfirmed,
        note=policy.note,
    )


def append_policy_outcome(episode_dir: Path, outcome: ConsultationPolicyOutcomeV1) -> None:
    _append(_consultation_dir(episode_dir) / OUTCOMES_NAME, outcome)


def load_policy_outcomes(episode_dir: Path) -> list[ConsultationPolicyOutcomeV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / OUTCOMES_NAME,
        ConsultationPolicyOutcomeV1,
        "consultation-log-corrupt",
    )


def load_policy_rebuild_entries(episode_dir: Path) -> list[RebuildRequestEntry]:
    """Judgment-linked rebuild entries, tolerant like the status view."""

    try:
        lines = (episode_dir / REBUILD_LOG_NAME).read_bytes().splitlines()
    except OSError:
        return []
    entries: list[RebuildRequestEntry] = []
    for line in lines:
        try:
            entries.append(RebuildRequestEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def _selection_run_state(
    stage_runs: Sequence[StageRunRow], run_id: str | None
) -> Literal["failed", "succeeded", "running", "requested"]:
    """State of one spawned selection rebuild from cockpit stage rows.

    ``requested`` means spawned but with no sign of life yet (no stage
    rows for the run) — the reservation is the only evidence. Only actual
    rows promote it to running/succeeded/failed.
    """

    if run_id is None:
        return "requested"
    seen = False
    failed = False
    preview_done = False
    for row in stage_runs:
        if row.run_id != run_id:
            continue
        seen = True
        if row.status == "failed_blocked":
            failed = True
        if row.stage_name == "preview" and row.status == "succeeded":
            preview_done = True
    if failed:
        return "failed"
    if preview_done:
        return "succeeded"
    if seen:
        return "running"
    return "requested"


def derive_policy_rebuild(
    episode_dir: Path,
    policy: AdoptedPolicyV1 | None,
    stage_runs: Sequence[StageRunRow] = (),
) -> dict[str, object]:
    """The consultation view's ``rebuild`` object for one adopted policy.

    Derived deterministically: a reservation carrying this policy's
    judgment is ``requested``; a spawned run is ``running``/``succeeded``/
    ``failed`` from its cockpit stage rows; a failed outcome (and no later
    honored one) is ``failed`` with the honest reason. ``detail`` never
    claims beyond what was verified.
    """

    if policy is None:
        linked: list[RebuildRequestEntry] = []
        outcomes: list[ConsultationPolicyOutcomeV1] = []
    else:
        linked = [
            entry
            for entry in load_policy_rebuild_entries(episode_dir)
            if entry.judgment_id == policy.judgment_id
        ]
        outcomes = [
            outcome
            for outcome in load_policy_outcomes(episode_dir)
            if outcome.judgment_id == policy.judgment_id
        ]
    honored = [outcome for outcome in outcomes if outcome.status == "honored"]
    if outcomes and outcomes[-1].status == "failed":
        last = outcomes[-1]
        detail = "; ".join(last.reasons) or last.note or "方針の反映に失敗しました。"
        return {"status": "failed", "target_version": None, "detail": detail}
    target = honored[-1].plan_version if honored else None
    spawns = [entry for entry in linked if entry.spawned]
    if not linked:
        return {
            "status": "none",
            "target_version": target,
            "detail": (
                "採用中の方針はありません。"
                if policy is None
                else "この方針の再生成は要求されていません。"
            ),
        }
    state = _selection_run_state(stage_runs, spawns[-1].run_id) if spawns else "requested"
    if state == "requested":
        return {
            "status": "requested",
            "target_version": target,
            "detail": (
                "再生成を起動しました。実行開始待ち。"
                if spawns
                else "再生成待ち (起動予約済み)。"
            ),
        }
    if state == "failed":
        return {
            "status": "failed",
            "target_version": target,
            "detail": "再生成の実行が失敗しました。相談へ戻って方針を見直せます。",
        }
    if state == "succeeded":
        return {
            "status": "succeeded",
            "target_version": target,
            "detail": (
                "反映の検証は構造検証のみ。"
                if honored
                else "再生成は完了しましたが方針の確定記録がありません。"
            ),
        }
    return {
        "status": "running",
        "target_version": target,
        "detail": (
            "方針を反映した版を生成中です。反映の検証は構造検証のみ。"
            if honored
            else "再生成を実行中です。"
        ),
    }


def selection_rebuild_active(
    episode_dir: Path, stage_runs: Sequence[StageRunRow] = ()
) -> bool:
    """True while a judgment-linked selection rebuild is still unresolved.

    Orphan reservations (spawn never happened) do NOT block: a subsequent
    schedule supersedes them and the latest policy is what it runs. A
    spawned run counts as active until its stage rows prove a terminal
    state — a spawn with no rows yet is presumed alive, never double
    scheduled.
    """

    linked = [
        entry
        for entry in load_policy_rebuild_entries(episode_dir)
        if entry.judgment_id is not None
    ]
    spawns = [entry for entry in linked if entry.spawned]
    if not spawns:
        return False
    return _selection_run_state(stage_runs, spawns[-1].run_id) in (
        "running",
        "requested",
    )


def append_budget_event(episode_dir: Path, event: ConsultationBudgetEventV1) -> None:
    _append(_consultation_dir(episode_dir) / BUDGET_NAME, event)


def budget_used(episode_dir: Path) -> ConsultationBudgetUsed:
    """Cumulative counters — the sum of the WHOLE budget journal (no reset)."""

    events = _load_jsonl(
        _consultation_dir(episode_dir) / BUDGET_NAME,
        ConsultationBudgetEventV1,
        "consultation-log-corrupt",
    )
    return ConsultationBudgetUsed(
        llm_calls=sum(event.llm_calls for event in events),
        intervals=sum(event.intervals for event in events),
        wall_seconds=sum(event.wall_seconds for event in events),
    )


def consume_budget(
    episode_dir: Path,
    consultation_id: str,
    *,
    llm_calls: int,
    intervals: int,
    wall_seconds: float,
) -> None:
    append_budget_event(
        episode_dir,
        ConsultationBudgetEventV1(
            consultation_id=consultation_id,
            llm_calls=llm_calls,
            intervals=intervals,
            wall_seconds=wall_seconds,
            created_at=now_stamp(),
        ),
    )


def ensure_budget_available(episode_dir: Path, limits: ConsultationBudgetLimits) -> None:
    """Typed 422 BEFORE any LLM call when any limit would be exceeded."""

    used = budget_used(episode_dir)
    exhausted = (
        used.llm_calls + 1 > limits.llm_calls_limit
        or used.intervals + 1 > limits.intervals_limit
        or used.wall_seconds >= limits.wall_seconds_limit
    )
    if exhausted:
        raise CockpitUnprocessableError(
            "consultation-budget-exhausted",
            f"consultation budget exhausted: {used.llm_calls}/"
            f"{limits.llm_calls_limit} LLM calls, {used.intervals}/"
            f"{limits.intervals_limit} intervals, {used.wall_seconds:.1f}/"
            f"{limits.wall_seconds_limit:.1f}s wall time used",
        )


def load_budget_limits(config_path: Path | None = None) -> ConsultationBudgetLimits:
    """Tolerant config read: absent/malformed file → the pinned defaults."""

    path = config_path if config_path is not None else _CONFIG_ROOT / CONFIG_RELATIVE
    try:
        parsed: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return ConsultationBudgetLimits()
    if not isinstance(parsed, dict):
        return ConsultationBudgetLimits()
    try:
        return ConsultationBudgetLimits.model_validate(parsed)
    except ValidationError:
        return ConsultationBudgetLimits()


def require_consultation(episode_dir: Path, consultation_id: str) -> ConsultationRecordV1:
    for record in load_consultations(episode_dir):
        if record.consultation_id == consultation_id:
            return record
    raise CockpitNotFoundError(
        "consultation-not-found", f"no consultation {consultation_id} in the journal"
    )


def require_proposal(
    episode_dir: Path, consultation_id: str, proposal_id: str
) -> ConsultationProposalV1:
    proposal_set = load_latest_proposal_sets(episode_dir).get(consultation_id)
    if proposal_set is not None:
        for proposal in proposal_set.proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
    raise CockpitUnprocessableError(
        "consultation-proposal-not-found",
        f"consultation {consultation_id} has no proposal {proposal_id} on the table",
    )


def consultation_view(
    episode_dir: Path,
    record: ConsultationRecordV1,
    limits: ConsultationBudgetLimits,
    *,
    snapshot: JobSnapshot | None = None,
) -> dict[str, object]:
    """The pinned consultation object (no schema_version noise on the wire)."""

    proposal_set = load_latest_proposal_sets(episode_dir).get(record.consultation_id)
    judgments = [
        judgment
        for judgment in load_judgments(episode_dir)
        if judgment.consultation_id == record.consultation_id
    ]
    used = budget_used(episode_dir)
    policy = latest_adopted_policy(episode_dir)
    stage_runs = snapshot.stage_runs if snapshot is not None else ()
    outcomes = [
        {**outcome.model_dump(mode="json"), "kind": "policy_outcome"}
        for outcome in load_policy_outcomes(episode_dir)
        if outcome.consultation_id == record.consultation_id
    ]
    return {
        "consultation_id": record.consultation_id,
        "created_at": record.created_at,
        "message": record.message,
        "proposals": [
            proposal.model_dump(mode="json")
            for proposal in (proposal_set.proposals if proposal_set else ())
        ],
        "judgments": [
            judgment.model_dump(
                mode="json", exclude={"schema_version", "consultation_id"}
            )
            for judgment in judgments
        ],
        "budget": {
            "llm_calls_used": used.llm_calls,
            "llm_calls_limit": limits.llm_calls_limit,
            "intervals_used": used.intervals,
            "intervals_limit": limits.intervals_limit,
            "wall_seconds_used": used.wall_seconds,
            "wall_seconds_limit": limits.wall_seconds_limit,
            "cost_display": "unmeasured",
        },
        "policy": {
            "adopted": policy.model_dump(mode="json") if policy is not None else None
        },
        "rebuild": derive_policy_rebuild(episode_dir, policy, stage_runs),
        "policy_outcomes": outcomes,
    }


__all__ = [
    "BUDGET_NAME",
    "CONFIG_RELATIVE",
    "CONSULTATIONS_NAME",
    "CONSULTATION_DIR_NAME",
    "DEFAULT_INTERVALS_LIMIT",
    "DEFAULT_LLM_CALLS_LIMIT",
    "DEFAULT_WALL_SECONDS_LIMIT",
    "JUDGMENTS_NAME",
    "OUTCOMES_NAME",
    "PROPOSALS_NAME",
    "REBUILD_LOG_NAME",
    "AdoptedPolicyV1",
    "ConsultationBudgetEventV1",
    "ConsultationBudgetLimits",
    "ConsultationBudgetUsed",
    "ConsultationDecision",
    "ConsultationJudgmentV1",
    "ConsultationPolicyOutcomeV1",
    "ConsultationProposalDetails",
    "ConsultationProposalSetV1",
    "ConsultationProposalV1",
    "ConsultationRecordV1",
    "ConsultationScope",
    "PolicyOutcomeStatus",
    "append_budget_event",
    "append_consultation",
    "append_judgment",
    "append_policy_outcome",
    "append_proposal_set",
    "budget_used",
    "consultation_view",
    "consume_budget",
    "derive_policy_rebuild",
    "ensure_budget_available",
    "latest_adopted_policy",
    "load_budget_limits",
    "load_consultations",
    "load_judgments",
    "load_latest_proposal_sets",
    "load_policy_outcomes",
    "load_policy_rebuild_entries",
    "now_stamp",
    "policy_summary",
    "require_consultation",
    "require_proposal",
    "selection_rebuild_active",
]
