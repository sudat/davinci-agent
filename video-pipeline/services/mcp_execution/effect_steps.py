"""Effect-intent + transition emitters (task 38).

Flat per-kind table: every ``EffectKindV2`` maps to its step (punch_in via
the accepted clip-transform capability; crop/speed_change with no MCP row on
the direct adapter rung; ducking/noise-cleanup on their failed capabilities'
fallback rungs; manual_required and the color_look contradiction on manual
finalization). Semantic transitions map to the transition-path capability —
FAILED in the real matrix, so the step lands on the explicit external rung
with a fallback record (PRD §19: report, never silently downgrade).
"""

# allow: SIZE_OK — one flat match case per EffectKindV2 plus transitions;
# the per-kind table is irreducible (T31 _apply_op precedent).

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.mcp_execution.plan_models import (
    FallbackRung,
    FallbackStepRecordV1,
    McpExecutionStepV1,
)
from services.mcp_execution.plan_payloads import (
    AudioOpParams,
    AudioStateReadback,
    DuckingParams,
    ManualNoteReadback,
    ManualRequiredParams,
    SpeedChangeParams,
    TransformParams,
    TransformReadback,
    TransitionParams,
    VoiceIsolationParams,
)
from services.mcp_execution.step_builders import (
    CapabilityView,
    mcp_or_executor,
    record_start_of,
    step_from,
)

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import EffectIntentV2, TimelineIrV2


def _manual_step(
    effect: EffectIntentV2,
    note: str,
    pos: int,
    effect_kind: Literal["manual_required", "color_look"],
) -> McpExecutionStepV1:
    rung: FallbackRung = "manual_finalization"
    record = FallbackStepRecordV1(
        rung=rung, reason=f"manual finalization required for {effect.effect_id} ({effect_kind})"
    )
    return step_from(
        f"stp-fx-{effect.effect_id}",
        ManualRequiredParams(
            action="manual_required", effect_kind=effect_kind, effect_id=effect.effect_id, note=note
        ),
        ManualNoteReadback(kind="manual_note", expectation=note),
        "manual_operator",
        rung,
        record,
        pos,
        timeline_ready=True,
    )


def effect_steps(  # noqa: C901 (flat table: one case per effect kind)
    ir: TimelineIrV2,
    caps: CapabilityView,
    *,
    wide: int,
    color_look_covered: bool,
) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    for effect in sorted(ir.effect_intents, key=lambda e: e.effect_id):
        target = effect.target_item_id
        pos = record_start_of(ir, target, wide)
        items = (target,) if target is not None else ()
        match effect.kind:
            case "punch_in":
                rung, record = caps.rung_for(
                    "clip-transform-punch-in", f"effect {effect.effect_id}"
                )
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        TransformParams(
                            action="apply_transform",
                            effect_kind="punch_in",
                            target_item_id=target,
                            note=effect.note,
                        ),
                        TransformReadback(
                            kind="transform",
                            item_id=target or "timeline",
                            properties=("ZoomX", "ZoomY"),
                        ),
                        mcp_or_executor(rung, "set_transform"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "crop":
                rung, record = caps.rung_for(None, f"crop effect {effect.effect_id}")
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        TransformParams(
                            action="apply_transform",
                            effect_kind="crop",
                            target_item_id=target,
                            note=effect.note,
                        ),
                        TransformReadback(
                            kind="transform",
                            item_id=target or "timeline",
                            properties=("CropLeft", "CropRight", "CropTop", "CropBottom"),
                        ),
                        mcp_or_executor(rung, "set_transform"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "voice_isolation":
                rung, record = caps.rung_for("voice-isolation", f"effect {effect.effect_id}")
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        VoiceIsolationParams(
                            action="apply_voice_isolation",
                            effect_kind="voice_isolation",
                            target_item_id=target,
                            note=effect.note,
                        ),
                        AudioStateReadback(
                            kind="audio_state",
                            item_ref=target or "timeline",
                            state_property="voice_isolation",
                        ),
                        mcp_or_executor(rung, "set_voice_isolation_state"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "noise_cleanup":
                rung, record = caps.rung_for(
                    "audio-property-operation", f"noise cleanup {effect.effect_id}"
                )
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        AudioOpParams(
                            action="apply_audio_op",
                            effect_kind="noise_cleanup",
                            capability="audio-property-operation",
                        ),
                        AudioStateReadback(
                            kind="audio_state",
                            item_ref=target or "timeline",
                            state_property="noise_reduction",
                        ),
                        mcp_or_executor(rung, "safe_set_audio_properties"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "ducking":
                rung, record = caps.rung_for(
                    "bgm-track-ducking", f"ducking effect {effect.effect_id}"
                )
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        DuckingParams(
                            action="apply_ducking", effect_kind="ducking", target_item_id=target
                        ),
                        AudioStateReadback(
                            kind="audio_state",
                            item_ref=target or "timeline",
                            state_property="duck_depth",
                        ),
                        mcp_or_executor(rung, "safe_set_audio_properties"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "speed_change":
                pct = effect.speed_factor_pct if effect.speed_factor_pct is not None else 100
                rung, record = caps.rung_for(None, f"retime {effect.effect_id}")
                note = f"clip retimed to {pct}% of source speed"
                steps.append(
                    step_from(
                        f"stp-fx-{effect.effect_id}",
                        SpeedChangeParams(
                            action="apply_speed_change",
                            effect_kind="speed_change",
                            target_item_id=target,
                            speed_factor_pct=pct,
                        ),
                        ManualNoteReadback(kind="manual_note", expectation=note),
                        mcp_or_executor(rung, "set_transform"),
                        rung,
                        record,
                        pos,
                        timeline_ready=True,
                        items=items,
                    )
                )
            case "color_look":
                if not color_look_covered:
                    steps.append(
                        _manual_step(
                            effect,
                            "color look intent with no configured look; human resolution required",
                            pos,
                            "color_look",
                        )
                    )
            case "manual_required":
                steps.append(
                    _manual_step(
                        effect,
                        effect.note or "creative plan marks this manual",
                        pos,
                        "manual_required",
                    )
                )
    return steps


def transition_steps(ir: TimelineIrV2, caps: CapabilityView) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    for transition in sorted(ir.transitions, key=lambda t: t.transition_id):
        rung, record = caps.rung_for("transition-path", f"transition {transition.transition_id}")
        expectation = (
            f"transition {transition.transition_id} ({transition.style}) visible at "
            f"record frame {transition.at_record_frame}"
        )
        steps.append(
            step_from(
                f"stp-transition-{transition.transition_id}",
                TransitionParams(
                    action="apply_transition",
                    effect_kind="transition",
                    transition_id=transition.transition_id,
                    style=transition.style,
                    at_record_frame=transition.at_record_frame,
                    duration_frames=transition.duration_frames,
                    a_item_ref=transition.a_item_ref,
                    b_item_ref=transition.b_item_ref,
                ),
                ManualNoteReadback(kind="manual_note", expectation=expectation),
                mcp_or_executor(rung, "duplicate_clips"),
                rung,
                record,
                transition.at_record_frame,
                timeline_ready=True,
                items=(transition.a_item_ref, transition.b_item_ref),
            )
        )
    return steps


__all__ = ["effect_steps", "transition_steps"]
