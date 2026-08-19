"""Render/QC toolchain section for Phase 2 (frozen completion + preset contract).

The pin freezes the live-verified render completion contract from the
Todo-19 findings: completion is detected ONLY via
``CompletionPercentage == 100`` (localized job status strings are never
parsed), ``SelectAllFrames`` bounds the render extent because MarkIn/MarkOut
are ignored, and the fixed preset (MP4/H264 1920x1080, aac 48 kHz stereo)
carries the basic_audio_preset capability. The smoke is fully deterministic
and offline: the contract invariants hold and the frozen classification rule
separates canned localized payloads — including a false "complete" status
string with percentage 99 — correctly. No Resolve launch, no render.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write

COMPLETION_FIELD: str = "CompletionPercentage"
COMPLETION_VALUE: int = 100


class RenderCompletionContract(StrictModel):
    completion_field: Literal["CompletionPercentage"]
    completion_value: Literal[100]
    status_strings_parsed: Literal[False]
    select_all_frames: Literal[True]
    marks_bound_render_extent: Literal[False]


class RenderPreset(StrictModel):
    video_format: Literal["MP4"]
    video_codec: Literal["H264"]
    width: Literal[1920]
    height: Literal[1080]
    audio_codec: Literal["aac"]
    audio_sample_rate: Literal[48000]
    audio_channels: Literal[2]


class RenderQcSection(StrictModel):
    schema_version: Literal["render-qc-v1"]
    completion: RenderCompletionContract
    preset: RenderPreset
    qc_policy_id: Literal["qc-phase2-v1"]
    external_credentials: Literal["none"]


class RenderQcSmokeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def render_complete(status: Mapping[str, object]) -> bool:
    """The frozen completion rule: only CompletionPercentage == 100 completes.

    A localized status string — even one reading as "complete" — never
    satisfies the contract by itself (finding: render-status-strings-localized).
    """

    percentage = status.get(COMPLETION_FIELD)
    return isinstance(percentage, int) and not isinstance(percentage, bool) and (
        percentage == COMPLETION_VALUE
    )


def verify_render_qc_contract(section: RenderQcSection) -> None:
    if section.external_credentials != "none":
        raise RenderQcSmokeError("render-qc pin must carry no credentials")
    contract = section.completion
    if contract.completion_field != COMPLETION_FIELD:
        raise RenderQcSmokeError("completion field must stay CompletionPercentage")
    if contract.completion_value != COMPLETION_VALUE or contract.status_strings_parsed:
        raise RenderQcSmokeError("localized status strings must stay unparsed")
    if not contract.select_all_frames or contract.marks_bound_render_extent:
        raise RenderQcSmokeError("SelectAllFrames must bound the render extent")
    if (section.preset.audio_codec, section.preset.audio_sample_rate) != ("aac", 48000):
        raise RenderQcSmokeError("the basic audio preset must stay aac/48kHz")


def _canned_status_payloads() -> tuple[dict[str, object], ...]:
    return (
        {"JobStatus": "完了", "CompletionPercentage": 100},
        {"JobStatus": "Complete", "CompletionPercentage": 99},
        {"JobStatus": "Rendering", "CompletionPercentage": 47},
        {"JobStatus": "完了"},
    )


def run_render_qc_smoke(section: RenderQcSection, smoke_dir: Path) -> Path:
    """Deterministic offline contract validation; evidence under smoke_dir."""

    smoke_dir.mkdir(parents=True, exist_ok=True)
    verify_render_qc_contract(section)
    classified = tuple(
        {"job_status": str(payload.get("JobStatus")), "render_complete": render_complete(payload)}
        for payload in _canned_status_payloads()
    )
    if tuple(row["render_complete"] for row in classified) != (True, False, False, False):
        raise RenderQcSmokeError("frozen completion rule misclassified the canned payloads")
    evidence_payload = {
        "classified_payloads": classified,
        "completion_contract": section.completion.model_dump(mode="json"),
        "live_render_rerun": False,
        "preset": section.preset.model_dump(mode="json"),
        "qc_policy_id": section.qc_policy_id,
    }
    evidence = smoke_dir / "render-qc-smoke.json"
    atomic_write(
        evidence,
        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode(),
    )
    return evidence


__all__ = [
    "COMPLETION_FIELD",
    "COMPLETION_VALUE",
    "RenderCompletionContract",
    "RenderPreset",
    "RenderQcSection",
    "RenderQcSmokeError",
    "render_complete",
    "run_render_qc_smoke",
    "verify_render_qc_contract",
]
