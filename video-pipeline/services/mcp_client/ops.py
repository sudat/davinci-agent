"""Typed probe/parity operations surface over the pinned MCP server (task 11).

:class:`McpOps` composes a :class:`~services.mcp_client.client.McpClient` and
exposes one typed method per tool action the capability probes and the MCP
parity backend need.  Every method funnels through the client's ``_call_tool``
base (single JSON-RPC round trip, envelope parsed) and returns a frozen model
from :mod:`services.mcp_client.ops_models` — never a raw dict.  Each method
takes an explicit ``timeout_seconds`` used for that one request so probes can
bound slow Resolve operations individually.
"""

# allow: SIZE_OK — pure flat typed-method table (one method per pinned MCP
# action); splitting would separate the methods from the shared _action seam.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from services.mcp_client.ops_models import (
    AddJobResult,
    AnalysisPlanResult,
    AppendResult,
    ApplyFairlightPresetResult,
    AudioPropertiesResult,
    CapabilityReport,
    CommitShotResult,
    CommitVisionResult,
    CompCountResult,
    DeepenResult,
    DrxApplyResult,
    EditExecuteResult,
    EditPlanResult,
    FairlightPresetsReadback,
    FindSimilarResult,
    FormatCodecResult,
    FusionInputsResult,
    FusionInputValueReadback,
    GapsOverlapsResult,
    GradeVersionSnapshotResult,
    ImportResult,
    McpActionOutcome,
    MediaPoolItemResult,
    MissingMediaResult,
    NodeGraphResult,
    ProjectResult,
    RenderBoundaryReport,
    RenderCodecsResult,
    RenderFormatsResult,
    RenderJobList,
    RenderJobStatus,
    RenderSettingsReadback,
    SafeSetRenderResult,
    SourceRangeResult,
    StructureSnapshot,
    SubtitleProbeResult,
    TextPlusReadback,
    TimelineResult,
    TrackCountResult,
    TrackItemsResult,
    TransformReadback,
    ValidatedSettings,
    VoiceIsolationReport,
    VoiceIsolationStateReadback,
)

if TYPE_CHECKING:
    from services.mcp_client.client import McpClient

DEFAULT_OP_TIMEOUT_SECONDS: Final = 30.0


class McpOps:
    """Typed operations surface; owns no transport of its own."""

    def __init__(self, client: McpClient) -> None:
        self._client = client

    @property
    def client(self) -> McpClient:
        return self._client

    # -- project / timeline ------------------------------------------------

    def create_project(self, name: str) -> ProjectResult:
        return self._action(ProjectResult, "project_manager", "create", {"name": name})

    def load_project(self, name: str) -> McpActionOutcome:
        return self._action(McpActionOutcome, "project_manager", "load", {"name": name})

    def get_current_project(self) -> ProjectResult:
        return self._action(ProjectResult, "project_manager", "get_current", {})

    def create_timeline(self, name: str) -> TimelineResult:
        return self._action(TimelineResult, "media_pool", "create_timeline", {"name": name})

    def create_timeline_from_clips(
        self, name: str, clip_infos: Sequence[Mapping[str, object]]
    ) -> TimelineResult:
        return self._action(
            TimelineResult,
            "media_pool",
            "create_timeline_from_clips",
            {"name": name, "clip_infos": [dict(info) for info in clip_infos]},
            timeout_seconds=120.0,
        )

    def get_current_timeline(self) -> TimelineResult:
        return self._action(TimelineResult, "timeline", "get_current", {})

    def set_current_timeline(self, name: str) -> McpActionOutcome:
        return self._action(McpActionOutcome, "timeline", "set_current", {"name": name})

    def duplicate_clips(
        self, *, selected: bool = True, copy_properties: Sequence[str] = ()
    ) -> McpActionOutcome:
        params: dict[str, object] = {"selected": selected}
        if copy_properties:
            params["copy_properties"] = list(copy_properties)
        return self._action(McpActionOutcome, "timeline", "duplicate_clips", params)

    def set_start_timecode(self, timecode: str) -> McpActionOutcome:
        return self._action(
            McpActionOutcome, "timeline", "set_start_timecode", {"timecode": timecode}
        )

    def set_project_setting(self, name: str, value: object) -> McpActionOutcome:
        return self._action(
            McpActionOutcome, "project_settings", "set_setting", {"name": name, "value": value}
        )

    def set_timeline_setting(self, name: str, value: object) -> McpActionOutcome:
        return self._action(
            McpActionOutcome, "timeline", "set_setting", {"name": name, "value": value}
        )

    def prepare_project(self, name: str, fps: float) -> McpActionOutcome:
        """Create a project and pin its frame rate BEFORE any timeline exists.

        Resolve refuses ``timelineFrameRate`` changes once a timeline is
        present (SetSetting returns False silently — verified live on
        21.0.4.5), so the rate must be set on the fresh project; every
        timeline created afterwards inherits it.  The value must be the
        STRING form ("30"), not a float.
        """
        created = self.create_project(name)
        if not created.ok:
            return created
        return self.set_project_setting("timelineFrameRate", str(fps))

    def ensure_timeline(self, name: str) -> TimelineResult:
        """Create an empty timeline and make it current (create does not)."""
        created = self.create_timeline(name)
        if not created.ok or not created.name:
            return created
        current = self.set_current_timeline(created.name)
        if not current.ok:
            return created
        return self.get_current_timeline()

    def timeline_structure(
        self, track_types: Sequence[str] = ("video", "audio", "subtitle")
    ) -> StructureSnapshot:
        return self._action(
            StructureSnapshot,
            "timeline",
            "probe_timeline_structure",
            {"track_types": list(track_types), "include_markers": False},
            timeout_seconds=60.0,
        )

    # -- media / placement -------------------------------------------------

    def safe_import_media(self, paths: Sequence[str]) -> ImportResult:
        return self._action(
            ImportResult, "media_pool", "safe_import_media", {"paths": list(paths)}
        )

    def append_to_timeline(
        self, clip_infos: Sequence[Mapping[str, object]]
    ) -> AppendResult:
        return self._action(
            AppendResult,
            "media_pool",
            "append_to_timeline",
            {"clip_infos": [dict(info) for info in clip_infos]},
            timeout_seconds=120.0,
        )

    def detect_gaps_overlaps(self) -> GapsOverlapsResult:
        return self._action(GapsOverlapsResult, "timeline", "detect_gaps_overlaps", {})

    def detect_missing_media(self) -> MissingMediaResult:
        return self._action(MissingMediaResult, "timeline", "detect_missing_media", {})

    def source_range_report(self) -> SourceRangeResult:
        return self._action(SourceRangeResult, "timeline", "source_range_report", {})

    # -- timeline item properties ------------------------------------------

    def set_transform(
        self, values: Mapping[str, float], *, track_index: int = 1, item_index: int = 0
    ) -> McpActionOutcome:
        payload: dict[str, object] = {
            "track_type": "video",
            "track_index": track_index,
            "item_index": item_index,
        }
        return self._action(
            McpActionOutcome,
            "timeline_item",
            "set_transform",
            {**payload, **dict(values)},
        )

    def get_transform(
        self, *, track_index: int = 1, item_index: int = 0
    ) -> TransformReadback:
        return self._action(
            TransformReadback,
            "timeline_item",
            "get_transform",
            {"track_type": "video", "track_index": track_index, "item_index": item_index},
        )

    def set_audio_properties(
        self, properties: Mapping[str, object], *, track_index: int = 1, item_index: int = 0
    ) -> AudioPropertiesResult:
        return self._action(
            AudioPropertiesResult,
            "timeline",
            "safe_set_audio_properties",
            {
                "properties": dict(properties),
                "track_type": "audio",
                "track_index": track_index,
                "item_index": item_index,
            },
        )

    def voice_isolation_capabilities(
        self, *, track_index: int = 1
    ) -> VoiceIsolationReport:
        return self._action(
            VoiceIsolationReport,
            "timeline",
            "voice_isolation_capabilities",
            {"track_index": track_index},
        )

    def set_voice_isolation_state(
        self, state: Mapping[str, object], *, track_index: int = 1
    ) -> McpActionOutcome:
        return self._action(
            McpActionOutcome,
            "timeline",
            "set_voice_isolation_state",
            {"state": dict(state), "track_index": track_index},
        )

    def get_voice_isolation_state(
        self, *, track_index: int = 1
    ) -> VoiceIsolationStateReadback:
        return self._action(
            VoiceIsolationStateReadback,
            "timeline",
            "get_voice_isolation_state",
            {"track_index": track_index},
        )

    def fairlight_presets(self) -> FairlightPresetsReadback:
        """``resolve_control {get_fairlight_presets}`` — the preset names the
        Fairlight apply action accepts (measured: ``{}`` when none saved)."""
        return self._action(
            FairlightPresetsReadback, "resolve_control", "get_fairlight_presets", {}
        )

    def apply_fairlight_preset(self, preset_name: str) -> ApplyFairlightPresetResult:
        """``project_settings {apply_fairlight_preset}`` onto the current
        timeline; a missing name answers ``success=false`` (measured)."""
        return self._action(
            ApplyFairlightPresetResult,
            "project_settings",
            "apply_fairlight_preset",
            {"preset_name": preset_name},
        )

    def subtitle_generation_probe(self) -> SubtitleProbeResult:
        return self._action(
            SubtitleProbeResult, "timeline", "subtitle_generation_probe", {}
        )

    def get_track_count(self, track_type: str) -> TrackCountResult:
        return self._action(
            TrackCountResult, "timeline", "get_track_count", {"track_type": track_type}
        )

    def add_video_track(self) -> McpActionOutcome:
        return self._action(McpActionOutcome, "timeline", "add_track", {"track_type": "video"})

    def get_items_in_track(self, track_type: str, track_index: int) -> TrackItemsResult:
        return self._action(
            TrackItemsResult,
            "timeline",
            "get_items_in_track",
            {"track_type": track_type, "track_index": track_index},
        )

    def current_timeline_media_pool_item(self) -> MediaPoolItemResult:
        return self._action(MediaPoolItemResult, "timeline", "get_media_pool_item", {})

    def set_fusion_inputs(
        self, tool_name: str, inputs: Mapping[str, object]
    ) -> FusionInputsResult:
        """``fusion_comp {safe_set_inputs}`` on the current timeline's V1 item 0.

        Scoped to the card-timeline contract (the sole title is V1 item 0),
        which is the only shape the nested-cue construction produces.
        """
        return self._action(
            FusionInputsResult,
            "fusion_comp",
            "safe_set_inputs",
            {
                "timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0},
                "tool_name": tool_name,
                "inputs": dict(inputs),
                "readback": True,
            },
        )

    def get_text_plus(self, tool_name: str) -> TextPlusReadback:
        """``fusion_comp {get_text_plus}`` on the current timeline's V1 item 0."""
        return self._action(
            TextPlusReadback,
            "fusion_comp",
            "get_text_plus",
            {
                "timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0},
                "tool_name": tool_name,
            },
        )

    def get_fusion_input(self, tool_name: str, input_name: str) -> FusionInputValueReadback:
        """``fusion_comp {get_input}`` on the current timeline's V1 item 0."""
        return self._action(
            FusionInputValueReadback,
            "fusion_comp",
            "get_input",
            {
                "timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0},
                "tool_name": tool_name,
                "input_name": input_name,
            },
        )

    def edit_kernel_capabilities(self) -> CapabilityReport:
        return self._action(CapabilityReport, "timeline", "edit_kernel_capabilities", {})

    def audio_mix_capability_report(self) -> CapabilityReport:
        return self._action(CapabilityReport, "timeline", "audio_mix_capability_report", {})

    def fusion_comp_count(
        self, *, track_index: int = 1, item_index: int = 0
    ) -> CompCountResult:
        return self._action(
            CompCountResult,
            "timeline_item_fusion",
            "get_comp_count",
            {"track_type": "video", "track_index": track_index, "item_index": item_index},
        )

    def insert_fusion_title(self, name: str) -> McpActionOutcome:
        """Insert a Fusion title on the current timeline at the playhead.

        Lands on the patch-panel target track (V1 in practice) — the
        nested-timeline card construction relies on that, per the measured
        api-limitations ledger.
        """
        return self._action(McpActionOutcome, "timeline", "insert_fusion_title", {"name": name})

    def insert_fusion_composition(self) -> McpActionOutcome:
        return self._action(McpActionOutcome, "timeline", "insert_fusion_composition", {})

    def set_title_text(
        self, text: str, *, track_index: int = 1, item_index: int = 0
    ) -> McpActionOutcome:
        return self._action(
            McpActionOutcome,
            "timeline",
            "set_title_text",
            {
                "text": text,
                "timeline_item": {
                    "track_type": "video",
                    "track_index": track_index,
                    "item_index": item_index,
                },
                "readback": True,
            },
        )

    # -- render -------------------------------------------------------------

    def render_set_format_and_codec(self, format_id: str, codec_id: str) -> FormatCodecResult:
        return self._action(
            FormatCodecResult,
            "render",
            "set_format_and_codec",
            {"format": format_id, "codec": codec_id},
        )

    def render_get_format_and_codec(self) -> FormatCodecResult:
        return self._action(FormatCodecResult, "render", "get_format_and_codec", {})

    def render_set_settings(self, settings: Mapping[str, object]) -> McpActionOutcome:
        return self._action(
            McpActionOutcome, "render", "set_settings", {"settings": dict(settings)}
        )

    def render_get_settings(self) -> RenderSettingsReadback:
        return self._action(RenderSettingsReadback, "render", "get_settings", {})

    def render_add_job(self) -> AddJobResult:
        return self._action(AddJobResult, "render", "add_job", {})

    def render_prepare_job(
        self,
        target_dir: str,
        custom_name: str,
        *,
        settings: Mapping[str, object] | None = None,
    ) -> AddJobResult:
        """``render {prepare_render_job}`` — queue (not start) one job with an
        explicit target dir and name (Task 4-measured probe render seam)."""
        merged: dict[str, object] = {"ExportVideo": True, "DataBurnIn": "None"}
        merged.update(dict(settings or {}))
        return self._action(
            AddJobResult,
            "render",
            "prepare_render_job",
            {
                "target_dir": target_dir,
                "custom_name": custom_name,
                "format": "mp4",
                "codec": "h264",
                "require_temp_target": False,
                "settings": merged,
            },
            timeout_seconds=60.0,
        )

    def render_start(self, job_ids: Sequence[str] | None = None) -> McpActionOutcome:
        params: dict[str, object] = (
            {} if job_ids is None else {"job_ids": list(job_ids)}
        )
        return self._action(McpActionOutcome, "render", "start", params)

    def render_job_status(self, job_id: str) -> RenderJobStatus:
        return self._action(RenderJobStatus, "render", "get_job_status", {"job_id": job_id})

    def render_list_jobs(self) -> RenderJobList:
        return self._action(RenderJobList, "render", "list_jobs", {})

    def render_delete_job(self, job_id: str) -> McpActionOutcome:
        return self._action(McpActionOutcome, "render", "delete_job", {"job_id": job_id})

    def render_boundary_report(self) -> RenderBoundaryReport:
        return self._action(RenderBoundaryReport, "render", "export_render_boundary_report", {})

    def validate_render_settings(
        self, settings: Mapping[str, object], *, require_temp_target: bool = False
    ) -> ValidatedSettings:
        params: dict[str, object] = {"settings": dict(settings)}
        if require_temp_target:
            params["require_temp_target"] = True
        return self._action(
            ValidatedSettings,
            "render",
            "validate_render_settings",
            params,
        )

    def safe_set_render_settings(
        self,
        settings: Mapping[str, object],
        *,
        dry_run: bool = False,
        require_temp_target: bool = False,
    ) -> SafeSetRenderResult:
        params: dict[str, object] = {"settings": dict(settings)}
        if dry_run:
            params["dry_run"] = True
        if require_temp_target:
            params["require_temp_target"] = True
        return self._action(
            SafeSetRenderResult,
            "render",
            "safe_set_render_settings",
            params,
        )

    def get_render_formats(self) -> RenderFormatsResult:
        return self._action(RenderFormatsResult, "render", "get_formats", {})

    def get_render_codecs(self, format_name: str) -> RenderCodecsResult:
        return self._action(
            RenderCodecsResult, "render", "get_codecs", {"format": format_name}
        )

    # -- analysis -----------------------------------------------------------

    def analyze_clip(  # noqa: PLR0913 (probe-tunable analysis knobs)
        self,
        clip_id: str | None = None,
        *,
        dry_run: bool = True,
        selected: bool = False,
        verbose: bool = False,
        sampling_mode: str = "adaptive_capped",
        include_transcription: bool = False,
    ) -> AnalysisPlanResult:
        params: dict[str, object] = {
            "dry_run": dry_run,
            "selected": selected,
            "verbose": verbose,
            "sampling_mode": sampling_mode,
            "include_transcription": include_transcription,
        }
        if clip_id is not None:
            params["clip_id"] = clip_id
        return self._action(
            AnalysisPlanResult,
            "media_analysis",
            "analyze_clip",
            params,
            timeout_seconds=300.0,
        )

    def commit_vision(
        self, visual: Mapping[str, object], *, clip_id: str, vision_token: str | None = None
    ) -> CommitVisionResult:
        params: dict[str, object] = {"visual": dict(visual), "clip_id": clip_id}
        if vision_token is not None:
            params["vision_token"] = vision_token
        return self._action(CommitVisionResult, "media_analysis", "commit_vision", params)

    def deepen(
        self, clip_id: str, *, confirm_token: str | None = None
    ) -> DeepenResult:
        params: dict[str, object] = {"clip_id": clip_id}
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        return self._action(
            DeepenResult, "media_analysis", "deepen", params, timeout_seconds=120.0
        )

    def commit_shot_vision(
        self,
        shots: Sequence[Mapping[str, object]],
        *,
        clip_id: str,
        vision_token: str | None = None,
    ) -> CommitShotResult:
        params: dict[str, object] = {
            "shots": [dict(shot) for shot in shots],
            "clip_id": clip_id,
        }
        if vision_token is not None:
            params["vision_token"] = vision_token
        return self._action(CommitShotResult, "media_analysis", "commit_shot_vision", params)

    def build_embeddings(
        self, *, clip_id: str | None = None, kinds: Sequence[str] = ("text",)
    ) -> McpActionOutcome:
        params: dict[str, object] = {"kinds": list(kinds)}
        if clip_id is not None:
            params["clip_id"] = clip_id
        return self._action(
            McpActionOutcome, "media_analysis", "build_embeddings", params,
            timeout_seconds=120.0,
        )

    def find_similar(
        self, *, text: str | None = None, clip_id: str | None = None, limit: int = 5
    ) -> FindSimilarResult:
        params: dict[str, object] = {"kind": "text", "limit": limit}
        if text is not None:
            params["text"] = text
        if clip_id is not None:
            params["clip_id"] = clip_id
        return self._action(
            FindSimilarResult, "media_analysis", "find_similar", params, timeout_seconds=120.0
        )

    # -- edit engine / color ------------------------------------------------

    def edit_engine_plan_selects(
        self, timeline_name: str | None = None
    ) -> EditPlanResult:
        params: dict[str, object] = (
            {} if timeline_name is None else {"timeline_name": timeline_name}
        )
        return self._action(EditPlanResult, "edit_engine", "plan_selects", params)

    def edit_engine_execute_selects(
        self, plan_id: str, *, confirm_token: str | None = None
    ) -> EditExecuteResult:
        params: dict[str, object] = {"plan_id": plan_id}
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        return self._action(
            EditExecuteResult,
            "edit_engine",
            "execute_selects",
            params,
            timeout_seconds=120.0,
        )

    def safe_apply_drx(  # noqa: PLR0913 (vendor action contract: path + guard + explicit item coords)
        self,
        path: str,
        *,
        dry_run: bool = False,
        grade_mode: int = 0,
        confirm_token: str | None = None,
        track_type: str = "video",
        track_index: int = 1,
        item_index: int = 0,
    ) -> DrxApplyResult:
        """Apply a DRX grade to ONE explicitly addressed timeline item.

        The vendor resolves items by (track_type, track_index, item_index)
        and silently defaults to V1/item0 — explicit coordinates are the
        only way an apply never lands on an implicit current clip.
        """

        params: dict[str, object] = {
            "path": path,
            "dry_run": dry_run,
            "grade_mode": grade_mode,
            "track_type": track_type,
            "track_index": track_index,
            "item_index": item_index,
        }
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        return self._action(
            DrxApplyResult,
            "timeline_item_color",
            "safe_apply_drx",
            params,
            timeout_seconds=120.0,
        )

    def grade_version_snapshot(
        self,
        *,
        track_type: str = "video",
        track_index: int = 1,
        item_index: int = 0,
    ) -> GradeVersionSnapshotResult:
        return self._action(
            GradeVersionSnapshotResult,
            "timeline_item_color",
            "grade_version_snapshot",
            {"track_type": track_type, "track_index": track_index, "item_index": item_index},
        )

    def probe_node_graph(
        self,
        *,
        track_type: str = "video",
        track_index: int = 1,
        item_index: int = 0,
        max_nodes: int = 8,
    ) -> NodeGraphResult:
        return self._action(
            NodeGraphResult,
            "timeline_item_color",
            "probe_node_graph",
            {
                "source": "item",
                "include_nodes": True,
                "max_nodes": max_nodes,
                "track_type": track_type,
                "track_index": track_index,
                "item_index": item_index,
            },
        )

    def add_grade_version(
        self,
        name: str,
        *,
        track_type: str = "video",
        track_index: int = 1,
        item_index: int = 0,
    ) -> McpActionOutcome:
        return self._action(
            McpActionOutcome,
            "timeline_item_color",
            "add_version",
            {
                "name": name,
                "type": 0,
                "track_type": track_type,
                "track_index": track_index,
                "item_index": item_index,
            },
        )

    # -- internals ----------------------------------------------------------

    def _action[T: McpActionOutcome](
        self,
        result_type: type[T],
        tool: str,
        action: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float = DEFAULT_OP_TIMEOUT_SECONDS,
    ) -> T:
        result = self._client._call_action_json(  # noqa: SLF001 (package-private parse seam)
            tool, action, params, timeout_seconds=timeout_seconds
        )
        return result_type.model_validate(result)


__all__ = ["DEFAULT_OP_TIMEOUT_SECONDS", "McpOps"]
