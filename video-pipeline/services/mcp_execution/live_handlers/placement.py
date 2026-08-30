"""Placement/session handlers: prepare, import, and append placements.

Split from ``common.py`` along the Task-3 responsibility boundary (shared
context vs the placement flow) once that module crossed the 250 pure-LOC
ceiling; the bounded reconciliation cluster lives in ``placement_verify``.
Handler order is fixed: validate (session state, then typed params) →
existing pinned MCP action(s) → typed response parse → independent
readback → product result. Logical product surface names never reach the
vendor transport from here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from services.mcp_client.ops_models import (
    AppendResult,
    ImportResult,
    McpActionOutcome,
    ProjectResult,
    TimelineResult,
)
from services.mcp_execution.live_errors import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
)
from services.mcp_execution.live_handlers.common import (
    LiveSessionContext,
    require_ok,
    validate_params,
)
from services.mcp_execution.live_handlers.placement_verify import (
    MEDIA_EOF_END_DRIFT_FRAMES,  # noqa: F401 (public import surface)
    PLACEMENT_TRACK_INDEX,
    PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
    SOURCE_READBACK_TOLERANCE_FRAMES,  # noqa: F401 (public import surface)
    _placement_present,
    _track_rows,
)
from services.mcp_execution.plan_payloads import (
    ImportMediaParams,
    PlaceAudioParams,
    PlaceClipParams,
    PlaceOverlayParams,
    PrepareProjectParams,
)

RANGE_PAIR_LENGTH: Final = 2


def _fps_str(num: int, den: int) -> str:
    return str(num) if den == 1 else str(num / den)


def prepare_project(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(PrepareProjectParams, params)
    fps = _fps_str(p.fps_num, p.fps_den)
    # Resume-first: a typed not-ok load means the project is absent, so
    # fall through to the create path; fps is pinned there only, because
    # Resolve refuses timelineFrameRate changes once a timeline exists.
    # Project identity prefers the create readback's own echo; the load
    # response exposes none, so the validated request name is the only
    # source on the resume arms.
    project_name = p.timeline_name
    loaded = McpActionOutcome.model_validate(
        ctx.transport("project_manager", "load", {"name": p.timeline_name})
    )
    if not loaded.ok:
        created = ProjectResult.model_validate(ctx.transport("project_manager", "create", {"name": p.timeline_name}))  # noqa: E501
        require_ok(created, "project-create")
        if created.name is not None:
            project_name = created.name
        require_ok(McpActionOutcome.model_validate(ctx.transport("project_settings", "set_setting", {"name": "timelineFrameRate", "value": fps})), "set-fps")  # noqa: E501
        require_ok(TimelineResult.model_validate(ctx.transport("media_pool", "create_timeline", {"name": p.timeline_name})), "create-timeline")  # noqa: E501
        require_ok(McpActionOutcome.model_validate(ctx.transport("timeline", "set_current", {"name": p.timeline_name})), "set-current")  # noqa: E501
    elif not McpActionOutcome.model_validate(
        ctx.transport("timeline", "set_current", {"name": p.timeline_name})
    ).ok:
        require_ok(TimelineResult.model_validate(ctx.transport("media_pool", "create_timeline", {"name": p.timeline_name})), "create-timeline")  # noqa: E501
        require_ok(McpActionOutcome.model_validate(ctx.transport("timeline", "set_current", {"name": p.timeline_name})), "set-current")  # noqa: E501
    cur = TimelineResult.model_validate(ctx.transport("timeline", "get_current", {}))
    require_ok(cur, "get-current")
    if cur.start_frame is None:
        raise LiveAdapterError("timeline-start-missing", "no start_frame")
    ctx.timeline_start = int(cur.start_frame)
    ctx.current_project_name = project_name
    ctx.current_timeline_name = cur.name
    ctx.current_timeline_id = cur.id
    return {"project_name": p.timeline_name, "timeline_frame_rate": fps, "start_frame": ctx.timeline_start}  # noqa: E501


def safe_import_media(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    p = validate_params(ImportMediaParams, params)
    path = ctx.media_paths.get(p.source_id) or ctx.source_paths.get(p.source_id) or ctx.path_for_source.get(p.source_id)  # noqa: E501
    if path is None:
        raise LiveAdapterUnsupportedError("unknown-media-path", f"no path for {p.source_id!r}")
    res = ImportResult.model_validate(ctx.transport("media_pool", "safe_import_media", {"paths": [path]}))  # noqa: E501
    require_ok(res, "safe-import")
    base = path.rsplit("/", 1)[-1]
    clip_id: str | None = None
    for c in res.clips:
        if c.name == base:
            clip_id = c.clip_id
            break
    if clip_id is None and len(res.clips) == 1:
        clip_id = res.clips[0].clip_id
    if clip_id is None:
        raise LiveAdapterError("clip-not-found", f"no clip for {path!r}")
    ctx.clip_ids[p.source_id] = clip_id
    ctx.path_for_source[p.source_id] = path
    ctx.media_paths[p.source_id] = path
    return {"source_id": p.source_id, "imported": res.imported, "clip_id": clip_id}


def append_to_timeline(
    ctx: LiveSessionContext, action: str, params: Mapping[str, object]
) -> dict[str, object]:
    if ctx.timeline_start is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    if action == "place_clip":
        q = validate_params(PlaceClipParams, params)
        src, rec, item_id, media_type, track_type = (
            q.source,
            q.record_span,
            str(q.item_id),
            1,
            "video",
        )
    elif action == "place_overlay":
        q2 = validate_params(PlaceOverlayParams, params)
        src, rec, item_id, media_type, track_type = (
            q2.source,
            q2.record_span,
            str(q2.item_id),
            1,
            "video",
        )
    elif action == "place_audio":
        q3 = validate_params(PlaceAudioParams, params)
        src, rec, item_id, media_type, track_type = (
            q3.source,
            q3.record_span,
            str(q3.item_id),
            2,
            "audio",
        )
    else:
        raise LiveAdapterUnsupportedError("unsupported-action", f"append {action!r}")
    clip_id = ctx.clip_ids.get(src.source_id)
    path = ctx.path_for_source.get(src.source_id) or ctx.media_paths.get(src.source_id)
    if clip_id is None or path is None:
        raise LiveAdapterUnsupportedError("media-not-imported", f"{src.source_id!r} not imported")
    abs_start = int(ctx.timeline_start) + int(rec.start_frame)
    abs_end = abs_start + (rec.end_frame - rec.start_frame)
    src_s, src_e = int(src.span.start_frame), int(src.span.end_frame)
    media_eof = ctx.media_frame_counts.get(src.source_id)
    if not _placement_present(
        ctx,
        track_type,
        _track_rows(ctx, track_type),
        (src_s, src_e),
        (abs_start, abs_end),
        media_eof,
    ):
        info: dict[str, object] = {"clip_id": clip_id, "start_frame": src_s, "end_frame": src_e, "record_frame": abs_start, "record_frame_mode": "absolute", "track_index": PLACEMENT_TRACK_INDEX, "media_type": media_type}  # noqa: E501
        require_ok(AppendResult.model_validate(ctx.transport("media_pool", "append_to_timeline", {"clip_infos": [info]})), "append")  # noqa: E501
        ctx.mark_timeline_mutated()
        # fresh post-mutation bounded scan (the snapshot was just cleared)
        if not _placement_present(
            ctx,
            track_type,
            _track_rows(ctx, track_type),
            (src_s, src_e),
            (abs_start, abs_end),
            media_eof,
        ):
            raise LiveAdapterError("placement-readback-mismatch", f"no {(src_s, src_e)}->{(abs_start, abs_end)}")  # noqa: E501
    return {"item_id": item_id, "source_span": {"start_frame": src_s, "end_frame": src_e, "rate": {"num": src.span.rate.num, "den": src.span.rate.den}}, "record_span": {"start_frame": rec.start_frame, "end_frame": rec.end_frame}}  # noqa: E501


__all__ = [
    "PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS",
    "append_to_timeline",
    "prepare_project",
    "safe_import_media",
]
