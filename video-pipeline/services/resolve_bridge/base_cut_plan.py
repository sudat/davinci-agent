"""Planning helpers: fixed 0A Timeline IR, request, and expected readback from the manifest."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
    StrictModel,
    TrackKind,
    TrackRef,
)
from services.contracts.timeline_ir import TimelineIr0A, TimelineItem0A, TimelineTrack0A
from services.foundation_io import canonical_model_bytes
from services.resolve_bridge.base_cut_models import BaseCutItem, BaseCutRequest

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest

SLATE_SOURCE_IDS: Final = {"av-intro": "intro", "av-outro": "outro"}
FIXTURE_MEDIA_FILES: Final = {"source": "source.mov", "intro": "intro.mov", "outro": "outro.mov"}
ITEM_KINDS: Final[tuple[TrackKind, TrackKind]] = ("video", "audio")


class BaseCutError(Exception):
    """The base-cut plan, build, or readback violated a required invariant."""


class ExpectedItem(StrictModel):
    item_id: str
    av_link_id: str
    kind: TrackKind
    track_index: int
    record_start: int
    record_end: int
    source_start: int
    source_end: int
    media_path: str


class ExpectedBaseCut(StrictModel):
    video_track_count: int
    audio_track_count: int
    subtitle_track_count: int
    record_frame_count: int
    items: tuple[ExpectedItem, ...]


class _IrCore(StrictModel):
    rate: RationalFrameRate
    tracks: tuple[TimelineTrack0A, ...]


def fixture_media_map(fixture_dir: Path) -> dict[str, str]:
    return {sid: str(fixture_dir / name) for sid, name in FIXTURE_MEDIA_FILES.items()}


def source_id_for(av_link_id: str) -> str:
    return SLATE_SOURCE_IDS.get(av_link_id, "source")


def expected_from_manifest(
    manifest: Phase0AFixtureManifest, media: dict[str, str]
) -> ExpectedBaseCut:
    readback = manifest.expected.readback
    items = tuple(
        ExpectedItem(
            item_id=row.item_id,
            av_link_id=row.av_link_id,
            kind=kind,
            track_index=row.video_track_index if kind == "video" else row.audio_track_index,
            record_start=row.record_span.start_frame,
            record_end=row.record_span.end_frame,
            source_start=row.source_span.start_frame,
            source_end=row.source_span.end_frame,
            media_path=os.path.realpath(media[source_id_for(row.av_link_id)]),
        )
        for row in readback.items
        for kind in ITEM_KINDS
    )
    return ExpectedBaseCut(
        video_track_count=readback.video_track_count,
        audio_track_count=readback.audio_track_count,
        subtitle_track_count=readback.subtitle_track_count,
        record_frame_count=readback.record_frame_count,
        items=items,
    )


def ir_from_manifest(manifest: Phase0AFixtureManifest) -> TimelineIr0A:
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    track_specs: tuple[tuple[TrackKind, str], ...] = (
        ("video", "video_track_index"),
        ("audio", "audio_track_index"),
    )
    tracks: list[TimelineTrack0A] = []
    for kind, index_field in track_specs:
        items = tuple(
            TimelineItem0A(
                item_id=row.item_id,
                kind=kind,
                source=SourceRef(
                    source_id=source_id_for(row.av_link_id),
                    span=SourceFrameSpan(
                        start_frame=row.source_span.start_frame,
                        end_frame=row.source_span.end_frame,
                        rate=rate,
                    ),
                ),
                record_span=RecordFrameSpan(
                    start_frame=row.record_span.start_frame, end_frame=row.record_span.end_frame
                ),
                av_link_id=row.av_link_id,
            )
            for row in manifest.expected.readback.items
        )
        index = max(getattr(row, index_field) for row in manifest.expected.readback.items)
        tracks.append(TimelineTrack0A(track=TrackRef(kind=kind, index=index), items=items))
    core = _IrCore(rate=rate, tracks=tuple(tracks))
    return TimelineIr0A(
        artifact_id="p0a-cfr30-fixed-base-cut-ir",
        artifact_type="timeline_ir_0a",
        schema_version="timeline-ir-0a-v1",
        content_hash=hashlib.sha256(canonical_model_bytes(core)).hexdigest(),
        producer=Producer(name="resolve-bridge-spike", version="0.1.0"),
        inputs=(),
        rate=rate,
        tracks=tuple(tracks),
    )


def request_from_ir(
    ir: TimelineIr0A, media: dict[str, str], *, require_files: bool = True
) -> BaseCutRequest:
    items = tuple(
        BaseCutItem(
            item_id=entry.item_id,
            kind=entry.kind,
            source_id=entry.source.source_id,
            source_start=entry.source.span.start_frame,
            source_end=entry.source.span.end_frame,
            record_start=entry.record_span.start_frame,
            record_end=entry.record_span.end_frame,
            track_index=track.track.index,
            av_link_id=entry.av_link_id or "",
        )
        for track in ir.tracks
        for entry in track.items
    )
    request = BaseCutRequest(
        rate_num=ir.rate.num, rate_den=ir.rate.den, items=items, media=media
    )
    for item in request.items:
        if item.source_id not in media:
            raise BaseCutError(f"media map is missing source {item.source_id}")
        if require_files and not Path(media[item.source_id]).is_file():
            raise BaseCutError(f"fixture media missing: {media[item.source_id]}")
    _validate_pairs(request)
    return request


def _validate_pairs(request: BaseCutRequest) -> None:
    grouped: dict[str, list[BaseCutItem]] = {}
    for item in request.items:
        if not item.av_link_id:
            raise BaseCutError(f"0A base cut requires av_link_id on every item: {item.item_id}")
        grouped.setdefault(item.av_link_id, []).append(item)
    for link_id, group in grouped.items():
        if sorted(item.kind for item in group) != ["audio", "video"]:
            raise BaseCutError(f"av link {link_id} must pair exactly one video and one audio item")
        if len({(item.record_start, item.record_end) for item in group}) != 1:
            raise BaseCutError(f"av link {link_id} items disagree on record span")


def ordered_pairs(request: BaseCutRequest) -> tuple[tuple[BaseCutItem, BaseCutItem], ...]:
    by_link: dict[str, dict[str, BaseCutItem]] = {}
    for item in request.items:
        by_link.setdefault(item.av_link_id, {})[item.kind] = item
    return tuple(
        (by_link[link_id]["video"], by_link[link_id]["audio"])
        for link_id in sorted(by_link, key=lambda key: by_link[key]["video"].record_start)
    )
