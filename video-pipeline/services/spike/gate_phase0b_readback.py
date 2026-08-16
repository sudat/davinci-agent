"""Live Resolve readback for one Phase-0B Edit Source (base-cut pattern).

Imports only the committed Edit Mezzanine (originals are refused by
:func:`require_edit_source` before any Resolve call), places the whole clip
plus one sub-clip per frozen readback marker into a disposable
``__fvp_test__`` timeline at CFR30, and reads every item back through public
timeline APIs. Exact spans are ``GetSourceStartFrame() + GetDuration()``;
record frames are timeline-absolute (origin 108000 at 30 fps) — both frozen
0A capability findings.
"""

from __future__ import annotations

import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.resolve_bridge.base_cut_plan import BaseCutError
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN
from services.resolve_bridge.lifecycle import (
    cleanup_owned_projects,
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)
from services.spike.gate_phase0b_models import (
    ANCHOR_WINDOW_FRAMES,
    READBACK_REPORT_NAME,
    LiveReadbackReport,
    Placement,
    ReadbackBindings,
    ReadbackItem,
    ReadbackObservation,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolApi,
        BaseCutMediaPoolItemApi,
        BaseCutProjectApi,
        BaseCutTimelineApi,
        BaseCutTimelineItemApi,
    )
    from services.resolve_bridge.connection import ResolveConnection

TIMELINE_RATE_SETTING: Final = "timelineFrameRate"
TIMELINE_RATE_NUM: Final = 30
VIDEO_TRACKS: Final = 1


class ClipPropertiesApi(Protocol):
    """Media-pool item exposing the full property dict (official API surface)."""

    def GetClipProperty(self, property_name: str = "") -> dict[str, object] | str: ...


class ReadbackRefusedError(Exception):
    """A requested media path is not a committed edit source; edit refused."""

    label = "original_refused_for_resolve_edit"


def require_edit_source(media: Path, record: NormalizeRecord) -> Path:
    """Refuse any path that is not exactly this record's committed output.

    VFR originals (and every other un-normalized file) must never enter a
    Resolve edit path; this guard runs before any Resolve API call.
    """

    resolved = media.resolve()
    output = Path(record.output.path).resolve()
    if resolved != output:
        raise ReadbackRefusedError(
            f"{media} is not the committed edit source of {record.artifact_id}; "
            "originals are never placed into Resolve edit paths"
        )
    if not resolved.is_file() or sha256_file(resolved) != record.output.sha256:
        raise ReadbackRefusedError(
            f"edit source {media} no longer hashes to its normalize record"
        )
    return resolved


def _frame(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BaseCutError(f"{what} returned a non-numeric value: {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise BaseCutError(f"{what} returned a fractional frame: {value!r}")
    return int(value)


def build_placements(manifest: Phase0BFixtureManifest, output_frames: int) -> tuple[Placement, ...]:
    whole = Placement(
        item_id="whole-clip", source_start=0, source_end=output_frames, record_start=FRAME_ORIGIN
    )
    placements = [whole]
    cursor = FRAME_ORIGIN + output_frames
    for marker in manifest.resolve_readback:
        start = marker.cfr30_frame
        if start is None or not 0 <= start < output_frames:
            raise BaseCutError(
                f"{marker.marker_id} anchor frame {start} outside [0, {output_frames})"
            )
        end = min(start + ANCHOR_WINDOW_FRAMES, output_frames)
        placements.append(
            Placement(
                item_id=marker.marker_id, source_start=start, source_end=end, record_start=cursor
            )
        )
        cursor += end - start
    return tuple(placements)


def _append_items(
    pool: BaseCutMediaPoolApi,
    media: BaseCutMediaPoolItemApi,
    placements: tuple[Placement, ...],
) -> tuple[BaseCutTimelineItemApi, ...]:
    infos = [
        {
            "mediaPoolItem": media,
            "startFrame": placement.source_start,
            "endFrame": placement.source_end,
            "mediaType": 1,
            "trackIndex": VIDEO_TRACKS,
            "recordFrame": placement.record_start,
        }
        for placement in placements
    ]
    added = pool.AppendToTimeline(infos)
    if added is None or len(added) != len(infos):
        raise BaseCutError(
            f"AppendToTimeline returned {len(added or [])} items for {len(infos)} placements"
        )
    return tuple(added)


def _read_item(raw: BaseCutTimelineItemApi, placement: Placement) -> ReadbackItem:
    source_start = _frame(raw.GetSourceStartFrame(), "GetSourceStartFrame")
    duration = _frame(raw.GetDuration(False), "GetDuration")
    prop = raw.GetMediaPoolItem().GetClipProperty("File Path")
    if not isinstance(prop, str) or not prop:
        raise BaseCutError("timeline item media has no File Path")
    return ReadbackItem(
        requested=placement,
        observed=ReadbackObservation(
            source_start=source_start,
            source_end=source_start + duration,
            record_start=_frame(raw.GetStart(False), "GetStart"),
            record_end=_frame(raw.GetEnd(False), "GetEnd"),
            media_path=prop,
        ),
    )


def _clip_properties(media: BaseCutMediaPoolItemApi) -> tuple[tuple[str, str], ...]:
    raw = cast("ClipPropertiesApi", media).GetClipProperty()
    if not isinstance(raw, dict):
        return ()
    return tuple(
        (str(key), str(value))
        for key, value in sorted(raw.items(), key=lambda pair: str(pair[0]))
        if isinstance(value, str | int | float | bool | type(None))
    )



def live_readback(
    connection: ResolveConnection,
    host_report_sha256: str,
    fixture_manifest_path: Path,
    fixture_manifest: Phase0BFixtureManifest,
    source_manifest: SourceManifest,
    record: NormalizeRecord,
    out_dir: Path,
    timeout_seconds: float = 300.0,
) -> LiveReadbackReport:
    """Drive one variant's live readback inside a disposable project."""

    deadline = time.monotonic() + timeout_seconds
    edit_source = require_edit_source(Path(record.output.path), record)
    placements = build_placements(fixture_manifest, record.drop_dup.expected.output_frames)
    manager = connection.project_manager()
    binding = connection.binding
    try:
        project = create_disposable_project(manager, owned_project_name())
        settings = cast("BaseCutProjectApi", project)
        if not settings.SetSetting(TIMELINE_RATE_SETTING, str(TIMELINE_RATE_NUM)):
            raise BaseCutError("SetSetting(timelineFrameRate) failed")
        if Fraction(str(settings.GetSetting(TIMELINE_RATE_SETTING))) != Fraction(TIMELINE_RATE_NUM):
            raise BaseCutError("timeline frame rate readback mismatch: expected 30")
        timeline = cast(
            "BaseCutTimelineApi", create_owned_timeline(project, owned_timeline_name())
        )
        while timeline.GetTrackCount("video") < VIDEO_TRACKS:
            if not timeline.AddTrack("video"):
                raise BaseCutError("AddTrack failed: video")
        pool = cast("BaseCutMediaPoolApi", project.GetMediaPool())
        imported = pool.ImportMedia([str(edit_source)])
        if imported is None or len(imported) != 1:
            raise BaseCutError(f"ImportMedia returned {len(imported or [])} items")
        media = imported[0]
        handles = _append_items(pool, media, placements)
        items = tuple(
            _read_item(handle, placement)
            for handle, placement in zip(handles, placements, strict=True)
        )
        if time.monotonic() > deadline:
            raise BaseCutError("live readback exceeded its bounded window")
        report = LiveReadbackReport(
            schema_version="phase-0b-readback-v1",
            fixture_id=fixture_manifest.fixture_id,
            bindings=ReadbackBindings(
                host_report_sha256=host_report_sha256,
                fixture_manifest_sha256=sha256_file(fixture_manifest_path),
                source_manifest_sha256=source_manifest.content_hash,
                normalize_record_sha256=record.content_hash,
                edit_source_sha256=record.output.sha256,
                resolve_version=binding.version_core,
                resolve_build=str(binding.build_number),
            ),
            timeline_rate_num=TIMELINE_RATE_NUM,
            timeline_rate_den=1,
            frame_origin=items[0].observed.record_start,
            items=items,
            clip_properties=_clip_properties(media)
            if fixture_manifest.rotation is not None
            else (),
            project_name=project.GetName(),
        )
    finally:
        try:
            cleanup_owned_projects(manager)
        except Exception as cleanup_error:  # noqa: BLE001 -- never mask the primary failure
            print(
                f"warning: post-readback owned cleanup failed: {cleanup_error}",
                file=sys.stderr,
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / READBACK_REPORT_NAME, canonical_model_bytes(report))
    return report
