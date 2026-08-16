"""Live capability probes backing the Phase-0A capability matrix findings.

One cheap disposable project records the raw API behaviors the matrix
documents: the ``GetSourceEndFrame`` float-floor artifact, the
timeline-absolute frame space (recordFrame origin 01:00:00:00), the raw
localized render-job status vocabulary, and the MarkIn/MarkOut echo in a
queued render job. Nothing here renders or mutates non-owned state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.base_cut import _append_pairs, _import_media
from services.resolve_bridge.base_cut_plan import ordered_pairs
from services.resolve_bridge.fixed_presentation import (
    apply_project_settings,
    ensure_fixed_layout,
    shifted_request,
)
from services.resolve_bridge.fixed_presentation_models import (
    FRAME_ORIGIN,
    TIMELINE_START_TC,
    FixedProjectApi,
    FixedTimelineApi,
)
from services.resolve_bridge.fixed_presentation_render import VIDEO_CODEC_KEY, VIDEO_FORMAT_KEY
from services.resolve_bridge.lifecycle import (
    cleanup_owned_projects,
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)
from services.spike.gate_models import PROBES_DIR, PROBES_NAME, CapabilityProbes
from services.spike.recovery import RecoveryError

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_bridge.base_cut_models import BaseCutMediaPoolApi, BaseCutRequest
    from services.resolve_bridge.connection import ResolveConnection


class ProbeItemApi(Protocol):
    def GetSourceStartFrame(self) -> int | float: ...

    def GetDuration(self, subframe_precision: bool) -> int | float: ...

    def GetSourceEndFrame(self) -> int | float: ...


PROBE_PAIR_INDEX: Final = 2
PROBE_RATE_DEN: Final = 1


class ProbeError(Exception):
    """A capability probe could not record its raw evidence."""


def _numeric(value: float, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError(f"{what} returned non-numeric value: {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ProbeError(f"{what} returned fractional frame: {value!r}")
        return int(value)
    return value


def run_capability_probes(
    connection: ResolveConnection,
    manifest: Phase0AFixtureManifest,
    request: BaseCutRequest,
    rate: RationalFrameRate,
    evidence: Path,
) -> CapabilityProbes:
    if rate.den != PROBE_RATE_DEN:
        raise ProbeError(f"probes support integer frame rates only (got {rate.num}/{rate.den})")
    manager = connection.project_manager()
    probes_dir = evidence / PROBES_DIR
    probes_dir.mkdir(parents=True, exist_ok=True)
    pairs = ordered_pairs(request)
    if len(pairs) <= PROBE_PAIR_INDEX:
        raise ProbeError("request lacks the probe pair")
    video, audio = pairs[PROBE_PAIR_INDEX]
    probe = shifted_request(request.model_copy(update={"items": (video, audio)}), FRAME_ORIGIN)
    try:
        project_api = create_disposable_project(manager, owned_project_name())
        project = cast("FixedProjectApi", project_api)
        apply_project_settings(project, manifest)
        timeline = cast(
            "FixedTimelineApi", create_owned_timeline(project_api, owned_timeline_name())
        )
        if not timeline.SetStartTimecode(TIMELINE_START_TC):
            raise ProbeError(f"SetStartTimecode({TIMELINE_START_TC}) failed")
        ensure_fixed_layout(timeline)
        pool = cast("BaseCutMediaPoolApi", project_api.GetMediaPool())
        media_items = _import_media(pool, probe)
        added = _append_pairs(pool, probe, media_items)
        probe_item = cast("ProbeItemApi", added[(video.item_id, "video")])
        raw_end = probe_item.GetSourceEndFrame()
        computed_end = _numeric(probe_item.GetSourceStartFrame(), "GetSourceStartFrame") + _numeric(
            probe_item.GetDuration(False), "GetDuration"
        )
        record_start = _numeric(timeline.GetStartFrame(), "GetStartFrame")
        record_end = _numeric(timeline.GetEndFrame(), "GetEndFrame")
        marks_in = FRAME_ORIGIN + video.record_start
        marks_out = FRAME_ORIGIN + video.record_end - 1
        if not project.SetCurrentRenderFormatAndCodec(VIDEO_FORMAT_KEY, VIDEO_CODEC_KEY):
            raise ProbeError("SetCurrentRenderFormatAndCodec failed (probe)")
        settings: dict[str, object] = {
            "TargetDir": str(probes_dir),
            "CustomName": "capability-probe",
            "MarkIn": marks_in,
            "MarkOut": marks_out,
            "SelectAllFrames": False,
        }
        if not project.SetRenderSettings(settings):
            raise ProbeError("SetRenderSettings failed (probe)")
        job_id = project.AddRenderJob()
        if not isinstance(job_id, str) or not job_id:
            raise ProbeError("AddRenderJob returned no job id (probe)")
        status_raw = project.GetRenderJobStatus(job_id)
        entry = next(
            (row for row in project.GetRenderJobList() if row.get("JobId") == job_id), None
        )
        if entry is None:
            raise ProbeError("probe render job missing from GetRenderJobList")
        probes = CapabilityProbes(
            source_end_frame_raw=repr(raw_end),
            source_end_frame_type=type(raw_end).__name__,
            source_end_frame_computed=computed_end,
            probe_item_id=video.item_id,
            probe_record_start=record_start,
            probe_record_end=record_end,
            frame_origin=FRAME_ORIGIN,
            job_status_raw=json.dumps(status_raw, sort_keys=True, ensure_ascii=False),
            job_marks_in=_echo_int(entry.get("MarkIn")),
            job_marks_out=_echo_int(entry.get("MarkOut")),
        )
    except RecoveryError as error:
        raise ProbeError(str(error)) from error
    finally:
        cleanup_owned_projects(manager)
    atomic_write(probes_dir / PROBES_NAME, canonical_model_bytes(probes))
    return probes


def _echo_int(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value)
    return None
