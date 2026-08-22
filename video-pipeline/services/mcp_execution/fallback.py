"""PRD §19 fallback ladder transitions (task 39).

The ladder order lives in :mod:`services.mcp_execution.plan_models`
(``FALLBACK_LADDER`` — task 38); this module never re-declares it. What
lives here is the REPORTING contract: every rung change becomes an
explicit :class:`FallbackRungEntryV1` — ``{step, from_rung, to_rung,
reason}`` — carried in the run report. Silent downgrades are structurally
impossible: a plan-declared fallback step (T38's
:class:`FallbackStepRecordV1`) ALWAYS yields an entry, and a runtime
permanent failure ALWAYS yields one (or a typed refusal on the terminal
rung, where no further fallback exists).

Two entry sources:

* ``plan_declared`` — the compiler already placed the step on a fallback
  rung (matrix capability failed). ``from_rung`` is the preferred MCP
  rung the capability would have used (ladder top).
* ``runtime_failure`` — the step failed on its own rung after its retry
  class was exhausted. The transition is exactly ONE rung down
  (``next_rung``); executing the fallback rung is a NEW plan, never an
  in-run mutation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.mcp_execution.plan_models import (
    FALLBACK_LADDER,
    FallbackRung,
    next_rung,
)

if TYPE_CHECKING:
    from services.mcp_execution.plan_models import McpExecutionStepV1

#: The preferred rung a plan-declared fallback step moved DOWN from.
PLAN_DECLARED_FROM: Final[FallbackRung] = "mcp_verified_workflow"

EntrySource = Literal["plan_declared", "runtime_failure"]


class FallbackLadderError(ValueError):
    """Typed refusal from a ladder transition (never a silent skip)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class FallbackRungEntryV1(StrictModel):
    """One reported rung change: ``{step, from_rung, to_rung, reason}``."""

    step: Identifier
    from_rung: FallbackRung
    to_rung: FallbackRung
    reason: str = Field(min_length=1, strict=True)
    source: EntrySource
    capability: str | None = None
    status: str | None = None

    @model_validator(mode="after")
    def require_downward_transition(self) -> FallbackRungEntryV1:
        if FALLBACK_LADDER.index(self.to_rung) <= FALLBACK_LADDER.index(self.from_rung):
            raise PydanticCustomError(
                "rung_not_downward",
                "fallback transitions only move down the ladder: {from_rung} -> {to_rung}",
                {"from_rung": self.from_rung, "to_rung": self.to_rung},
            )
        return self


def plan_declared_entry(step: McpExecutionStepV1) -> FallbackRungEntryV1 | None:
    """The explicit entry for a T38 fallback step; ``None`` on MCP rungs."""
    record = step.fallback_record
    if record is None:
        return None
    return FallbackRungEntryV1(
        step=step.step_id,
        from_rung=PLAN_DECLARED_FROM,
        to_rung=step.rung,
        reason=record.reason,
        source="plan_declared",
        capability=record.capability,
        status=record.status,
    )


def runtime_transition(step: McpExecutionStepV1, reason: str) -> FallbackRungEntryV1:
    """One-rung-down transition after a step exhausted its retry class.

    The successor is fixed by the ladder (``next_rung``) — callers cannot
    pick a rung. The terminal rung has no successor, so a step already on
    ``unsupported_with_report`` is a typed refusal.
    """
    if step.fallback == step.rung:
        raise FallbackLadderError(
            "terminal-rung",
            f"step {step.step_id} is on the terminal rung {step.rung!r}; "
            "no further fallback exists",
        )
    return FallbackRungEntryV1(
        step=step.step_id,
        from_rung=step.rung,
        to_rung=next_rung(step.rung),
        reason=reason,
        source="runtime_failure",
    )


__all__ = [
    "PLAN_DECLARED_FROM",
    "EntrySource",
    "FallbackLadderError",
    "FallbackRungEntryV1",
    "plan_declared_entry",
    "runtime_transition",
]
