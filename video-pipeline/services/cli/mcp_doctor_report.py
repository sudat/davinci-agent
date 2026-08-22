"""Typed report model for ``mcp-doctor``: sections, codes, and the aggregate.

Every check lands as one :class:`DoctorSection` carrying an explicit
``status`` and typed ``code`` — a failed check is never silently downgraded
to ok. The :class:`DoctorReport` aggregate fixes the exit code and renders
the JSON contract printed to stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True, slots=True)
class DoctorPaths:
    clone_dir: Path
    pin_path: Path
    repo_venv_python: Path


class DoctorSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    status: Literal["ok", "fail"]
    code: str
    detail: str


class DoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["mcp-doctor-v1"]
    ok: bool
    exit_code: int
    sections: tuple[DoctorSection, ...]

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def ok_section(check: str, detail: str) -> DoctorSection:
    return DoctorSection(check=check, status="ok", code="ok", detail=detail)


def fail_section(check: str, code: str, detail: str) -> DoctorSection:
    return DoctorSection(check=check, status="fail", code=code, detail=detail)


__all__ = [
    "DoctorPaths",
    "DoctorReport",
    "DoctorSection",
    "fail_section",
    "ok_section",
]
