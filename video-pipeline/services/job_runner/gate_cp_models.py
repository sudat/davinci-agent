"""Observation models for the Control Plane Baseline Gate (Todo 13).

Observations record RAW operation outcomes only — never an authored
pass/fail. The evaluator recomputes every criterion from these files
plus the filesystem/store state they point at.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import StrictModel


class OperationOutcome(StrictModel):
    name: str
    result: str = Field(min_length=1)
    detail: str = ""


class GateObservation(StrictModel):
    """One driven scenario's raw evidence, written under ``<evidence>/``."""

    fixture_id: str
    kind: str
    work_dir: str
    operations: tuple[OperationOutcome, ...] = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    outcome_basis: Literal["raw-operation-capture"] = "raw-operation-capture"

    def operation(self, name: str) -> OperationOutcome:
        for candidate in self.operations:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def field(self, name: str) -> str:
        return self.fields[name]


__all__ = ["GateObservation", "OperationOutcome"]
