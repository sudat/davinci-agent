"""Step-construction core: capability view + envelope builder (task 38).

No emitter lives here. :class:`CapabilityView` resolves mcp-fit acceptance
statuses into PRD §19 rungs (+ fallback records), and :func:`step_from` is
the SINGLE envelope constructor every emitter calls — it fixes the ladder
successor, retry class, and precondition shape, so no builder can mint a
step with a self-referential fallback or a mismatched retry class. The
emitters live in ``placement_steps.py`` (media/placement/subtitle),
``effect_steps.py`` (effects/transitions), and ``plan_steps.py``
(audio-plan/color/kit); the compiler (``compiler.py``) orchestrates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from services.mcp_execution.plan_models import (
    MATRIX_FALLBACK_RUNG,
    MCP_RUNGS,
    CompileExecutionPlanError,
    FallbackRung,
    FallbackStepRecordV1,
    McpExecutionStepV1,
    StepPreconditionsV1,
    ToolSurface,
    next_rung,
)

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier, SourceId
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.mcp_execution.plan_payloads import ExpectedReadback, StepParams

_EXECUTOR_SURFACE: Final[dict[FallbackRung, ToolSurface]] = {
    "direct_scripting_gap_adapter": "direct_script_adapter",
    "external_asset_render": "external_asset_builder",
    "manual_finalization": "manual_operator",
    "unsupported_with_report": "manual_operator",
}


class CapabilityView:
    """Frozen per-compile view of mcp-fit statuses + row fallback strings."""

    __slots__ = ("_fallbacks", "_statuses")

    def __init__(self, statuses: Mapping[str, str], fallbacks: Mapping[str, str]) -> None:
        self._statuses = dict(statuses)
        self._fallbacks = dict(fallbacks)

    def status(self, capability: str) -> str:
        try:
            return self._statuses[capability]
        except KeyError:
            raise CompileExecutionPlanError(
                "capability-unknown", f"capability {capability!r} absent from the status map"
            ) from None

    def as_mcp_fit(self) -> dict[str, object]:
        """Rows shape for task-37 ``resolve_binding`` (the kit hard gate)."""
        rows = [
            {"capability": capability, "status": status}
            for capability, status in sorted(self._statuses.items())
        ]
        return {"capabilities": rows}

    def rung_for(
        self, capability: str | None, purpose: str
    ) -> tuple[FallbackRung, FallbackStepRecordV1 | None]:
        """Acceptance -> verified MCP rung; otherwise the matrix fallback rung."""
        if capability is None:
            rung: FallbackRung = "direct_scripting_gap_adapter"
            return rung, FallbackStepRecordV1(
                rung=rung,
                reason=f"no MCP capability row for {purpose}; direct scripting gap adapter",
            )
        status = self.status(capability)
        if status == "accepted":
            return "mcp_verified_workflow", None
        fallback = self._fallbacks.get(capability, "legacy_direct")
        rung = MATRIX_FALLBACK_RUNG.get(fallback, "direct_scripting_gap_adapter")
        return rung, FallbackStepRecordV1(
            rung=rung,
            reason=(
                f"capability {capability!r} status {status!r} "
                f"(matrix fallback {fallback!r}) for {purpose}"
            ),
            capability=capability,
            status=status,
        )


def executor_surface(rung: FallbackRung) -> ToolSurface:
    return _EXECUTOR_SURFACE.get(rung, "direct_script_adapter")


def mcp_or_executor(rung: FallbackRung, mcp_surface: ToolSurface) -> ToolSurface:
    """The pinned MCP surface on MCP rungs; the rung executor otherwise."""
    return mcp_surface if rung in MCP_RUNGS else executor_surface(rung)


def timeline_end(ir: TimelineIrV2) -> int:
    ends = [
        item.record_span.end_frame
        for track in ir.video_tracks + ir.audio_tracks
        for item in track.items
    ]
    return max(ends, default=0)


def record_start_of(ir: TimelineIrV2, item_id: Identifier | None, fallback: int) -> int:
    if item_id is None:
        return fallback
    for track in ir.video_tracks + ir.audio_tracks:
        for item in track.items:
            if item.item_id == item_id:
                return item.record_span.start_frame
    return fallback


def step_from(  # noqa: PLR0913, PLR0917 (envelope knobs; one per step-contract invariant)
    step_id: str,
    params: StepParams,
    readback: ExpectedReadback,
    surface: ToolSurface,
    rung: FallbackRung,
    fallback_record: FallbackStepRecordV1 | None,
    record_position: int,
    *,
    destructive: bool = True,
    dry_run_token: str | None = None,
    project_ready: bool = False,
    timeline_ready: bool = False,
    media: tuple[SourceId, ...] = (),
    items: tuple[Identifier, ...] = (),
) -> McpExecutionStepV1:
    return McpExecutionStepV1(
        step_id=step_id,
        action=params.action,
        tool_surface=surface,
        rung=rung,
        fallback=next_rung(rung),
        fallback_record=fallback_record,
        normalized_params=params,
        preconditions=StepPreconditionsV1(
            project_ready=project_ready,
            timeline_ready=timeline_ready,
            media_imported=tuple(media),
            items_placed=tuple(items),
        ),
        destructive=destructive,
        dry_run_token=dry_run_token,
        expected_readback=readback,
        retry_class="transient" if rung in MCP_RUNGS else "permanent",
        record_position=record_position,
    )


__all__ = [
    "CapabilityView",
    "executor_surface",
    "mcp_or_executor",
    "record_start_of",
    "step_from",
    "timeline_end",
]
