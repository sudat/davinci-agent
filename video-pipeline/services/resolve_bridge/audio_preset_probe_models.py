"""Protocols and report models for the Todo-59 audio preset capability probe.

The protocols mirror ONLY the documented Resolve 21 scripting surface the
probe touches: disposable project creation, timeline audio tracks, project
Get/SetSetting, and structural readback. The report records every step
honestly — an apply surface that exists but cannot be read back is
``api_available`` with ``live_verified=False``, never a fake verified row.
"""

from __future__ import annotations

from typing import Literal, Protocol

from services.contracts.primitives import StrictModel

StepStatus = Literal["verified", "failed", "unsupported", "timeout"]
PresetRung = Literal["fixed_track_preset", "project_template", "none"]


class PresetTimelineApi(Protocol):
    def GetName(self) -> str: ...

    def GetTrackCount(self, track_type: str) -> int: ...

    def AddTrack(self, track_type: str, sub_track_type: str = ...) -> bool: ...


class PresetProjectApi(Protocol):
    def GetName(self) -> str: ...

    def GetSetting(self, setting_name: str) -> str: ...

    def SetSetting(self, setting_name: str, setting_value: str) -> bool: ...


class PresetStep(StrictModel):
    step: str
    status: StepStatus
    detail: str = ""


class MethodProbe(StrictModel):
    """One candidate API method: does it exist on the live timeline?"""

    name: str
    present: bool


class SettingProbe(StrictModel):
    """One candidate project audio setting and its set+readback round-trip."""

    setting: str
    value: str
    set_roundtrip: bool


class AudioPresetProbeReport(StrictModel):
    """The honest capability verdict for the Fairlight preset surface."""

    schema_version: Literal["audio-preset-probe-report-v1"]
    binding: str
    preset_rung: PresetRung
    applied: bool
    readback_verified: bool
    methods: tuple[MethodProbe, ...]
    settings: tuple[SettingProbe, ...]
    steps: tuple[PresetStep, ...]

    @property
    def preset_supported(self) -> bool:
        return self.applied and self.readback_verified


__all__ = [
    "AudioPresetProbeReport",
    "MethodProbe",
    "PresetProjectApi",
    "PresetRung",
    "PresetStep",
    "PresetTimelineApi",
    "SettingProbe",
    "StepStatus",
]
