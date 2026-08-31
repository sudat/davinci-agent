"""McpExecutionPlanV1 models (task 38; PRD §11.3, §19).

The deterministic MCP Execution Plan: an ordered tuple of typed steps the
task-39 runner executes serially under the single-writer lease. Each step
names a CLOSED tool surface (task-7/11 ops vocabulary plus the three
non-MCP executors the PRD §19 ladder names), typed normalized params, typed
preconditions, an expected readback, a retry class, the rung it executes
on, and the next fallback rung. Steps exist ONLY through the compiler's
typed builders — the module exposes no generic emit-a-raw-call constructor
(the action vocabulary and the discriminated params union make one
unrepresentable; tests freeze the surface).

Fallback contract (PRD §19 — never silently downgrade): a step on a
fallback rung (or the advanced-library rung) carries a
``FallbackStepRecordV1`` naming the capability, its matrix status, and why
the rung moved. The plan stays COMPLETE — the downgrade is reported in the
step, not hidden.
"""

from __future__ import annotations

import hashlib
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, SourceId, StrictModel, to_tuple
from services.foundation_io import canonical_model_bytes
from services.mcp_execution.plan_payloads import (  # noqa: TC001 (pydantic field types)
    ExpectedReadback,
    StepAction,
    StepParams,
)

FallbackRung = Literal[
    "mcp_verified_workflow",
    "mcp_granular_tool",
    "advanced_mcp_library",
    "direct_scripting_gap_adapter",
    "external_asset_render",
    "manual_finalization",
    "unsupported_with_report",
]

#: PRD §19 ladder order — each rung's successor is the next fallback.
FALLBACK_LADDER: tuple[FallbackRung, ...] = (
    "mcp_verified_workflow",
    "mcp_granular_tool",
    "advanced_mcp_library",
    "direct_scripting_gap_adapter",
    "external_asset_render",
    "manual_finalization",
    "unsupported_with_report",
)

#: mcp-fit row ``fallback`` vocabulary (task 11) -> PRD §19 rung.
MATRIX_FALLBACK_RUNG: dict[str, FallbackRung] = {
    "legacy_direct": "direct_scripting_gap_adapter",
    "template_external": "external_asset_render",
    "manual": "manual_finalization",
}

#: Rungs executed by the pinned MCP server (retry class "transient").
MCP_RUNGS: frozenset[FallbackRung] = frozenset(
    {"mcp_verified_workflow", "mcp_granular_tool", "advanced_mcp_library"}
)

#: Stable step-class order (sort key component 1); record position is component 2;
#: audio order is component 3 (stage ladder), step id is component 4.
STEP_CLASS_RANK: dict[StepAction, int] = {
    "prepare_project": 0,
    "import_media": 1,
    "place_clip": 2,
    "place_overlay": 2,
    "place_title": 2,
    "place_audio": 2,
    "apply_subtitles": 3,
    "apply_transform": 4,
    "apply_speed_change": 4,
    "apply_voice_isolation": 5,
    "apply_audio_op": 5,
    "apply_audio_stage": 5,
    "apply_ducking": 5,
    "apply_transition": 6,
    "apply_kit_recipe": 7,
    "apply_color": 8,
    "render_native": 9,
    "manual_required": 10,
}

ToolSurface = Literal[
    "prepare_project",
    "safe_import_media",
    "append_to_timeline",
    "insert_fusion_title",
    "insert_fusion_composition",
    "set_title_text",
    "set_transform",
    "duplicate_clips",
    "set_voice_isolation_state",
    "safe_set_audio_properties",
    "safe_apply_drx",
    "subtitle_generation_probe",
    "render_boundary_report",
    "render_native",
    "direct_script_adapter",
    "external_asset_builder",
    "manual_operator",
]

RetryClass = Literal["transient", "permanent"]


class CompileExecutionPlanError(ValueError):
    """Typed refusal from the execution-plan compiler (never a silent partial)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def next_rung(rung: FallbackRung) -> FallbackRung:
    """The PRD §19 successor of ``rung`` (terminal rung reports unsupported)."""
    index = FALLBACK_LADDER.index(rung)
    return FALLBACK_LADDER[min(index + 1, len(FALLBACK_LADDER) - 1)]


class FallbackStepRecordV1(StrictModel):
    """Why a step left its MCP rung (the PRD §19 no-silent-downgrade report)."""

    rung: FallbackRung
    reason: str = Field(min_length=1, strict=True)
    capability: str | None = None
    status: str | None = None


class StepPreconditionsV1(StrictModel):
    """Typed execution preconditions (runner refuses when unmet)."""

    project_ready: bool = False
    timeline_ready: bool = False
    media_imported: Annotated[tuple[SourceId, ...], BeforeValidator(to_tuple)] = ()
    items_placed: Annotated[tuple[Identifier, ...], BeforeValidator(to_tuple)] = ()


class McpExecutionStepV1(StrictModel):
    """One step: tool surface, action, typed params, readback, fallback."""

    step_id: Identifier
    action: StepAction
    tool_surface: ToolSurface
    rung: FallbackRung
    fallback: FallbackRung
    fallback_record: FallbackStepRecordV1 | None = None
    normalized_params: StepParams
    preconditions: StepPreconditionsV1
    destructive: bool
    dry_run_token: str | None = None
    expected_readback: ExpectedReadback
    retry_class: RetryClass
    record_position: int = Field(strict=True)

    @model_validator(mode="after")
    def require_coherent_step(self) -> McpExecutionStepV1:
        if self.normalized_params.action != self.action:
            raise PydanticCustomError(
                "params_action_mismatch",
                "normalized_params action {params_action} must match step action {action}",
                {"params_action": self.normalized_params.action, "action": self.action},
            )
        expected_retry: RetryClass = "transient" if self.rung in MCP_RUNGS else "permanent"
        if self.retry_class != expected_retry:
            raise PydanticCustomError(
                "retry_class_rung",
                "rung {rung} requires retry_class {retry}",
                {"rung": self.rung, "retry": expected_retry},
            )
        on_mcp = self.rung in {"mcp_verified_workflow", "mcp_granular_tool"}
        if on_mcp == (self.fallback_record is not None):
            raise PydanticCustomError(
                "fallback_record_parity",
                "verified/granular MCP steps carry no fallback record; fallback rungs must",
            )
        if self.rung == "unsupported_with_report" and self.fallback == self.rung:
            return self  # terminal rung may self-loop as "no further fallback"
        if self.fallback == self.rung:
            raise PydanticCustomError("fallback_self", "fallback rung must differ from rung")
        return self


#: Audio ladder order for deterministic within-phase sorting (final QC last).
#: Maps each distinct audio step to its committed AudioFinishingPlanV1 position:
#: cleanup 0, normalization 1, eq 2, compression 3, voice 4, bgm 5, ducking 6,
#: ambience 7, sfx 8, loudness 9. All other actions use 0.
_AUDIO_ORDER: dict[str, int] = {
    "dialogue_cleanup": 0,
    "dialogue_level_normalization": 1,
    "eq": 2,
    "compression": 3,
    "voice_isolation": 4,
    "bgm_placement": 5,
    "music_ducking": 6,
    "ambience_preservation": 7,
    "optional_sfx": 8,
    "loudness_peak_qc": 9,
}


def _audio_order(step: McpExecutionStepV1) -> int:  # noqa: PLR0911
    action = step.action
    params = step.normalized_params
    if action == "apply_audio_stage":
        stage = getattr(params, "stage", None)
        if isinstance(stage, str):
            return _AUDIO_ORDER.get(stage, 99)
        return 99
    if action == "apply_voice_isolation":
        return _AUDIO_ORDER["voice_isolation"]
    if action == "apply_audio_op":
        effect = getattr(params, "effect_kind", None)
        if isinstance(effect, str) and effect in _AUDIO_ORDER:
            return _AUDIO_ORDER[effect]
        return 99
    if action == "apply_ducking":
        return _AUDIO_ORDER["music_ducking"]
    return 0


def step_sort_key(step: McpExecutionStepV1) -> tuple[int, int, int, str]:
    return (
        STEP_CLASS_RANK[step.action],
        step.record_position,
        _audio_order(step),
        step.step_id,
    )


class McpExecutionPlanV1(StrictModel):
    """The committed deterministic MCP Execution Plan (schema v1)."""

    schema_version: Literal["mcp-execution-plan-v1"]
    plan_id: Sha256
    episode_id: Identifier
    steps: Annotated[tuple[McpExecutionStepV1, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def require_ordered_unique_steps(self) -> McpExecutionPlanV1:
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise PydanticCustomError("duplicate_step", "step ids are unique")
        for earlier, later in pairwise(self.steps):
            if step_sort_key(later) < step_sort_key(earlier):
                raise PydanticCustomError(
                    "step_order",
                    "steps are sorted by (step class, record position, audio order, id): {step}",
                    {"step": later.step_id},
                )
        return self


def compute_plan_id(episode_id: Identifier, steps: tuple[McpExecutionStepV1, ...]) -> Sha256:
    """Deterministic plan id: sha256 over canonical bytes with the id zeroed."""
    draft = McpExecutionPlanV1(
        schema_version="mcp-execution-plan-v1",
        plan_id="0" * 64,
        episode_id=episode_id,
        steps=steps,
    )
    digest: Sha256 = hashlib.sha256(canonical_model_bytes(draft)).hexdigest()
    return digest


__all__ = [
    "FALLBACK_LADDER",
    "MATRIX_FALLBACK_RUNG",
    "MCP_RUNGS",
    "STEP_CLASS_RANK",
    "CompileExecutionPlanError",
    "FallbackRung",
    "FallbackStepRecordV1",
    "McpExecutionPlanV1",
    "McpExecutionStepV1",
    "RetryClass",
    "StepPreconditionsV1",
    "ToolSurface",
    "compute_plan_id",
    "next_rung",
    "step_sort_key",
]
