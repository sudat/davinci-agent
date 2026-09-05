"""Typed result models for the MCP probe/parity operations surface (task 11).

These models sit BETWEEN the raw tool payloads and the application: every
:class:`~services.mcp_client.ops.McpOps` method parses its tool response into
one of these frozen models.  Unlike domain artifacts (``extra="forbid"`` in
:mod:`services.mcp_client.response_normalize`) these are *envelope-tolerant*
readback models — the live compound server adds keys freely between builds,
so unknown keys are ignored, but every field the probes assert on is REQUIRED
and therefore fails loudly when the server stops reporting it.
"""

# allow: SIZE_OK — pure flat readback-model table (one result class per
# pinned MCP action); splitting would separate the models from the ops table.

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, Field

from services.contracts.primitives import StrictModel, to_tuple


def _to_float(value: object) -> object:
    """Coerce an integer metric to float (strict mode rejects int for float)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _coerce_error(value: object) -> object:
    """Legacy handlers return ``{"error": "<message>"}`` with a STRING error."""
    if isinstance(value, str):
        return {"message": value}
    return value


class McpErrorEnvelope(StrictModel):
    """The compound server's structured error block (``_err`` shape)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    message: str
    code: str = "UNSPECIFIED"
    category: str = "resolve_api_failed"
    retryable: bool = False


class McpActionOutcome(StrictModel):
    """Base result: success flag plus the structured error envelope.

    ``ok`` is the derived verdict: a response is ok when it carries no error
    envelope and did not report ``success: false``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    success: bool | None = None
    error: Annotated[McpErrorEnvelope | None, BeforeValidator(_coerce_error)] = None
    status: str | None = None
    confirm_token: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.success is not False


class ProjectResult(McpActionOutcome):
    """``project_manager {create|get_current}`` readback."""

    name: str | None = None


class TimelineResult(McpActionOutcome):
    """``media_pool {create_timeline*}`` / ``timeline {get_current}`` readback."""

    name: str | None = None
    id: str | None = None
    created_new: bool | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    start_timecode: str | None = None


class ClipSummary(StrictModel):
    """One imported media-pool clip (``{name, id}`` from ``_clip_summaries``)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    name: str
    clip_id: str = Field(alias="id")

    @property
    def id(self) -> str:
        return self.clip_id


class ImportResult(McpActionOutcome):
    """``media_pool {safe_import_media}`` readback."""

    imported: int = 0
    clips: Annotated[tuple[ClipSummary, ...], BeforeValidator(to_tuple)] = ()


class AppendedItem(StrictModel):
    """One ``append_to_timeline`` result item."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    timeline_item_id: str | None = None


class AppendResult(McpActionOutcome):
    """``media_pool {append_to_timeline}`` readback (verified operation)."""

    count: int | None = None
    items: Annotated[tuple[AppendedItem, ...], BeforeValidator(to_tuple)] = ()
    verification_status: str | None = None


class StructureItem(StrictModel):
    """One item row of ``timeline {probe_timeline_structure}``.

    ``start``/``end`` are absolute RECORD frames (end exclusive);
    ``source_start``/``source_end`` are FILE-relative SOURCE frames (end
    exclusive, counted in the media's own rate — see ``source_fps``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    name: str | None = None
    timeline_item_id: str | None = None
    track_type: str | None = None
    track_index: int | None = None
    item_index: int | None = None
    start: int | None = None
    end: int | None = None
    duration: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    source_fps: Annotated[float | None, BeforeValidator(_to_float)] = None
    media_pool_item_id: str | None = None
    media_pool_item_name: str | None = None
    file_path: str | None = None


class TrackRow(StrictModel):
    """One track's row in the structure snapshot."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    track_index: int
    item_count: int
    items: Annotated[tuple[StructureItem, ...], BeforeValidator(to_tuple)] = ()


class TrackGroup(StrictModel):
    """All tracks of one ``track_type`` in the structure snapshot."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    track_count: int
    tracks: Annotated[tuple[TrackRow, ...], BeforeValidator(to_tuple)] = ()


class StructureSnapshot(McpActionOutcome):
    """``timeline {probe_timeline_structure}`` — the conform readback."""

    name: str
    id: str
    start_frame: int
    end_frame: int
    start_timecode: str
    item_count: int
    tracks: dict[str, TrackGroup]


class TransformReadback(McpActionOutcome):
    """``timeline_item {get_transform}`` values (punch-in readback)."""

    Pan: Annotated[float | None, BeforeValidator(_to_float)] = None
    Tilt: Annotated[float | None, BeforeValidator(_to_float)] = None
    ZoomX: Annotated[float | None, BeforeValidator(_to_float)] = None
    ZoomY: Annotated[float | None, BeforeValidator(_to_float)] = None
    RotationAngle: Annotated[float | None, BeforeValidator(_to_float)] = None


class AudioPropertyRow(StrictModel):
    """One property row of ``timeline {safe_set_audio_properties}``."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    requested: object = None
    original: object = None
    write: bool | None = None
    readback: object = None


class AudioPropertiesResult(McpActionOutcome):
    """``timeline {safe_set_audio_properties}`` readback."""

    results: dict[str, AudioPropertyRow] = Field(default_factory=dict)


class VoiceIsolationStateReadback(McpActionOutcome):
    """``timeline {get_voice_isolation_state}`` track state."""

    is_enabled: bool | None = Field(default=None, alias="isEnabled")
    amount: int | None = None


class VoiceIsolationReport(McpActionOutcome):
    """``timeline {voice_isolation_capabilities}`` readback.

    The two sub-blobs are opaque capability documentation dictionaries; the
    typed envelope proves the call shape, the probe logs the raw payload.
    """

    timeline_track: dict[str, object] | None = None
    item: dict[str, object] | None = None


def _preset_names(value: object) -> object:
    """Measured shapes only: a JSON array of names, or an index-keyed mapping
    of preset names (Resolve 21.0.4.5 answers ``{"presets": {}}`` when the
    host has no saved Fairlight presets and ``{"presets": {"0": "<name>"}}``
    — keys are the consecutive list positions, in order — once presets are
    saved). Any other shape fails loudly instead of being guessed into
    names."""
    if isinstance(value, dict):
        if not value:
            return ()
        names: list[str] = []
        for position, entry in enumerate(value.items()):
            key, name = entry
            if (
                not isinstance(key, str)
                or key != str(position)
                or not isinstance(name, str)
            ):
                raise ValueError(f"unobserved fairlight preset mapping shape: {value!r}")
            names.append(name)
        return tuple(names)
    return value


class FairlightPresetsReadback(McpActionOutcome):
    """``resolve_control {get_fairlight_presets}`` preset-name listing."""

    presets: Annotated[
        tuple[str, ...], BeforeValidator(_preset_names), BeforeValidator(to_tuple)
    ] = ()


class ApplyFairlightPresetResult(McpActionOutcome):
    """``project_settings {apply_fairlight_preset}`` outcome.

    Measured live: a missing preset answers ``{"success": false}`` with no
    error envelope — ``ok`` stays the sole verdict signal.
    """

    preset_name: str | None = None


class SubtitleProbeResult(McpActionOutcome):
    """``timeline {subtitle_generation_probe}`` readback."""

    would_generate: bool | None = None
    settings: dict[str, object] | None = None


class ProjectSettingReadback(McpActionOutcome):
    """``project_settings {get_setting}`` single-name readback.

    Live-measured 21.0.4.5: the single-name form returns the VALUE itself
    (``{"success": true, "settings": "smart"}``), not a name→value dict.
    """

    settings: object = None


class DrxApplyResult(McpActionOutcome):
    """``timeline_item_color {safe_apply_drx}`` readback.

    Covers the three flow arms: dry-run (``would_apply`` echo), the
    confirmation-required response (``McpActionOutcome.confirm_token`` +
    error envelope ``CONFIRMATION_REQUIRED``), and the applied outcome
    (``success``/``path``/``source``).
    """

    path: str | None = None
    source: str | None = None
    would_apply: bool | None = None


class GradeVersionSnapshotResult(McpActionOutcome):
    """``timeline_item_color {grade_version_snapshot}`` readback.

    ``current`` is the item's serialized current version (string or small
    object depending on the Resolve build — opaque identity, never parsed);
    ``local``/``remote`` are the named-version lists (type 0/1).
    """

    current: object = None
    local: Annotated[tuple[object, ...], BeforeValidator(to_tuple)] = ()
    remote: Annotated[tuple[object, ...], BeforeValidator(to_tuple)] = ()
    errors: Annotated[tuple[object, ...], BeforeValidator(to_tuple)] = ()


class NodeGraphRow(StrictModel):
    """One node row of ``timeline_item_color {probe_node_graph}``.

    Row keys (lut/cache_mode/label/tools) vary by graph state; the typed
    envelope pins the row shape the handler compares as a signature.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    node_index: int | None = None
    label: object = None
    lut: object = None
    tools: object = None


class NodeGraphResult(McpActionOutcome):
    """``timeline_item_color {probe_node_graph}`` readback."""

    available: bool | None = None
    num_nodes: int | None = None
    nodes: Annotated[tuple[NodeGraphRow, ...], BeforeValidator(to_tuple)] = ()
    source: str | None = None


class TrackCountResult(McpActionOutcome):
    """``timeline {get_track_count}`` readback."""

    count: int | None = None


class TrackItemRow(StrictModel):
    """One ``timeline {get_items_in_track}`` item row (absolute frames).

    ``kind`` appeared on items measured live 2026-09-05 (pin v2.207.0,
    sol-cu-integration evidence): observed value ``"transition"`` on a
    transition item; the vocabulary beyond that is unknown (not yet
    measured) — the raw string is accepted and never interpreted here.
    """

    name: str | None = None
    id: str | None = None
    start: int | None = None
    end: int | None = None
    duration: int | None = None
    kind: str | None = Field(default=None)


class TrackItemsResult(McpActionOutcome):
    """``timeline {get_items_in_track}`` readback."""

    items: Annotated[tuple[TrackItemRow, ...], BeforeValidator(to_tuple)] = ()


class SourceFrameResult(McpActionOutcome):
    """``timeline_item {get_source_start_frame|get_source_end_frame}`` readback."""

    frame: int | None = None


class MediaPoolItemResult(McpActionOutcome):
    """``timeline {get_media_pool_item}`` readback (current timeline)."""

    name: str | None = None
    id: str | None = None


class FusionInputRow(StrictModel):
    """One ``fusion_comp {safe_set_inputs}`` per-input result row."""

    success: bool | None = None
    value: object = None
    error: str | None = None


class FusionInputsResult(McpActionOutcome):
    """``fusion_comp {safe_set_inputs}`` readback."""

    tool_name: str | None = None
    results: dict[str, FusionInputRow] = Field(default_factory=dict)


class TextPlusReadback(McpActionOutcome):
    """``fusion_comp {get_text_plus}`` readback."""

    tool_name: str | None = None
    input_name: str | None = None
    text: str | None = None


class FusionInputValueReadback(McpActionOutcome):
    """``fusion_comp {get_input}`` readback (single input's current value)."""

    value: object = None


class FormatCodecResult(McpActionOutcome):
    """``render {set_format_and_codec|get_format_and_codec}`` readback."""

    format: str | None = None
    codec: str | None = None
    format_id: str | None = None
    codec_id: str | None = None


class RenderSettingsReadback(McpActionOutcome):
    """``render {get_settings}`` — the fields the parity structure needs."""

    FormatWidth: int | None = None
    FormatHeight: int | None = None
    FrameRate: Annotated[float | None, BeforeValidator(_to_float)] = None
    AudioCodec: str | None = None
    AudioSampleRate: int | None = None


class SourceRangeOccurrence(StrictModel):
    """One row of ``timeline {source_range_report}`` occurrences."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    key: str | None = None
    source_range: Annotated[tuple[int, ...] | None, BeforeValidator(to_tuple)] = None
    timeline_range: Annotated[tuple[int, ...] | None, BeforeValidator(to_tuple)] = None
    timeline_item_id: str | None = None
    track_type: str | None = None


class RenderJobStatus(McpActionOutcome):
    """``render {get_job_status}`` readback (wire keys are PascalCase,
    ``JobStatus`` is LOCALIZED by Resolve — compare via the percentage)."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    job_status: str | None = Field(default=None, alias="JobStatus")
    completion_percentage: Annotated[float | None, BeforeValidator(_to_float)] = Field(
        default=None, alias="CompletionPercentage"
    )
    is_rendering_in_progress: bool | None = Field(
        default=None, alias="IsRenderingInProgress"
    )
    #: Vendor progress signal present while a real render runs (measured on
    #: 21.0.4.5: {"JobStatus": "レンダリング", "CompletionPercentage": 88,
    #: "EstimatedTimeRemainingInMs": 107000}); absent on terminal payloads.
    estimated_time_remaining_ms: Annotated[float | None, BeforeValidator(_to_float)] = (
        Field(default=None, alias="EstimatedTimeRemainingInMs")
    )
    #: The vendor's explicit failed-job payload carries a PascalCase ``Error``
    #: message (measured: {"JobStatus": "失敗しました", "Error": "..."}).
    error_message: str | None = Field(default=None, alias="Error")


class RenderInProgressResult(McpActionOutcome):
    """``render {is_rendering}`` readback (global render-queue state).

    Measured pinned-MCP key shapes: the compound server answers
    ``{"rendering": bool}``, the granular tool ``{"is_rendering": bool}``.
    """

    rendering: bool | None = None
    is_rendering: bool | None = None


class RenderJobList(McpActionOutcome):
    """``render {list_jobs}`` readback."""

    jobs: Annotated[tuple[dict[str, object], ...], BeforeValidator(to_tuple)] = ()


class AddJobResult(McpActionOutcome):
    """``render {add_job|prepare_render_job}`` readback.

    ``prepare_render_job`` echoes the queued job's full settings dict; it
    is the only independent readback of what the job will render with.
    """

    job_id: str | None = None
    settings: dict[str, object] | None = None


class GapRecord(StrictModel):
    """One gap/overlap row from ``timeline {detect_gaps_overlaps}``."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    track_type: str | None = None
    track_index: int | None = None
    start: int | None = None
    end: int | None = None
    duration: int | None = None


class GapsOverlapsResult(McpActionOutcome):
    """``timeline {detect_gaps_overlaps}`` readback."""

    gaps: Annotated[tuple[GapRecord, ...], BeforeValidator(to_tuple)] = ()
    overlaps: Annotated[tuple[GapRecord, ...], BeforeValidator(to_tuple)] = ()


class MissingMediaResult(McpActionOutcome):
    """``timeline {detect_missing_media}`` readback."""

    missing_count: int | None = None


class SourceRangeResult(McpActionOutcome):
    """``timeline {source_range_report}`` readback.

    ``ranges`` maps a media key to its used SOURCE ranges (dict on the wire);
    ``occurrences`` lists per-item placements with source and timeline spans.
    """

    ranges: dict[str, object] | None = None
    occurrences: Annotated[
        tuple[SourceRangeOccurrence, ...], BeforeValidator(to_tuple)
    ] = ()


class AnalysisPlanResult(McpActionOutcome):
    """``media_analysis {analyze_clip}`` readback (dry-run plan or executed)."""

    pending_action: dict[str, object] | str | None = None
    manifest: dict[str, object] | None = None


class CommitVisionResult(McpActionOutcome):
    """``media_analysis {commit_vision}`` readback."""

    visual_json: str | None = None
    clip_dir: str | None = None
    analysis_json: str | None = None


class DeepenResult(McpActionOutcome):
    """``media_analysis {deepen}`` readback (estimate or deferred payload).

    The confirmed payload carries the real shot rows under ``shot_table``
    (each with shot_uuid, time bounds, and sampled frame indices).
    """

    confirm_token: str | None = None
    vision_token: str | None = None
    shot_table: Annotated[tuple[dict[str, object], ...], BeforeValidator(to_tuple)] = ()


class CommitShotResult(McpActionOutcome):
    """``media_analysis {commit_shot_vision}`` readback."""

    updated: int | None = None


class FindSimilarResult(McpActionOutcome):
    """``media_analysis {find_similar}`` readback."""

    results: Annotated[tuple[dict[str, object], ...], BeforeValidator(to_tuple)] = ()
    matches: Annotated[tuple[dict[str, object], ...], BeforeValidator(to_tuple)] = ()


class EditPlanResult(McpActionOutcome):
    """``edit_engine {plan_selects}`` readback."""

    plan_id: str | None = None


class EditExecuteResult(McpActionOutcome):
    """``edit_engine {execute_selects}`` readback."""

    executed: int | None = None


class CompCountResult(McpActionOutcome):
    """``timeline_item_fusion {get_comp_count}`` readback."""

    count: int | None = None


class CapabilityReport(McpActionOutcome):
    """Capability-report readbacks (edit kernel / audio mix).

    The sub-sections are opaque documentation dictionaries; the typed
    envelope proves the call shape and the probe logs the raw payload.
    """

    supported: dict[str, object] | None = None
    partially_supported: dict[str, object] | None = None
    unsupported: dict[str, object] | None = None
    capabilities: dict[str, object] | None = None


class RenderBoundaryReport(McpActionOutcome):
    """``render {export_render_boundary_report}`` readback."""

    capabilities: dict[str, object] | None = None
    settings: dict[str, object] | None = None


class ValidatedSettings(McpActionOutcome):
    """``render {validate_render_settings}`` readback (validated echo)."""

    valid: bool | None = None
    settings: dict[str, object] | None = None
    errors: Annotated[tuple[object, ...], BeforeValidator(to_tuple)] = ()
    warnings: Annotated[tuple[object, ...], BeforeValidator(to_tuple)] = ()


class SafeSetRenderResult(McpActionOutcome):
    """``render {safe_set_render_settings}`` readback (diff-validated set)."""

    validation: dict[str, object] | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    diff: dict[str, object] | None = None


class RenderFormatsResult(McpActionOutcome):
    """``render {get_formats}`` readback."""

    formats: dict[str, object] | None = None


class RenderCodecsResult(McpActionOutcome):
    """``render {get_codecs}`` readback."""

    codecs: dict[str, object] | None = None


__all__ = [
    "AddJobResult",
    "AnalysisPlanResult",
    "AppendResult",
    "AppendedItem",
    "AudioPropertiesResult",
    "AudioPropertyRow",
    "CapabilityReport",
    "ClipSummary",
    "CommitShotResult",
    "CommitVisionResult",
    "CompCountResult",
    "DeepenResult",
    "DrxApplyResult",
    "EditExecuteResult",
    "EditPlanResult",
    "FindSimilarResult",
    "FormatCodecResult",
    "GapRecord",
    "GapsOverlapsResult",
    "GradeVersionSnapshotResult",
    "ImportResult",
    "McpActionOutcome",
    "McpErrorEnvelope",
    "MissingMediaResult",
    "NodeGraphResult",
    "NodeGraphRow",
    "ProjectResult",
    "RenderBoundaryReport",
    "RenderCodecsResult",
    "RenderFormatsResult",
    "RenderInProgressResult",
    "RenderJobList",
    "RenderJobStatus",
    "RenderSettingsReadback",
    "SafeSetRenderResult",
    "SourceRangeOccurrence",
    "SourceRangeResult",
    "StructureItem",
    "StructureSnapshot",
    "SubtitleProbeResult",
    "TimelineResult",
    "TrackGroup",
    "TrackRow",
    "TransformReadback",
    "ValidatedSettings",
    "VoiceIsolationReport",
    "VoiceIsolationStateReadback",
]
