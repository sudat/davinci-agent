"""Package application on the staging timeline: media, placement, links, readback.

Placement uses only the verified AppendToTimeline clipInfo path — media
pool items with source span frames, an explicit Resolve track, and an
ABSOLUTE record frame at/above the timeline origin — appended one link
group at a time (the live-verified pair cadence). Every placed item is then
read back through public timeline APIs and compared field-by-field against
the package; any mismatch is a typed failure. There is deliberately no code
path that moves, trims, or otherwise structurally mutates an already placed
item: structural change means a fresh clean build.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import BuildFailure, ItemReadbackRow, as_frame
from services.resolve_adapter.models import AppendTrackMapEntry, RoleTrackMapEntry

if TYPE_CHECKING:
    from pathlib import Path

    from services.resolve_adapter.models import AppendPlacement, ResolvePackage
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolItemApi,
        BaseCutTimelineItemApi,
    )
    from services.resolve_bridge.fixed_presentation_models import (
        FixedMediaPoolApi,
        FixedTimelineApi,
    )

MEDIA_TYPE: Final = {"video": 1, "audio": 2}
PLACEMENT_FIELDS: Final = 2


def import_media(
    pool: FixedMediaPoolApi, paths: dict[str, Path]
) -> dict[str, BaseCutMediaPoolItemApi]:
    ordered = sorted({str(path) for path in paths.values()})
    imported = pool.ImportMedia(ordered)
    if imported is None or len(imported) != len(ordered):
        raise BuildFailure(
            "media-import-failed",
            f"ImportMedia returned {len(imported or [])} items for {len(ordered)} files",
        )
    by_real: dict[str, BaseCutMediaPoolItemApi] = {}
    for item in imported:
        prop = item.GetClipProperty("File Path")
        if not isinstance(prop, str) or not prop:
            raise BuildFailure("media-import-failed", "imported media has no File Path property")
        by_real[os.path.realpath(prop)] = item
    media: dict[str, BaseCutMediaPoolItemApi] = {}
    for source_id, path in paths.items():
        item = by_real.get(os.path.realpath(str(path)))
        if item is None:
            raise BuildFailure("media-import-failed", f"imported media does not cover {path}")
        media[source_id] = item
    return media


def ensure_track_layout(timeline: FixedTimelineApi, package: ResolvePackage) -> None:
    for kind in ("video", "audio"):
        indexes = [
            entry.resolve_track_index
            for entry in package.track_map
            if isinstance(entry, AppendTrackMapEntry | RoleTrackMapEntry)
            and entry.resolve_track_type == kind
        ]
        want = max(indexes, default=1)
        while timeline.GetTrackCount(kind) < want:
            if kind == "audio":
                added = timeline.AddTrack(kind, "stereo")
            else:
                added = timeline.AddTrack(kind)
            if not added:
                raise BuildFailure("track-layout-failed", f"AddTrack failed: {kind}")
        while timeline.GetTrackCount(kind) > want:
            if not timeline.DeleteTrack(kind, timeline.GetTrackCount(kind)):
                raise BuildFailure("track-layout-failed", f"DeleteTrack failed: {kind}")


def _placement_groups(package: ResolvePackage) -> list[list[AppendPlacement]]:
    link_of = {
        item_id: group.av_link_id
        for group in package.link_groups
        for item_id in group.item_ids
    }
    grouped: dict[str, list[AppendPlacement]] = {}
    for placement in package.placements:
        key = link_of.get(placement.item_id, f"solo:{placement.item_id}")
        grouped.setdefault(key, []).append(placement)
    return sorted(
        grouped.values(),
        key=lambda group: min(p.clip_info.record_frame for p in group),
    )


def _clip_info(
    placement: AppendPlacement, media: dict[str, BaseCutMediaPoolItemApi]
) -> dict[str, object]:
    info = placement.clip_info
    return {
        "mediaPoolItem": media[info.media_source_id],
        "startFrame": info.start_frame,
        "endFrame": info.end_frame,
        "mediaType": MEDIA_TYPE[info.track_type],
        "trackIndex": info.track_index,
        "recordFrame": info.record_frame,
    }


def place_items(
    pool: FixedMediaPoolApi,
    package: ResolvePackage,
    media: dict[str, BaseCutMediaPoolItemApi],
) -> dict[str, BaseCutTimelineItemApi]:
    handles: dict[str, BaseCutTimelineItemApi] = {}
    for group in _placement_groups(package):
        infos = [_clip_info(placement, media) for placement in group]
        added = pool.AppendToTimeline(infos)
        if added is None or len(added) != len(infos):
            ids = ",".join(placement.item_id for placement in group)
            raise BuildFailure(
                "placement-failed",
                f"AppendToTimeline returned {len(added or [])} items for [{ids}]",
            )
        for placement, item in zip(group, added, strict=True):
            handles[placement.item_id] = item
    return handles


def apply_link_groups(
    timeline: FixedTimelineApi,
    package: ResolvePackage,
    handles: dict[str, BaseCutTimelineItemApi],
) -> None:
    for group in package.link_groups:
        members = [handles[item_id] for item_id in group.item_ids]
        if not timeline.SetClipsLinked(members, True):  # noqa: FBT003 (Resolve API flag)
            raise BuildFailure("link-failed", f"SetClipsLinked failed for {group.av_link_id}")


def readback_verify(
    timeline: FixedTimelineApi,
    package: ResolvePackage,
    handles: dict[str, BaseCutTimelineItemApi],
) -> tuple[ItemReadbackRow, ...]:
    """Read every placed item back and compare it against the package."""

    del timeline  # items are read through their own handles
    declared = {
        binding.source_id: os.path.realpath(binding.path)
        for binding in package.inputs_view.declared_media
    }
    unique_of = {
        item_id: handles[item_id].GetUniqueId()
        for item_id in handles
    }
    expected_links: dict[str, frozenset[str]] = {}
    for group in package.link_groups:
        members = frozenset(unique_of[item_id] for item_id in group.item_ids)
        for item_id in group.item_ids:
            expected_links[item_id] = members - {unique_of[item_id]}
    rows: list[ItemReadbackRow] = []
    for placement in package.placements:
        item = handles.get(placement.item_id)
        if item is None:
            raise BuildFailure(
                "readback-mismatch", f"{placement.item_id}: no placed item handle to read back"
            )
        rows.append(_verify_one(placement, item, declared, expected_links))
    failed = [row for row in rows if not row.passed]
    if failed:
        detail = "; ".join(f"{row.item_id}: {row.detail}" for row in failed)
        raise BuildFailure("readback-mismatch", detail)
    return tuple(rows)


def _verify_one(
    placement: AppendPlacement,
    item: BaseCutTimelineItemApi,
    declared: dict[str, str],
    expected_links: dict[str, frozenset[str]],
) -> ItemReadbackRow:
    info = placement.clip_info
    what = placement.item_id
    source_start = as_frame(item.GetSourceStartFrame(), f"{what} source start")
    duration = as_frame(item.GetDuration(False), f"{what} duration")  # noqa: FBT003 (Resolve API flag)
    source_end = source_start + duration
    record_start = as_frame(item.GetStart(False), f"{what} record start")  # noqa: FBT003 (Resolve API flag)
    record_end = as_frame(item.GetEnd(False), f"{what} record end")  # noqa: FBT003 (Resolve API flag)
    track = item.GetTrackTypeAndIndex()
    linked = frozenset(link.GetUniqueId() for link in item.GetLinkedItems() or ())
    prop = item.GetMediaPoolItem().GetClipProperty("File Path")
    media_path = os.path.realpath(prop) if isinstance(prop, str) else "<non-string path>"
    problems: list[str] = []
    expected_record_end = info.record_frame + (info.end_frame - info.start_frame)
    if record_start != info.record_frame:
        problems.append(f"record start {record_start} != {info.record_frame}")
    if record_end != expected_record_end:
        problems.append(f"record end {record_end} != {expected_record_end}")
    if source_start != info.start_frame or source_end != info.end_frame:
        problems.append(
            f"source span [{source_start},{source_end}) != [{info.start_frame},{info.end_frame})"
        )
    if len(track) != PLACEMENT_FIELDS or track[0] != info.track_type:
        problems.append(f"track type {track!r} != {info.track_type!r}")
    else:
        index = track[1]
        if isinstance(index, bool) or not isinstance(index, int) or index != info.track_index:
            problems.append(f"track index {index!r} != {info.track_index}")
    if media_path != declared.get(info.media_source_id):
        problems.append(f"media {media_path} != declared {declared.get(info.media_source_id)}")
    wanted_links = expected_links.get(placement.item_id, frozenset())
    if linked != wanted_links:
        problems.append(f"links {sorted(linked)} != {sorted(wanted_links)}")
    return ItemReadbackRow(
        item_id=placement.item_id,
        kind=info.track_type,
        track_index=info.track_index,
        record_start=record_start,
        record_end=record_end,
        source_start=source_start,
        source_end=source_end,
        media_path=media_path,
        linked_ids=tuple(sorted(linked)),
        passed=not problems,
        detail="; ".join(problems),
    )


__all__ = [
    "apply_link_groups",
    "ensure_track_layout",
    "import_media",
    "place_items",
    "readback_verify",
]
