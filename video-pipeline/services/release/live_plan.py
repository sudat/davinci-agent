"""Cwd-independent access to the frozen Phase-3 A/B plan (Todo 67).

The live replay runs from external lane working directories (the F3
bootstrap cwd), never from the source root, so every ``compile_ab`` call
must resolve the frozen manifest directory against the SOURCE root the
running code belongs to — never the process cwd.
"""

from __future__ import annotations

from pathlib import Path

from services.job_runner.gate_p3_ab import MANIFEST_DIR, AbPlan, compile_ab
from services.job_runner.gate_p3_scan import repo_root


def frozen_plan_dir() -> Path:
    """The frozen phase-3 manifest directory inside the running source root."""

    return repo_root() / MANIFEST_DIR


def frozen_ab_plan() -> AbPlan:
    """Compile the frozen A/B plan from the source-root manifest directory."""

    return compile_ab(frozen_plan_dir())


__all__ = ["frozen_ab_plan", "frozen_plan_dir"]
