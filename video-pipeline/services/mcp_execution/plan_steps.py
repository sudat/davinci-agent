"""Audio-ladder, color, and production-kit emitters (task 38).

Enabled audio finishing stages become audio steps (voice-isolation gated by
its accepted matrix row; eq/compression and ducking land on their failed
capabilities' fallback rungs; the Task 5 dialogue-chain stages ride the
granular Fairlight preset route; loudness/peak QC rides the accepted
advanced-delivery-qc row read-only). Needed color sections become grade
steps on the task-35 preferred path. Presentation intents become
production-kit recipe steps via task-37 selections — the accepted_only
binding refuses the whole compile when its capability is not accepted.
"""

# allow: SIZE_OK — pure flat emitter table (one builder per plan domain);
# splitting would separate the emitters from the shared step_from envelope
# (placement_steps.py precedent).

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from services.mcp_execution.plan_models import (
    MCP_RUNGS,
    FallbackRung,
    FallbackStepRecordV1,
    McpExecutionStepV1,
)
from services.mcp_execution.plan_payloads import (
    AudioMetricReadback,
    AudioOpParams,
    AudioStageParams,
    AudioStateReadback,
    ColorParams,
    ColorTargetPayload,
    DuckingParams,
    GradeReadback,
    RenderNativeParams,
    RenderNativeReadback,
    VoiceIsolationParams,
)
from services.mcp_execution.step_builders import (
    CapabilityView,
    mcp_or_executor,
    step_from,
)
from services.outputs.geometry import DEFAULT_OUTPUT_ID, OutputId, geometry_for

if TYPE_CHECKING:
    from services.creative_plan.audio_finishing import AudioFinishingPlanV1
    from services.creative_plan.color_finishing import ColorFinishingPlanV1
    from services.creative_plan.ir_models_v2 import TimelineIrV2

_OPTIONAL_STAGE: Final = "optional_eq_compression_voice_isolation"

#: Task 5: the dialogue-chain stages ride the pinned MCP's granular
#: Fairlight preset actions (resolve_control list + project_settings
#: apply), so they take the granular MCP rung directly — the color_steps
#: precedent for a measured vendor route without a v4.3 matrix row. Every
#: other audio stage keeps its matrix-driven rung.
_PRESET_STAGES: Final[dict[str, str]] = {
    "dialogue_cleanup": "fairlight-dialogue-chain-v1",
    "dialogue_level_normalization": "fairlight-dialogue-chain-v1",
}

#: Task 6: the measured exposure-correction section rides the pinned
#: MCP's guarded DRX route (dry-run → confirmation token → apply) with a
#: hash-pinned DRX binding and explicit target item ids; other color
#: sections keep their current surface and refuse live typed (no binding).
_DRX_SECTIONS: Final[dict[str, str]] = {
    "technical_correction.exposure": "drx-technical-normalize-v1",
}


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
            preset_ref = _PRESET_STAGES.get(name)
            if preset_ref is not None:
                rung: FallbackRung = "mcp_granular_tool"
                record = None
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
                        preset_ref=preset_ref,
                    ),
                    AudioMetricReadback(
                        kind="audio_metric",
                        stage=name,
                        metric=target.metric,
                        minimum=target.minimum,
                        maximum=target.maximum,
                        unit=target.unit,
                    ),
                    mcp_or_executor(
                        rung,
                        "render_boundary_report" if preset_ref is None else "safe_set_audio_properties",  # noqa: E501
                    ),
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


def _exposure_targets(
    plan: ColorFinishingPlanV1, ir_v2: TimelineIrV2 | None
) -> tuple[ColorTargetPayload, ...]:
    """Explicit target items for the exposure DRX: video items placed from
    the sources the exposure evidence cites (builder-pinned
    ``"{source_id}: {detail}"`` prefix), ordered by record position."""

    if ir_v2 is None or not plan.technical_correction.exposure.needed:
        return ()
    sources = {
        row.split(": ", 1)[0]
        for row in plan.technical_correction.exposure.evidence
    }
    items = sorted(
        (
            item
            for track in ir_v2.video_tracks
            for item in track.items
            if item.source.source_id in sources
        ),
        key=lambda item: (item.record_span.start_frame, str(item.item_id)),
    )
    # source_span is the committed identity that disambiguates duplicate
    # record spans live (subtitle cue cards share each target's span).
    return tuple(
        ColorTargetPayload(
            item_id=item.item_id,
            record_span=item.record_span,
            source_span=item.source.span,
        )
        for item in items
    )


def color_steps(
    plan: ColorFinishingPlanV1,
    caps: CapabilityView,
    wide: int,
    ir_v2: TimelineIrV2 | None = None,
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
    drx_targets = _exposure_targets(plan, ir_v2)
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
        drx_ref = _DRX_SECTIONS.get(section)
        targets = drx_targets if drx_ref is not None else ()
        preset = look_ref if look_ref is not None else drx_ref or "channel-default"
        steps.append(
            step_from(
                f"stp-color-{section_id}",
                ColorParams(
                    action="apply_color",
                    section=section,
                    target_note=note or "needed section",
                    look_ref=look_ref,  # type: ignore[arg-type]
                    drx_ref=drx_ref,
                    targets=targets,
                ),
                GradeReadback(kind="grade", target=section, preset_ref=preset),
                mcp_or_executor(rung, "safe_apply_drx"),
                rung,
                record,
                wide,
                dry_run_token=f"dryrun:stp-color-{section_id}" if rung in MCP_RUNGS else None,
                timeline_ready=True,
                items=tuple(target.item_id for target in targets),
            )
        )
    return steps


def render_native_steps(
    ir_v2: TimelineIrV2,
    caps: CapabilityView,  # noqa: ARG001 (uniform emitter signature)
    wide: int,
    output_id: OutputId = DEFAULT_OUTPUT_ID,
) -> list[McpExecutionStepV1]:
    """One deterministic native render step at the end of the plan."""

    geometry = geometry_for(output_id)
    custom_name = f"finishing-native-{ir_v2.episode_id}"
    rung: FallbackRung = "mcp_verified_workflow"
    record = None
    return [
        step_from(
            "stp-render-native",
            RenderNativeParams(
                action="render_native",
                custom_name=custom_name,
                format_id="mp4",
                codec_id="H264",
                width=geometry.width,
                height=geometry.height,
                frame_rate=30.0,
                select_all_frames=True,
                export_video=True,
                export_audio=True,
                data_burn_in="None",
            ),
            RenderNativeReadback(kind="render_native", custom_name=custom_name),
            mcp_or_executor(rung, "render_native"),
            rung,
            record,
            wide,
            destructive=True,
            timeline_ready=True,
        )
    ]


__all__ = ["audio_plan_steps", "color_steps", "render_native_steps"]
