"""Audio-ladder, color, and production-kit emitters (task 38).

Enabled audio finishing stages become audio steps (voice-isolation gated by
its accepted matrix row; eq/compression and ducking land on their failed
capabilities' fallback rungs; loudness/peak QC rides the accepted
advanced-delivery-qc row read-only). Needed color sections become grade
steps on the task-35 preferred path. Presentation intents become
production-kit recipe steps via task-37 selections — the accepted_only
binding refuses the whole compile when its capability is not accepted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from services.mcp_execution.plan_models import (
    MCP_RUNGS,
    FallbackStepRecordV1,
    McpExecutionStepV1,
)
from services.mcp_execution.plan_payloads import (
    AudioMetricReadback,
    AudioOpParams,
    AudioStageParams,
    AudioStateReadback,
    ColorParams,
    DuckingParams,
    GradeReadback,
    VoiceIsolationParams,
)
from services.mcp_execution.step_builders import (
    CapabilityView,
    mcp_or_executor,
    step_from,
)

if TYPE_CHECKING:
    from services.creative_plan.audio_finishing import AudioFinishingPlanV1
    from services.creative_plan.color_finishing import ColorFinishingPlanV1

_OPTIONAL_STAGE: Final = "optional_eq_compression_voice_isolation"


def audio_plan_steps(
    plan: AudioFinishingPlanV1, caps: CapabilityView, wide: int
) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    for stage in plan.stages:
        if not stage.enabled:
            continue
        name = stage.stage
        if name == _OPTIONAL_STAGE:
            for op in stage.ops:
                if not op.enabled:
                    continue
                steps.extend(_audio_op_step(op.op, op.capability, op.justification, caps, wide))
        elif name == "music_ducking":
            rung, record = caps.rung_for("bgm-track-ducking", "music ducking stage")
            steps.append(
                step_from(
                    "stp-audio-music_ducking",
                    DuckingParams(action="apply_ducking", effect_kind="ducking", stage=name),
                    AudioStateReadback(
                        kind="audio_state", item_ref="timeline", state_property="duck_depth"
                    ),
                    mcp_or_executor(rung, "safe_set_audio_properties"),
                    rung,
                    record,
                    wide,
                    timeline_ready=True,
                )
            )
        else:
            capability = "advanced-delivery-qc" if name == "loudness_peak_qc" else None
            rung, record = caps.rung_for(capability, f"{name} stage")
            target = stage.target_ranges[0]
            steps.append(
                step_from(
                    f"stp-audio-{name}",
                    AudioStageParams(
                        action="apply_audio_stage",
                        stage=name,
                        goal=stage.goal,
                        metric=target.metric,
                        minimum=target.minimum,
                        maximum=target.maximum,
                        unit=target.unit,
                    ),
                    AudioMetricReadback(
                        kind="audio_metric",
                        stage=name,
                        metric=target.metric,
                        minimum=target.minimum,
                        maximum=target.maximum,
                        unit=target.unit,
                    ),
                    mcp_or_executor(rung, "render_boundary_report"),
                    rung,
                    record,
                    wide,
                    destructive=name != "loudness_peak_qc",
                    timeline_ready=True,
                )
            )
    return steps


def _audio_op_step(
    op: Literal["eq", "compression", "voice_isolation"],
    capability: str,
    justification: str | None,
    caps: CapabilityView,
    wide: int,
) -> list[McpExecutionStepV1]:
    if op == "voice_isolation":
        rung, record = caps.rung_for("voice-isolation", "voice isolation op")
        return [
            step_from(
                f"stp-audioop-{op}",
                VoiceIsolationParams(
                    action="apply_voice_isolation",
                    effect_kind="voice_isolation",
                    stage=_OPTIONAL_STAGE,
                    note=justification or "",
                ),
                AudioStateReadback(
                    kind="audio_state", item_ref="timeline", state_property="voice_isolation"
                ),
                mcp_or_executor(rung, "set_voice_isolation_state"),
                rung,
                record,
                wide,
                timeline_ready=True,
            )
        ]
    rung, record = caps.rung_for(capability, f"{op} op")
    return [
        step_from(
            f"stp-audioop-{op}",
            AudioOpParams(
                action="apply_audio_op",
                effect_kind=op,
                stage=_OPTIONAL_STAGE,
                capability=capability,
                justification=justification,
            ),
            AudioStateReadback(kind="audio_state", item_ref="timeline", state_property=op),
            mcp_or_executor(rung, "safe_set_audio_properties"),
            rung,
            record,
            wide,
            timeline_ready=True,
        )
    ]


def color_steps(
    plan: ColorFinishingPlanV1, caps: CapabilityView, wide: int
) -> list[McpExecutionStepV1]:
    technical = plan.technical_correction
    sections: list[tuple[str, str, str | None]] = []
    if technical.exposure.needed:
        sections.append(
            ("technical_correction.exposure", "; ".join(technical.exposure.evidence), None)
        )
    if technical.white_balance.needed:
        sections.append(
            (
                "technical_correction.white_balance",
                "; ".join(technical.white_balance.evidence),
                None,
            )
        )
    if plan.camera_shot_matching.needed:
        sections.append(
            ("camera_shot_matching", "; ".join(plan.camera_shot_matching.evidence), None)
        )
    if plan.channel_episode_look.needed:
        sections.append(
            (
                "channel_episode_look",
                "; ".join(plan.channel_episode_look.evidence),
                plan.channel_episode_look.look_ref,
            )
        )
    steps: list[McpExecutionStepV1] = []
    for section, note, look_ref in sections:
        if plan.preferred_path == "mcp_live_grading":
            rung, record = caps.rung_for("color-grade-preset-drx", f"{section} grade")
        elif plan.preferred_path == "advanced_drx_qc":
            rung = "advanced_mcp_library"
            record = FallbackStepRecordV1(
                rung=rung,
                reason="live grading not accepted; advanced DRX QC path selected",
                capability="color-grade-preset-drx",
            )
        else:
            rung = "external_asset_render"
            record = FallbackStepRecordV1(
                rung=rung,
                reason="color preferred path is external",
                capability="color-grade-preset-drx",
            )
        section_id = section.replace(".", "-")
        preset = look_ref if look_ref is not None else "channel-default"
        steps.append(
            step_from(
                f"stp-color-{section_id}",
                ColorParams(
                    action="apply_color",
                    section=section,
                    target_note=note or "needed section",
                    look_ref=look_ref,  # type: ignore[arg-type]
                ),
                GradeReadback(kind="grade", target=section, preset_ref=preset),
                mcp_or_executor(rung, "safe_apply_drx"),
                rung,
                record,
                wide,
                dry_run_token=f"dryrun:stp-color-{section_id}" if rung in MCP_RUNGS else None,
                timeline_ready=True,
            )
        )
    return steps


__all__ = ["audio_plan_steps", "color_steps"]
