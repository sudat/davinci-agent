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
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.foundation_io import canonical_model_bytes

# allow: SIZE_OK — one storage concern per file (the five journal record
# models, the append/reload journal primitives, and the cumulative budget
# accounting over those SAME journals); the record classes and their
# journals stay together exactly like ``review_proposals.py``.

CONSULTATION_DIR_NAME = "consultation"
CONSULTATIONS_NAME = "consultations.jsonl"
PROPOSALS_NAME = "proposals.jsonl"
JUDGMENTS_NAME = "judgments.jsonl"
BUDGET_NAME = "budget.jsonl"

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
    episode_dir: Path, record: ConsultationRecordV1, limits: ConsultationBudgetLimits
) -> dict[str, object]:
    """The pinned consultation object (no schema_version noise on the wire)."""

    proposal_set = load_latest_proposal_sets(episode_dir).get(record.consultation_id)
    judgments = [
        judgment
        for judgment in load_judgments(episode_dir)
        if judgment.consultation_id == record.consultation_id
    ]
    used = budget_used(episode_dir)
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
    "PROPOSALS_NAME",
    "ConsultationBudgetEventV1",
    "ConsultationBudgetLimits",
    "ConsultationBudgetUsed",
    "ConsultationDecision",
    "ConsultationJudgmentV1",
    "ConsultationProposalDetails",
    "ConsultationProposalSetV1",
    "ConsultationProposalV1",
    "ConsultationRecordV1",
    "ConsultationScope",
    "append_budget_event",
    "append_consultation",
    "append_judgment",
    "append_proposal_set",
    "budget_used",
    "consultation_view",
    "consume_budget",
    "ensure_budget_available",
    "load_budget_limits",
    "load_consultations",
    "load_judgments",
    "load_latest_proposal_sets",
    "now_stamp",
    "require_consultation",
    "require_proposal",
]
