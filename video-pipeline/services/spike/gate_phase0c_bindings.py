"""IR loading, preview projection, and media bindings for the 0C gate cases.

``load_ir_file`` rehydrates a store IR through strict model validation;
``preview_ir`` projects an audio-less store IR into a renderable preview
timeline (the Todo-27 adapter requires linked A/V); ``media_bindings`` binds
A/V items to the 0A edit-source mezzanine, generates a per-version SRT
subtitle table from the IR record spans, and attaches the BGM fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal

from services.contracts.timeline_ir import (
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
    TrackRef0C,
)
from services.foundation_io import atomic_write, sha256_file
from services.preview.models import ItemBinding, MediaBinding, PreviewMediaBindings
from services.preview.srt import expected_subtitle_cues, render_srt

EDIT_SOURCE_MEDIA: Final = "source.mov"
BGM_MEDIA: Final = "pulse.wav"


def _tuplize(value: object) -> object:
    """Coerce parsed JSON arrays to tuples for strict contract models."""

    if isinstance(value, list):
        return tuple(_tuplize(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuplize(item) for key, item in value.items()}
    return value


def load_ir_file(path: Path) -> TimelineIr0C:
    document = json.loads(path.read_bytes())
    return TimelineIr0C.model_validate(_tuplize(document))


def preview_ir(ir: TimelineIr0C) -> TimelineIr0C:
    """Project a store IR into a renderable preview timeline when needed.

    The frozen ``p0c-ambiguous-two-targets`` plan models video+subtitle only,
    while the Todo-27 preview adapter requires exactly one linked A/V pair.
    For such plans the projection appends an audio track mirroring the video
    items (same source/record spans, same mezzanine source); video and
    subtitle items are untouched, so trace coverage over the video track is
    identical to the store IR. IRs that already carry an audio track pass
    through unchanged.
    """

    if any(track.track.kind == "audio" for track in ir.tracks):
        return ir
    video = next(track for track in ir.tracks if track.track.kind == "video")
    audio_items = tuple(
        TimelineItem0C(
            item_id=f"a-{item.item_id}",
            kind="audio",
            source=item.source,
            record_span=item.record_span,
            av_link_id=item.av_link_id,
        )
        for item in video.items
    )
    audio_track = TimelineTrack0C(
        track=TrackRef0C(kind="audio", index=max(track.track.index for track in ir.tracks) + 1),
        items=audio_items,
    )
    return ir.model_copy(update={"tracks": (*ir.tracks, audio_track)})


def _subtitle_table(ir: TimelineIr0C, out_dir: Path, label: str) -> Path:
    subtitle_items = tuple(
        item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
    )
    cues = expected_subtitle_cues(subtitle_items, ir.rate)
    path = out_dir / f"subtitle-table-{label}.srt"
    atomic_write(path, render_srt(cues))
    return path


def _binding(path: Path) -> MediaBinding:
    if not path.is_file():
        raise FileNotFoundError(f"fixture media file missing: {path}")
    return MediaBinding(media_path=str(path), sha256=sha256_file(path))


def media_bindings(
    ir: TimelineIr0C,
    fixture_dir: Path,
    out_dir: Path,
    label: Literal["v1", "v2"],
) -> PreviewMediaBindings:
    """Bind every IR item: A/V through the 0A mezzanine, subtitles per-version SRT."""

    out_dir.mkdir(parents=True, exist_ok=True)
    table: Path | None = None
    items: list[ItemBinding] = []
    for track in ir.tracks:
        for item in track.items:
            if item.kind == "subtitle":
                if table is None:
                    table = _subtitle_table(ir, out_dir, label)
                media = table
            else:
                media = fixture_dir / EDIT_SOURCE_MEDIA
            items.append(ItemBinding(item_id=item.item_id, binding=_binding(media)))
    bgm_path = fixture_dir / BGM_MEDIA
    return PreviewMediaBindings(
        items=tuple(items),
        bgm=_binding(bgm_path) if bgm_path.is_file() else None,
    )


__all__ = [
    "BGM_MEDIA",
    "EDIT_SOURCE_MEDIA",
    "load_ir_file",
    "media_bindings",
    "preview_ir",
]
