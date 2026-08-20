"""Protocols and report models for the Todo-60 color setting capability probe.

The protocols mirror ONLY the documented Resolve 21 scripting surface the
probe touches: disposable project creation and project Get/SetSetting. The
report records every candidate setting honestly — a setting that exists
but cannot round-trip is ``value``-recorded with ``set_roundtrip=False``,
never a fake verified row.
"""

from __future__ import annotations

from typing import Literal, Protocol

from services.contracts.primitives import StrictModel

StepStatus = Literal["verified", "failed", "unsupported", "timeout"]


class ColorProjectApi(Protocol):
    def GetName(self) -> str: ...

    def GetSetting(self, setting_name: str) -> str: ...

    def SetSetting(self, setting_name: str, setting_value: str) -> bool: ...


class ColorProbeStep(StrictModel):
    step: str
    status: StepStatus
    detail: str = ""


class SettingProbe(StrictModel):
    """One candidate project color setting and its set+readback round-trip."""

    setting: str
    value: str
    set_roundtrip: bool


class ColorSettingProbeReport(StrictModel):
    """The honest capability verdict for the project color settings surface."""

    schema_version: Literal["color-setting-probe-report-v1"]
    binding: str
    settings: tuple[SettingProbe, ...]
    steps: tuple[ColorProbeStep, ...]

    @property
    def any_roundtrip(self) -> bool:
        return any(row.set_roundtrip for row in self.settings)


__all__ = [
    "ColorProbeStep",
    "ColorProjectApi",
    "ColorSettingProbeReport",
    "SettingProbe",
    "StepStatus",
]
