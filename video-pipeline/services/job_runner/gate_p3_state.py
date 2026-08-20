"""Shared criterion state for the Phase-3 gate checks (Todo 62)."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.gates.phase3 import PHASE_3_CRITERIA

C_REGRESSION, C_STRUCTURE, C_RIGHTS, C_PRESENTATION = PHASE_3_CRITERIA


@dataclass(frozen=True, slots=True)
class Mismatch:
    code: str
    detail: str


@dataclass(slots=True)
class CheckState:
    criteria: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(PHASE_3_CRITERIA, True)
    )
    evidence: dict[str, list[str]] = field(
        default_factory=lambda: {key: [] for key in PHASE_3_CRITERIA}
    )
    mismatches: list[Mismatch] = field(default_factory=list)

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.mismatches.append(Mismatch(code=code, detail=detail))

    def note(self, criterion: str, *shas: str) -> None:
        for sha in shas:
            if sha and sha not in self.evidence[criterion]:
                self.evidence[criterion].append(sha)


__all__ = [
    "C_PRESENTATION",
    "C_REGRESSION",
    "C_RIGHTS",
    "C_STRUCTURE",
    "CheckState",
    "Mismatch",
]
