"""Spike adapter: build the 0A base cut in a fresh disposable timeline and read it back.

Imports fixture media into the Media Pool, places every linked A/V pair from the
fixed 0A Timeline IR via ``AppendToTimeline`` clipInfo records, links each pair,
and reads every placed item back through public timeline APIs. Phase-0A spike
harness only: it never edits an existing timeline incrementally; every run builds
into a brand-new ``__fvp_test__`` project and deletes it afterwards.

Live-verified readback caveats (Resolve 21.0.4): ``Timeline.GetStartFrame``/
``GetEndFrame`` return the timeline start timecode, not content extent, and
``TimelineItem.GetSourceEndFrame`` carries a float-floor artifact (299/300/599
for equivalent clips), so exact spans are read as ``GetSourceStartFrame() +
GetDuration()`` and item min/max record frames.
"""

from __future__ import annotations

import argparse
import os
import sys
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.resolve_bridge.base_cut_models import (
    BaseCutMediaPoolApi,
    BaseCutMediaPoolItemApi,
    BaseCutProjectApi,
    BaseCutRequest,
    BaseCutTimelineApi,
    BaseCutTimelineItemApi,
    ReadItem,
    TimelineSnapshot,
)
from services.resolve_bridge.base_cut_plan import BaseCutError, ordered_pairs
from services.resolve_bridge.lifecycle import (
    cleanup_owned_projects,
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.contracts.primitives import TrackKind
    from services.resolve_bridge.connection import ProjectApi, ResolveConnection

SUBTITLE_TRACKS: Final = 1
TIMELINE_RATE_SETTING: Final = "timelineFrameRate"
EXIT_UNAVAILABLE: Final = 4
PLACEMENT_FIELDS: Final = 2


def _frame(value: float, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BaseCutError(f"{what} returned a non-numeric value: {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise BaseCutError(f"{what} returned a fractional frame: {value!r}")
        return int(value)
    return value


def build_base_cut(connection: ResolveConnection, request: BaseCutRequest) -> TimelineSnapshot:
    if request.rate_den != 1:
        raise BaseCutError("0A spike supports integer timeline frame rates only")
    manager = connection.project_manager()
    try:
        project = create_disposable_project(manager, owned_project_name())
        _apply_timeline_rate(project, request.rate_num)
        timeline = cast(
            "BaseCutTimelineApi", create_owned_timeline(project, owned_timeline_name())
        )
        _ensure_track_layout(timeline, request)
        pool = cast("BaseCutMediaPoolApi", project.GetMediaPool())
        media = _import_media(pool, request)
        handles = _append_pairs(pool, request, media)
        _link_pairs(timeline, request, handles)
        return read_back(timeline)
    finally:
        cleanup_owned_projects(manager)


def _apply_timeline_rate(project: ProjectApi, num: int) -> None:
    settings = cast("BaseCutProjectApi", project)
    if not settings.SetSetting(TIMELINE_RATE_SETTING, str(num)):
        raise BaseCutError(f"SetSetting({TIMELINE_RATE_SETTING}) failed")
    if Fraction(settings.GetSetting(TIMELINE_RATE_SETTING)) != Fraction(num):
        raise BaseCutError(f"timeline frame rate readback mismatch: expected {num}")


def _ensure_track_layout(timeline: BaseCutTimelineApi, request: BaseCutRequest) -> None:
    needed = {"video": 0, "audio": 0, "subtitle": SUBTITLE_TRACKS}
    for item in request.items:
        needed[item.kind] = max(needed[item.kind], item.track_index)
    for track_type, want in needed.items():
        while timeline.GetTrackCount(track_type) < want:
            if not timeline.AddTrack(track_type):
                raise BaseCutError(f"AddTrack failed: {track_type}")
        while timeline.GetTrackCount(track_type) > want:
            if not timeline.DeleteTrack(track_type, timeline.GetTrackCount(track_type)):
                raise BaseCutError(f"DeleteTrack failed: {track_type}")


def _import_media(
    pool: BaseCutMediaPoolApi, request: BaseCutRequest
) -> dict[str, BaseCutMediaPoolItemApi]:
    paths = sorted(set(request.media.values()))
    imported = pool.ImportMedia(paths)
    if imported is None or len(imported) != len(paths):
        raise BaseCutError(
            f"ImportMedia returned {len(imported or [])} items for {len(paths)} files"
        )
    by_real: dict[str, BaseCutMediaPoolItemApi] = {}
    for item in imported:
        prop = item.GetClipProperty("File Path")
        if not isinstance(prop, str) or not prop:
            raise BaseCutError("imported media has no File Path property")
        by_real[os.path.realpath(prop)] = item
    media: dict[str, BaseCutMediaPoolItemApi] = {}
    for source_id, path in request.media.items():
        item = by_real.get(os.path.realpath(path))
        if item is None:
            raise BaseCutError(f"imported media does not cover {path}")
        media[source_id] = item
    return media


def _append_pairs(
    pool: BaseCutMediaPoolApi,
    request: BaseCutRequest,
    media: dict[str, BaseCutMediaPoolItemApi],
) -> dict[tuple[str, str], BaseCutTimelineItemApi]:
    handles: dict[tuple[str, str], BaseCutTimelineItemApi] = {}
    for video, audio in ordered_pairs(request):
        infos = [
            {
                "mediaPoolItem": media[video.source_id],
                "startFrame": video.source_start,
                "endFrame": video.source_end,
                "mediaType": 1,
                "trackIndex": video.track_index,
                "recordFrame": video.record_start,
            },
            {
                "mediaPoolItem": media[audio.source_id],
                "startFrame": audio.source_start,
                "endFrame": audio.source_end,
                "mediaType": 2,
                "trackIndex": audio.track_index,
                "recordFrame": audio.record_start,
            },
        ]
        added = pool.AppendToTimeline(infos)
        if added is None or len(added) != len(infos):
            raise BaseCutError(
                f"AppendToTimeline returned {len(added or [])} items for {video.av_link_id}"
            )
        for item, handle in zip((video, audio), added, strict=True):
            handles[(item.item_id, item.kind)] = handle
    return handles


def _link_pairs(
    timeline: BaseCutTimelineApi,
    request: BaseCutRequest,
    handles: dict[tuple[str, str], BaseCutTimelineItemApi],
) -> None:
    for video, audio in ordered_pairs(request):
        pair = [handles[(video.item_id, "video")], handles[(audio.item_id, "audio")]]
        if not timeline.SetClipsLinked(pair, True):
            raise BaseCutError(f"SetClipsLinked failed for {video.av_link_id}")


ITEM_KINDS: Final[tuple[TrackKind, TrackKind]] = ("video", "audio")
COUNT_KINDS: Final[tuple[str, str, str]] = ("video", "audio", "subtitle")


def read_back(timeline: BaseCutTimelineApi) -> TimelineSnapshot:
    counts = {kind: timeline.GetTrackCount(kind) for kind in COUNT_KINDS}
    items: list[ReadItem] = []
    for kind in ITEM_KINDS:
        for index in range(1, counts[kind] + 1):
            items.extend(
                _read_item(raw, kind) for raw in timeline.GetItemListInTrack(kind, index) or ()
            )
    starts = [item.record_start for item in items]
    ends = [item.record_end for item in items]
    return TimelineSnapshot(
        video_track_count=counts["video"],
        audio_track_count=counts["audio"],
        subtitle_track_count=counts["subtitle"],
        record_frame_count=(max(ends) - min(starts)) if items else 0,
        items=tuple(items),
    )


def _read_item(raw: BaseCutTimelineItemApi, kind: TrackKind) -> ReadItem:
    placement = raw.GetTrackTypeAndIndex()
    if len(placement) != PLACEMENT_FIELDS or placement[0] != kind:
        raise BaseCutError(f"item track type mismatch: {placement!r}")
    index = placement[1]
    if isinstance(index, bool) or not isinstance(index, (int, float)):
        raise BaseCutError(f"track index is not numeric: {index!r}")
    prop = raw.GetMediaPoolItem().GetClipProperty("File Path")
    if not isinstance(prop, str):
        raise BaseCutError("timeline item media has no File Path")
    return ReadItem(
        kind=kind,
        track_index=_frame(index, "track index"),
        record_start=_frame(raw.GetStart(False), "GetStart"),
        record_end=_frame(raw.GetEnd(False), "GetEnd"),
        source_start=_frame(raw.GetSourceStartFrame(), "GetSourceStartFrame"),
        source_end=_frame(
            raw.GetSourceStartFrame() + raw.GetDuration(False), "source end (start + duration)"
        ),
        media_path=prop,
        unique_id=raw.GetUniqueId(),
        linked_ids=frozenset(link.GetUniqueId() for link in raw.GetLinkedItems() or ()),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        from services.resolve_bridge.base_cut_faults import run_fault_cli  # noqa: PLC0415

        return run_fault_cli(Path(fault_fixture), arguments.manifest, arguments.fixture_dir)
    if arguments.report is None:
        print("--report is required outside fault mode", file=sys.stderr)
        return 2
    from services.resolve_bridge.base_cut_cli import run_cli  # noqa: PLC0415

    return run_cli(arguments.manifest, arguments.fixture_dir, arguments.report, arguments.evidence)


if __name__ == "__main__":
    raise SystemExit(main())
