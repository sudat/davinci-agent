"""Media/placement/subtitle emitters (task 38).

Pure per-element step builders over the IR video/audio tracks and the
subtitle plan artifact; every step goes through ``step_from`` (envelope
invariants: PRD §19 fallback ladder, retry class, preconditions).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.mcp_execution.plan_models import (
    FallbackRung,
    FallbackStepRecordV1,
    McpExecutionStepV1,
)
from services.mcp_execution.plan_payloads import (
    CueCountReadback,
    ImportMediaParams,
    ImportReadback,
    PlaceAudioParams,
    PlaceClipParams,
    PlacementReadback,
    PlaceOverlayParams,
    PlaceTitleParams,
    PrepareProjectParams,
    ProjectReadback,
    SubtitleParams,
    TitleReadback,
)
from services.mcp_execution.step_builders import (
    CapabilityView,
    mcp_or_executor,
    step_from,
)

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.creative_plan.subtitle_models import SubtitlePlanV1

# T31 synthetic source convention: still/graphic items are generated
# placeholders, never imported media (compile_ir_v2 mints ``still-{op_id}``
# and ``graphic-{op_id}`` source ids).
_SYNTHETIC_SOURCE_PREFIXES: Final[tuple[str, ...]] = ("still-", "graphic-")

_VIDEO_ROLE_ORDER: Final[dict[str, int]] = {
    "primary": 0,
    "b_roll": 1,
    "insert": 2,
    "still": 3,
    "graphic": 4,
}


def prepare_step(ir: TimelineIrV2, caps: CapabilityView) -> McpExecutionStepV1:
    rung, record = caps.rung_for("project-timeline-creation", "project bootstrap")
    timeline_name = f"{ir.episode_id}-timeline"
    rate = str(ir.rate.num) if ir.rate.den == 1 else str(ir.rate.num / ir.rate.den)
    return step_from(
        f"stp-prepare-{ir.episode_id}",
        PrepareProjectParams(
            action="prepare_project",
            timeline_name=timeline_name,
            fps_num=ir.rate.num,
            fps_den=ir.rate.den,
        ),
        ProjectReadback(kind="project", project_name=timeline_name, timeline_frame_rate=rate),
        mcp_or_executor(rung, "prepare_project"),
        rung,
        record,
        -2,
    )


def import_steps(ir: TimelineIrV2, caps: CapabilityView) -> list[McpExecutionStepV1]:
    referenced = {
        item.source.source_id for track in ir.video_tracks + ir.audio_tracks for item in track.items
    }
    sources = sorted(
        source for source in referenced if not source.startswith(_SYNTHETIC_SOURCE_PREFIXES)
    )
    steps: list[McpExecutionStepV1] = []
    for source_id in sources:
        rung, record = caps.rung_for("import-media", f"import {source_id}")
        steps.append(
            step_from(
                f"stp-import-{source_id}",
                ImportMediaParams(action="import_media", source_id=source_id),
                ImportReadback(kind="import", source_id=source_id),
                mcp_or_executor(rung, "safe_import_media"),
                rung,
                record,
                -1,
                project_ready=True,
            )
        )
    return steps


def video_steps(ir: TimelineIrV2, caps: CapabilityView) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    ordered = sorted(ir.video_tracks, key=lambda t: _VIDEO_ROLE_ORDER.get(t.role, 9))
    for track in ordered:
        for item in sorted(track.items, key=lambda i: (i.record_span.start_frame, i.item_id)):
            pos = item.record_span.start_frame
            if track.role == "primary":
                rung, record = caps.rung_for(
                    "exact-source-range-placement", f"place {item.item_id}"
                )
                steps.append(
                    step_from(
                        f"stp-place-{item.item_id}",
                        PlaceClipParams(
                            action="place_clip",
                            item_id=item.item_id,
                            source=item.source,
                            record_span=item.record_span,
                            track_role="primary",
                            av_link_id=item.av_link_id,
                        ),
                        PlacementReadback(
                            kind="placement",
                            item_id=item.item_id,
                            source_span=item.source.span,
                            record_span=item.record_span,
                        ),
                        mcp_or_executor(rung, "append_to_timeline"),
                        rung,
                        record,
                        pos,
                        project_ready=True,
                        timeline_ready=True,
                        media=(item.source.source_id,),
                    )
                )
            elif track.role == "graphic":
                rung, record = caps.rung_for("fusion-template-insertion", f"title {item.item_id}")
                steps.append(
                    step_from(
                        f"stp-title-{item.item_id}",
                        PlaceTitleParams(
                            action="place_title",
                            item_id=item.item_id,
                            record_span=item.record_span,
                            track_role="graphic",
                        ),
                        TitleReadback(
                            kind="title", item_id=item.item_id, record_span=item.record_span
                        ),
                        mcp_or_executor(rung, "insert_fusion_title"),
                        rung,
                        record,
                        pos,
                        project_ready=True,
                        timeline_ready=True,
                    )
                )
            else:
                rung, record = caps.rung_for(
                    "exact-source-range-placement", f"overlay {item.item_id}"
                )
                steps.append(
                    step_from(
                        f"stp-place-{item.item_id}",
                        PlaceOverlayParams(
                            action="place_overlay",
                            item_id=item.item_id,
                            source=item.source,
                            record_span=item.record_span,
                            track_role=track.role,
                        ),
                        PlacementReadback(
                            kind="placement",
                            item_id=item.item_id,
                            source_span=item.source.span,
                            record_span=item.record_span,
                        ),
                        mcp_or_executor(rung, "append_to_timeline"),
                        rung,
                        record,
                        pos,
                        project_ready=True,
                        timeline_ready=True,
                        media=(item.source.source_id,),
                    )
                )
    return steps


def audio_track_steps(ir: TimelineIrV2, caps: CapabilityView) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    for track in sorted(ir.audio_tracks, key=lambda t: t.track_id):
        for item in sorted(track.items, key=lambda i: (i.record_span.start_frame, i.item_id)):
            rung, record = caps.rung_for(
                "exact-source-range-placement", f"place audio {item.item_id}"
            )
            steps.append(
                step_from(
                    f"stp-place-{item.item_id}",
                    PlaceAudioParams(
                        action="place_audio",
                        item_id=item.item_id,
                        source=item.source,
                        record_span=item.record_span,
                        audio_role=track.role,
                        av_link_id=item.av_link_id,
                    ),
                    PlacementReadback(
                        kind="placement",
                        item_id=item.item_id,
                        source_span=item.source.span,
                        record_span=item.record_span,
                    ),
                    mcp_or_executor(rung, "append_to_timeline"),
                    rung,
                    record,
                    item.record_span.start_frame,
                    project_ready=True,
                    timeline_ready=True,
                    media=(item.source.source_id,),
                )
            )
    return steps


def subtitle_step(plan: SubtitlePlanV1, caps: CapabilityView) -> McpExecutionStepV1:
    selected = plan.capability_path.selected
    if selected == "native_text_plus":
        rung, record = caps.rung_for("subtitle-capability", "native subtitle rung")
    elif selected in ("styled_template", "external_ass_srt"):
        rung: FallbackRung = "external_asset_render"
        record = FallbackStepRecordV1(
            rung=rung,
            reason=f"subtitle ladder selected the {selected} rung (renders outside native Text+)",
            capability="subtitle-capability",
            status=plan.capability_path.matrix_status,
        )
    else:  # manual
        rung = "manual_finalization"
        record = FallbackStepRecordV1(
            rung=rung,
            reason="subtitle ladder selected the manual rung",
            capability="subtitle-capability",
            status=plan.capability_path.matrix_status,
        )
    cue_count = len(plan.cues)
    first = min((cue.record_span.start_frame for cue in plan.cues), default=0)
    return step_from(
        "stp-subtitle-plan",
        SubtitleParams(
            action="apply_subtitles",
            selected_path=selected,
            cue_count=cue_count,
            style_profile_id=plan.style_profile.profile_id,
        ),
        CueCountReadback(kind="cue_count", cue_count=cue_count),
        mcp_or_executor(rung, "subtitle_generation_probe"),
        rung,
        record,
        first,
        project_ready=True,
        timeline_ready=True,
    )


__all__ = [
    "audio_track_steps",
    "import_steps",
    "prepare_step",
    "subtitle_step",
    "video_steps",
]
