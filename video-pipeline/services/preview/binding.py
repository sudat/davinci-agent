"""Frozen 0A binding -> preview Timeline IR + media bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
    TrackRef0C,
)
from services.foundation_io import sha256_file
from services.preview.models import (
    ItemBinding,
    MediaBinding,
    PreviewBindingError,
    PreviewMediaBindings,
)

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest

PREVIEW_PRODUCER: Final = Producer(name="pinned-ffmpeg-preview", version="1")
INTRO_SOURCE: Final = "intro"
SOURCE_SOURCE: Final = "source"
OUTRO_SOURCE: Final = "outro"
SUBTITLE_SOURCE: Final = "subtitle-table"
FILE_NAMES: Final[Mapping[str, str]] = {
    INTRO_SOURCE: "intro.mov",
    SOURCE_SOURCE: "source.mov",
    OUTRO_SOURCE: "outro.mov",
    SUBTITLE_SOURCE: "subtitle.srt",
}


@dataclass(frozen=True, slots=True)
class _ItemSpec:
    item_id: str
    kind: Literal["video", "audio", "subtitle"]
    source_id: str
    source_span: tuple[int, int]
    record_span: tuple[int, int]
    av_link_id: str | None = None
    subtitle_text: str | None = None


def _ir_item(spec: _ItemSpec, rate: RationalFrameRate) -> TimelineItem0C:
    return TimelineItem0C(
        item_id=spec.item_id,
        kind=spec.kind,
        source=SourceRef(
            source_id=spec.source_id,
            span=SourceFrameSpan(
                start_frame=spec.source_span[0], end_frame=spec.source_span[1], rate=rate
            ),
        ),
        record_span=RecordFrameSpan(start_frame=spec.record_span[0], end_frame=spec.record_span[1]),
        av_link_id=spec.av_link_id,
        subtitle_text=spec.subtitle_text,
    )


def _ir_digest(rate: RationalFrameRate, tracks: tuple[TimelineTrack0C, ...]) -> str:
    payload = json.dumps(
        {
            "rate": {"num": rate.num, "den": rate.den},
            "tracks": [track.model_dump(mode="json") for track in tracks],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def initial_timeline_ir(
    manifest: Phase0AFixtureManifest, artifact_id: str = "timeline-ir-preview-initial-0a"
) -> TimelineIr0C:
    """Project the frozen 0A record layout into the 0C Timeline IR subset."""

    recipe = manifest.recipe
    rate = RationalFrameRate(num=recipe.source.frame_rate.num, den=recipe.source.frame_rate.den)
    video_specs: list[_ItemSpec] = [
        _ItemSpec(
            item_id="intro-001",
            kind="video",
            source_id=INTRO_SOURCE,
            source_span=(0, recipe.intro.duration_frames),
            record_span=(
                recipe.intro.record_span.start_frame,
                recipe.intro.record_span.end_frame,
            ),
            av_link_id=recipe.intro.av_link_id,
        ),
        *(
            _ItemSpec(
                item_id=cut.item_id,
                kind="video",
                source_id=SOURCE_SOURCE,
                source_span=(cut.source_span.start_frame, cut.source_span.end_frame),
                record_span=(cut.record_span.start_frame, cut.record_span.end_frame),
                av_link_id=cut.av_link_id,
            )
            for cut in recipe.cuts
        ),
        _ItemSpec(
            item_id="outro-001",
            kind="video",
            source_id=OUTRO_SOURCE,
            source_span=(0, recipe.outro.duration_frames),
            record_span=(
                recipe.outro.record_span.start_frame,
                recipe.outro.record_span.end_frame,
            ),
            av_link_id=recipe.outro.av_link_id,
        ),
    ]
    video = [_ir_item(spec, rate) for spec in video_specs]
    audio_specs = [
        _ItemSpec(
            item_id=f"a-{spec.item_id}",
            kind="audio",
            source_id=spec.source_id,
            source_span=spec.source_span,
            record_span=spec.record_span,
            av_link_id=spec.av_link_id,
        )
        for spec in video_specs
    ]
    audio = [_ir_item(spec, rate) for spec in audio_specs]
    subtitle = _ir_item(
        _ItemSpec(
            item_id="s-fixed-001",
            kind="subtitle",
            source_id=SUBTITLE_SOURCE,
            source_span=(
                recipe.subtitle.record_span.start_frame,
                recipe.subtitle.record_span.end_frame,
            ),
            record_span=(
                recipe.subtitle.record_span.start_frame,
                recipe.subtitle.record_span.end_frame,
            ),
            subtitle_text=recipe.subtitle.text,
        ),
        rate,
    )
    tracks = (
        TimelineTrack0C(track=TrackRef0C(kind="video", index=1), items=tuple(video)),
        TimelineTrack0C(track=TrackRef0C(kind="audio", index=2), items=tuple(audio)),
        TimelineTrack0C(track=TrackRef0C(kind="subtitle", index=3), items=(subtitle,)),
    )
    total = video[-1].record_span.end_frame
    expected_total = manifest.expected.readback.record_frame_count
    if total != expected_total:
        raise PreviewBindingError(
            f"initial 0A binding projects {total} record frames, "
            f"but the frozen manifest expects {expected_total}"
        )
    return TimelineIr0C(
        artifact_id=artifact_id,
        artifact_type="timeline_ir_0c",
        schema_version="timeline-ir-0c-v1",
        content_hash=_ir_digest(rate, tracks),
        producer=PREVIEW_PRODUCER,
        inputs=(),
        rate=rate,
        tracks=tracks,
    )


def _binding(path: Path) -> MediaBinding:
    if not path.is_file():
        raise PreviewBindingError(f"fixture media file missing: {path}")
    return MediaBinding(media_path=str(path), sha256=sha256_file(path))


def initial_bindings(fixture_dir: Path) -> PreviewMediaBindings:
    """Bind every 0A item to its frozen fixture medium plus the BGM fixture."""

    files = {
        "intro-001": INTRO_SOURCE,
        "a-intro-001": INTRO_SOURCE,
        "outro-001": OUTRO_SOURCE,
        "a-outro-001": OUTRO_SOURCE,
        "s-fixed-001": SUBTITLE_SOURCE,
    }
    for cut in ("cut-001", "cut-002"):
        files[cut] = SOURCE_SOURCE
        files[f"a-{cut}"] = SOURCE_SOURCE
    items = tuple(
        ItemBinding(item_id=item_id, binding=_binding(fixture_dir / FILE_NAMES[source]))
        for item_id, source in files.items()
    )
    return PreviewMediaBindings(items=items, bgm=_binding(fixture_dir / "pulse.wav"))


def bindings_for_ir(
    ir: TimelineIr0C, media_by_source: Mapping[str, Path], bgm_path: Path | None
) -> PreviewMediaBindings:
    """Bind every IR item through its source id; subtitle sources must be SRT media."""

    def media_for(source_id: str, item_id: str) -> Path:
        path = media_by_source.get(source_id)
        if path is None:
            raise PreviewBindingError(f"no media bound for source {source_id} (item {item_id})")
        return path

    items = tuple(
        ItemBinding(
            item_id=item.item_id,
            binding=_binding(media_for(item.source.source_id, item.item_id)),
        )
        for track in ir.tracks
        for item in track.items
    )
    return PreviewMediaBindings(
        items=items,
        bgm=_binding(bgm_path) if bgm_path is not None else None,
    )


__all__ = [
    "PREVIEW_PRODUCER",
    "bindings_for_ir",
    "initial_bindings",
    "initial_timeline_ir",
]
