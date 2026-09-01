"""Todo-47 rig: production IRs + package compile requests from frozen p2 fixtures.

Every compile input derives from a frozen Phase-2 fixture manifest (the
Phase-1-reference-derived base scenario) and the frozen phase-2 toolchain
lock. Golden comparisons read the independent ``phase-2`` golden tables —
never the compiler's own output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.fixtures.manifest_phase2 import Phase2FixtureManifest
from services.foundation_io import canonical_model_bytes, sha256_file
from services.resolve_adapter.models import MediaBinding
from services.toolchain.models import Phase2ToolchainLock, load_lock

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import BaseRecordRow

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-2")
GOLDEN_PATH = Path("tests/goldens/reference/phase-2/expected.json")
LOCK_PATH = Path("config/toolchains/phase-2-v2.json")
IR_PRODUCER = Producer(name="phase2-test-rig", version="1")
CUE_STYLE_REF = "style-default-ja"
CUE_MIN_DURATION_FRAMES = 15


def load_p2_manifest(fixture_id: str) -> Phase2FixtureManifest:
    return Phase2FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def golden_fixture(fixture_id: str) -> dict[str, object]:
    document: object = json.loads(GOLDEN_PATH.read_bytes())
    assert isinstance(document, dict)
    fixtures = document["fixtures"]
    assert isinstance(fixtures, dict)
    entry = fixtures[fixture_id]
    assert isinstance(entry, dict)
    return entry


def _item(
    row: BaseRecordRow, rate: RationalFrameRate, source_id: str
) -> TimelineItem0C | SubtitleCueItem:
    if row.kind == "subtitle":
        assert row.subtitle_text is not None
        return SubtitleCueItem(
            item_id=row.item_id,
            source=SourceRef(
                source_id=row.item_id,
                span=SourceFrameSpan(
                    start_frame=row.source_start,
                    end_frame=row.source_end,
                    rate=rate,
                ),
            ),
            record_span=RecordFrameSpan(
                start_frame=row.record_start, end_frame=row.record_end
            ),
            text=row.subtitle_text,
            lines=(row.subtitle_text,),
            style_ref=CUE_STYLE_REF,
            safe_area=True,
            min_duration_frames=CUE_MIN_DURATION_FRAMES,
        )
    return TimelineItem0C(
        item_id=row.item_id,
        kind=row.kind,
        source=SourceRef(
            source_id=source_id,
            span=SourceFrameSpan(
                start_frame=row.source_start, end_frame=row.source_end, rate=rate
            ),
        ),
        record_span=RecordFrameSpan(start_frame=row.record_start, end_frame=row.record_end),
        av_link_id=row.av_link_id,
        subtitle_text=None,
    )


def ir_for(manifest: Phase2FixtureManifest) -> TimelineIrProduction:
    rate = RationalFrameRate(num=manifest.base.frame_rate_num, den=manifest.base.frame_rate_den)
    by_track: dict[tuple[str, int], list[TimelineItem0C | SubtitleCueItem]] = {}
    for row in manifest.base.base_records:
        by_track.setdefault((row.kind, row.track_index), []).append(
            _item(row, rate, manifest.base.source_id)
        )
    kinds: tuple[Literal["video", "audio", "subtitle"], ...] = (
        "video",
        "audio",
        "subtitle",
    )
    tracks: list[TimelineTrackProduction] = []
    for kind in kinds:
        for (track_kind, index), items in sorted(by_track.items()):
            if track_kind != kind:
                continue
            tracks.append(
                TimelineTrackProduction(
                    track=TrackRef0C(kind=kind, index=index),
                    items=tuple(items),
                )
            )
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(rate))
    for track in tracks:
        digest.update(canonical_model_bytes(track))
    return TimelineIrProduction(
        artifact_id=f"timeline-ir-{manifest.fixture_id}",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash=digest.hexdigest(),
        producer=IR_PRODUCER,
        inputs=(),
        rate=rate,
        tracks=tuple(tracks),
    )


def phase2_lock() -> Phase2ToolchainLock:
    lock = load_lock(LOCK_PATH)
    assert isinstance(lock, Phase2ToolchainLock)
    return lock


def lock_sha256() -> str:
    return sha256_file(LOCK_PATH)


def declared_media(manifest: Phase2FixtureManifest) -> tuple[MediaBinding, ...]:
    return (MediaBinding.model_validate(manifest.base.declared_media.model_dump(mode="json")),)
