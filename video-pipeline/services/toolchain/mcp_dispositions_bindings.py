"""Reviewed exact route-binding authority for the pinned MCP dispositions.

Two frozen tables are the ONLY operations allowed to claim ``mapped`` routes:

- ``SURFACE_OPERATION_BINDINGS`` — for each supported product surface, the
  compound operation IDs its registered live handler provably reaches with
  literal ``ctx.transport(tool, action, ...)`` vendor calls (handler plus the
  bounded helper call graph inside the handler package);
- ``OPS_READ_OPERATION_BINDINGS`` — each compound operation ID bound to the
  exact one typed ``McpOps`` read method whose body issues that vendor call.

Both were extracted from the handler/ops sources at pin commit
132e134d3aa25d3d0df6bdf38f051bd29d128211 and hand-reviewed (Task 10). The
static verifier in :mod:`services.toolchain.mcp_dispositions_static` re-derives
the literal call sets from source on every validation, so a binding whose
handler no longer issues the vendor call fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

SURFACE_OPERATION_BINDINGS: Final[Mapping[str, frozenset[str]]] = {
    "append_to_timeline": frozenset((
        "media_pool.append_to_timeline",
        "timeline.get_items_in_track",
        "timeline_item.get_source_end_frame",
        "timeline_item.get_source_start_frame",
    )),
    "prepare_project": frozenset((
        "media_pool.create_timeline",
        "project_manager.create",
        "project_manager.load",
        "project_settings.set_setting",
        "timeline.get_current",
        "timeline.set_current",
    )),
    "render_boundary_report": frozenset((
        "render.export_render_boundary_report",
        "render.get_job_status",
        "render.prepare_render_job",
        "render.start",
    )),
    "render_native": frozenset((
        "render.delete_job",
        "render.get_codecs",
        "render.get_format_and_codec",
        "render.get_formats",
        "render.get_job_status",
        "render.is_rendering",
        "render.list_jobs",
        "render.prepare_render_job",
        "render.safe_set_render_settings",
        "render.set_format_and_codec",
        "render.start",
        "render.validate_render_settings",
    )),
    "safe_apply_drx": frozenset((
        "render.get_job_status",
        "render.prepare_render_job",
        "render.start",
        "timeline.probe_timeline_structure",
        "timeline_item_color.add_version",
        "timeline_item_color.grade_version_snapshot",
        "timeline_item_color.probe_node_graph",
        "timeline_item_color.safe_apply_drx",
    )),
    "safe_import_media": frozenset((
        "media_pool.safe_import_media",
    )),
    "safe_set_audio_properties": frozenset((
        "project_settings.apply_fairlight_preset",
        "render.get_job_status",
        "render.prepare_render_job",
        "render.start",
        "resolve_control.get_fairlight_presets",
    )),
    "set_voice_isolation_state": frozenset((
        "timeline.get_voice_isolation_state",
        "timeline.set_voice_isolation_state",
    )),
    "set_transform": frozenset((
        "timeline_item.get_transform",
        "timeline_item.set_transform",
    )),
    "subtitle_generation_probe": frozenset((
        "fusion_comp.get_input",
        "fusion_comp.get_text_plus",
        "fusion_comp.safe_set_inputs",
        "media_pool.append_to_timeline",
        "media_pool.create_timeline",
        "timeline.add_track",
        "timeline.get_items_in_track",
        "timeline.get_media_pool_item",
        "timeline.get_track_count",
        "timeline.insert_fusion_title",
        "timeline.set_current",
    )),
}

OPS_READ_OPERATION_BINDINGS: Final[Mapping[str, str]] = {
    "project_manager.get_current": "get_current_project",
    "timeline.detect_gaps_overlaps": "detect_gaps_overlaps",
    "timeline.detect_missing_media": "detect_missing_media",
    "timeline_item.get_transform": "get_transform",
    "timeline.voice_isolation_capabilities": "voice_isolation_capabilities",
    "timeline.edit_kernel_capabilities": "edit_kernel_capabilities",
    "timeline.audio_mix_capability_report": "audio_mix_capability_report",
    "timeline_item_fusion.get_comp_count": "fusion_comp_count",
    "render.get_settings": "render_get_settings",
    "media_analysis.find_similar": "find_similar",
    "edit_engine.plan_selects": "edit_engine_plan_selects",
}

__all__ = ["OPS_READ_OPERATION_BINDINGS", "SURFACE_OPERATION_BINDINGS"]
