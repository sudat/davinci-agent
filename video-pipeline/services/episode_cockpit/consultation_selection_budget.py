"""Selection-attempt budget ledger (slice2 P1): one cumulative episode budget.

Proposal generation and selection rebuilds consume the SAME episode
totals (calls + wall time); the sample-preview allowance (30 s) is shared
across selection attempts. This journal is runtime state, NEVER an
authoritative artifact. A reservation without its matching settle counts
as still reserved, so a crash never replays a paid attempt for free.
"""

# allow: SIZE_OK — one ledger concern per file (the entry model + the
# conservative fold + the reserve/settle/check helpers over those SAME
# entries); the record classes and their journal stay together exactly
# like consultation_store.py.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C

SELECTION_BUDGET_NAME = "selection-budget.jsonl"
DIRECTOR_WALL_ALLOWANCE_SECONDS = 120.0

type BudgetPhase = Literal[
    "director_reserved", "director_settled", "preview_reserved", "preview_settled"
]
type BudgetResult = Literal["succeeded", "failed", "uncertain"]
type BudgetScope = Literal["sample", "full_episode"]


class SelectionBudgetEntryV1(StrictModel):
    """One selection-attempt ledger line.

    ``scope`` is the sample→full-episode boundary: "sample" lines feed
    the sample allowance, "full_episode" lines feed only the
    full-episode allowance — neither gate ever counts the other.
    ``model_id`` attributes the line to one production model (None =
    unattributed). ``retry_index`` itemizes internal retries so every
    attempt of a retried call settles its own line. Absent (defaults)
    on legacy lines.
    """

    schema_version: Literal["cockpit-selection-budget-v1"] = (
        "cockpit-selection-budget-v1"
    )
    attempt_id: str
    consultation_id: str
    judgment_id: str
    reservation_sequence: int = Field(ge=1, strict=True)
    phase: BudgetPhase
    llm_calls_reserved: int = Field(ge=0, strict=True)
    llm_calls_used: int = Field(ge=0, strict=True)
    wall_seconds_reserved: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    wall_seconds_used: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    preview_seconds_reserved: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    preview_seconds_used: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    result: BudgetResult | None = None
    failure_code: str | None = None
    scope: BudgetScope = "sample"
    model_id: str | None = None
    retry_index: int = Field(default=0, ge=0, strict=True)
    created_at: str


class SelectionBudgetUsed(NamedTuple):
    llm_calls: int
    wall_seconds: float
    preview_seconds: float


@dataclass(frozen=True, slots=True)
class SelectionAttempt:
    attempt_id: str
    consultation_id: str
    judgment_id: str
    reservation_sequence: int
    scope: BudgetScope = "sample"


def attempt_for(
    reservation_sequence: int,
    consultation_id: str,
    judgment_id: str,
    scope: BudgetScope = "sample",
) -> SelectionAttempt:
    suffix = "" if scope == "sample" else f"-{scope}"
    return SelectionAttempt(
        attempt_id=f"selection-{reservation_sequence}{suffix}",
        consultation_id=consultation_id,
        judgment_id=judgment_id,
        reservation_sequence=reservation_sequence,
        scope=scope,
    )


def _journal_path(episode_dir: Path) -> Path:
    return episode_dir / "consultation" / SELECTION_BUDGET_NAME


def append_selection_budget_entry(
    episode_dir: Path, entry: SelectionBudgetEntryV1
) -> None:
    path = _journal_path(episode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")


def load_selection_budget_entries(episode_dir: Path) -> list[SelectionBudgetEntryV1]:
    try:
        lines = _journal_path(episode_dir).read_bytes().splitlines()
    except OSError:
        return []
    entries: list[SelectionBudgetEntryV1] = []
    for line in lines:
        try:
            entries.append(SelectionBudgetEntryV1.model_validate_json(line))
        except ValidationError as error:
            raise CockpitUnprocessableError(
                "consultation-log-corrupt", f"unparsable line in {_journal_path(episode_dir)}"
            ) from error
    return entries


def selection_budget_used(episode_dir: Path) -> SelectionBudgetUsed:
    """Sample-scope totals (the sample gate never counts full-episode)."""

    return selection_budget_used_in_scope(episode_dir, "sample")


def selection_budget_used_in_scope(
    episode_dir: Path, scope: BudgetScope
) -> SelectionBudgetUsed:
    """Cumulative totals for one scope over the whole journal (no reset).

    Every settled line counts — including failed results and every
    internal retry's own settle line — so the ledger is the complete
    call breakdown, not just the first observation per attempt. An open
    reservation (no matching settle yet) counts as still reserved, so a
    crash never replays a paid attempt for free.
    """

    by_attempt: dict[str, list[SelectionBudgetEntryV1]] = {}
    for entry in load_selection_budget_entries(episode_dir):
        if entry.scope != scope:
            continue
        by_attempt.setdefault(entry.attempt_id, []).append(entry)
    calls = 0
    wall = 0.0
    preview = 0.0
    for rows in by_attempt.values():
        director_settled = [
            row for row in rows if row.phase == "director_settled"
        ]
        preview_settled = [
            row for row in rows if row.phase == "preview_settled"
        ]
        if director_settled:
            calls += sum(row.llm_calls_used for row in director_settled)
            wall += sum(row.wall_seconds_used for row in director_settled)
        else:
            reserved = next(
                (row for row in rows if row.phase == "director_reserved"), None
            )
            if reserved is not None:
                calls += reserved.llm_calls_reserved
                wall += reserved.wall_seconds_reserved
        if preview_settled:
            wall += sum(row.wall_seconds_used for row in preview_settled)
            preview += sum(row.preview_seconds_used for row in preview_settled)
        else:
            reserved = next(
                (row for row in rows if row.phase == "preview_reserved"), None
            )
            if reserved is not None:
                wall += reserved.wall_seconds_reserved
                preview += reserved.preview_seconds_reserved
    return SelectionBudgetUsed(llm_calls=calls, wall_seconds=wall, preview_seconds=preview)


def has_open_director_reservation(episode_dir: Path, attempt_id: str) -> bool:
    rows = [
        entry
        for entry in load_selection_budget_entries(episode_dir)
        if entry.attempt_id == attempt_id
    ]
    return any(row.phase == "director_reserved" for row in rows) and not any(
        row.phase == "director_settled" for row in rows
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _entry(  # noqa: PLR0913 (ledger-line factory; kwargs are the entry contract)
    attempt: SelectionAttempt,
    phase: BudgetPhase,
    *,
    calls_reserved: int = 0,
    calls_used: int = 0,
    wall_reserved: float = 0.0,
    wall_used: float = 0.0,
    preview_reserved: float = 0.0,
    preview_used: float = 0.0,
    result: BudgetResult | None = None,
    failure_code: str | None = None,
    model_id: str | None = None,
    retry_index: int = 0,
) -> SelectionBudgetEntryV1:
    return SelectionBudgetEntryV1(
        attempt_id=attempt.attempt_id,
        consultation_id=attempt.consultation_id,
        judgment_id=attempt.judgment_id,
        reservation_sequence=attempt.reservation_sequence,
        phase=phase,
        llm_calls_reserved=calls_reserved,
        llm_calls_used=calls_used,
        wall_seconds_reserved=wall_reserved,
        wall_seconds_used=wall_used,
        preview_seconds_reserved=preview_reserved,
        preview_seconds_used=preview_used,
        result=result,
        failure_code=failure_code,
        scope=attempt.scope,
        model_id=model_id,
        retry_index=retry_index,
        created_at=_now(),
    )


def reserve_director(
    episode_dir: Path,
    attempt: SelectionAttempt,
    wall_allowance: float,
    *,
    model_id: str | None = None,
) -> SelectionBudgetEntryV1:
    entry = _entry(
        attempt, "director_reserved", calls_reserved=1,
        wall_reserved=wall_allowance, model_id=model_id,
    )
    append_selection_budget_entry(episode_dir, entry)
    return entry


def settle_director(  # noqa: PLR0913 (ledger-line settle; kwargs are the entry contract)
    episode_dir: Path,
    attempt: SelectionAttempt,
    *,
    wall_elapsed: float,
    result: BudgetResult,
    failure_code: str | None = None,
    model_id: str | None = None,
    retry_index: int = 0,
) -> SelectionBudgetEntryV1:
    entry = _entry(
        attempt, "director_settled", calls_used=1, wall_used=wall_elapsed,
        result=result, failure_code=failure_code,
        model_id=model_id, retry_index=retry_index,
    )
    append_selection_budget_entry(episode_dir, entry)
    return entry


def reserve_preview(
    episode_dir: Path, attempt: SelectionAttempt, preview_seconds: float
) -> SelectionBudgetEntryV1:
    entry = _entry(attempt, "preview_reserved", preview_reserved=preview_seconds)
    append_selection_budget_entry(episode_dir, entry)
    return entry


def settle_preview(  # noqa: PLR0913 (ledger-line settle; kwargs are the entry contract)
    episode_dir: Path,
    attempt: SelectionAttempt,
    *,
    preview_seconds: float,
    wall_elapsed: float,
    result: BudgetResult,
    failure_code: str | None = None,
) -> SelectionBudgetEntryV1:
    entry = _entry(
        attempt, "preview_settled", wall_used=wall_elapsed, preview_used=preview_seconds,
        result=result, failure_code=failure_code,
    )
    append_selection_budget_entry(episode_dir, entry)
    return entry


def plan_preview_seconds(plan: EditPlan0C) -> float:
    end = max((item.span.end_frame for item in plan.plan.items), default=0)
    return end * plan.frame_rate.den / plan.frame_rate.num


def ir_preview_seconds(ir: TimelineIr0C) -> float:
    end = max(
        (
            item.record_span.end_frame
            for track in ir.tracks
            for item in track.items
        ),
        default=0,
    )
    return end * ir.rate.den / ir.rate.num


__all__ = [
    "DIRECTOR_WALL_ALLOWANCE_SECONDS",
    "SELECTION_BUDGET_NAME",
    "BudgetPhase",
    "BudgetResult",
    "BudgetScope",
    "SelectionAttempt",
    "SelectionBudgetEntryV1",
    "SelectionBudgetUsed",
    "append_selection_budget_entry",
    "attempt_for",
    "has_open_director_reservation",
    "ir_preview_seconds",
    "load_selection_budget_entries",
    "plan_preview_seconds",
    "reserve_director",
    "reserve_preview",
    "selection_budget_used",
    "selection_budget_used_in_scope",
    "settle_director",
    "settle_preview",
]
