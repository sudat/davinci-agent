"""Phase-2 gate input rig: manifest IRs, compile probes, media, declarations.

Every compile input derives from a frozen Phase-2 fixture manifest and the
frozen phase-2 toolchain lock. The fault probes are the manifest-declared
typed refusals (stale capability matrix, same-duration wrong media); the
clean compile is the positive control. Fixture edit-source media is
synthesized through the frozen Todo-44 deterministic recipe chain and the
materialized bytes become the live build binding of record — the manifest's
declared binding stays the compile-time fault baseline and is recorded
alongside so nothing is misrepresented.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

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
from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
from services.fixtures.manifest_phase2 import (
    PHASE_2_DERIVED_FROM,
    Phase2FixtureManifest,
)
from services.foundation_io import canonical_model_bytes, sha256_file
from services.resolve_adapter.models import MediaBinding, ResolvePackage
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.toolchain.models import (
    Phase1TechnicalToolchainLock,
    Phase2ToolchainLock,
    load_lock,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import BaseRecordRow

MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-2")
P1_MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-1-technical")
LOCK_PATH: Final = Path("config/toolchains/phase-2-v2.json")
P1_LOCK_PATH: Final = Path("config/toolchains/phase-1-technical-v2.json")
IR_PRODUCER: Final = Producer(name="phase2-gate-rig", version="1")
CUE_STYLE_REF: Final = "style-default-ja"
CUE_MIN_DURATION_FRAMES: Final = 15


def manifest_for(fixture_id: str) -> Phase2FixtureManifest:
    return Phase2FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def phase2_lock() -> Phase2ToolchainLock:
    lock = load_lock(LOCK_PATH)
    if not isinstance(lock, Phase2ToolchainLock):
        raise TypeError("the frozen phase-2 lock is not a Phase2ToolchainLock")
    return lock


def lock_sha256() -> str:
    return sha256_file(LOCK_PATH)


def stale_lock_for(manifest: Phase2FixtureManifest) -> Phase2ToolchainLock:
    """The stale-capability fault: the lock pins the manifest's stale matrix hash."""

    from services.fixtures.manifest_phase2 import StaleCapabilityFault  # noqa: PLC0415

    lock = phase2_lock()
    fault = manifest.fault
    if not isinstance(fault, StaleCapabilityFault):
        raise TypeError("not a stale-capability fixture")
    return lock.model_copy(
        update={
            "resolve_package": lock.resolve_package.model_copy(
                update={"capability_matrix_sha256": fault.declared_matrix_sha256}
            )
        }
    )


def wrong_media_presented(manifest: Phase2FixtureManifest) -> tuple[MediaBinding, ...]:
    """The wrong-media fault: same duration, different bytes."""

    from services.fixtures.manifest_phase2 import SameDurationWrongMediaFault  # noqa: PLC0415

    fault = manifest.fault
    if not isinstance(fault, SameDurationWrongMediaFault):
        raise TypeError("not a wrong-media fixture")
    return (
        MediaBinding(
            source_id=fault.substituted_source_id,
            path=manifest.base.declared_media.path,
            sha256=fault.presented_media_sha256,
            duration_frames=fault.presented_duration_frames,
        ),
    )


def declared_media(manifest: Phase2FixtureManifest) -> tuple[MediaBinding, ...]:
    return (MediaBinding.model_validate(manifest.base.declared_media.model_dump(mode="json")),)


def _item(row: BaseRecordRow, rate: RationalFrameRate, source_id: str) -> object:
    if row.kind == "subtitle":
        if row.subtitle_text is None:
            raise TypeError("subtitle row carries no text")
        return SubtitleCueItem(
            item_id=row.item_id,
            source=SourceRef(
                source_id=row.item_id,
                span=SourceFrameSpan(
                    start_frame=row.source_start, end_frame=row.source_end, rate=rate
                ),
            ),
            record_span=RecordFrameSpan(start_frame=row.record_start, end_frame=row.record_end),
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


def production_ir(manifest: Phase2FixtureManifest) -> TimelineIrProduction:
    """The validated production IR derived from the frozen manifest base table."""

    rate = RationalFrameRate(num=manifest.base.frame_rate_num, den=manifest.base.frame_rate_den)
    by_track: dict[tuple[str, int], list[object]] = {}
    for row in manifest.base.base_records:
        by_track.setdefault((row.kind, row.track_index), []).append(
            _item(row, rate, manifest.base.source_id)
        )
    tracks: list[TimelineTrackProduction] = []
    for kind in ("video", "audio", "subtitle"):
        for (track_kind, index), items in sorted(by_track.items()):
            if track_kind != kind:
                continue
            tracks.append(
                TimelineTrackProduction(
                    track=TrackRef0C(kind=kind, index=index),
                    items=tuple(items),  # pyright: ignore[reportArgumentType]
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


def compile_package(
    manifest: Phase2FixtureManifest,
    *,
    lock_override: Phase2ToolchainLock | None = None,
    presented_media: tuple[MediaBinding, ...] | None = None,
    media_binding: tuple[MediaBinding, ...] | None = None,
) -> ResolvePackage:
    request = PackageCompileRequest(
        ir=production_ir(manifest),
        lock=lock_override if lock_override is not None else phase2_lock(),
        lock_sha256=lock_sha256(),
        declared_media=media_binding if media_binding is not None else declared_media(manifest),
        presented_media=presented_media,
        artifact_id=f"resolve-package-{manifest.fixture_id}",
    )
    return compile_resolve_package(request)


def synthesize_fixture_media(manifest: Phase2FixtureManifest, out_dir: Path) -> MediaBinding:
    """Materialize the fixture edit source via the frozen deterministic recipe."""

    from services.cli.media import (  # noqa: PLC0415 (recipe chain import)
        synthesize_edit_source,
    )

    parent = Phase1TechnicalFixtureManifest.model_validate_json(
        (P1_MANIFEST_DIR / f"{PHASE_2_DERIVED_FROM[manifest.fixture_id]}.json").read_bytes()
    )
    parent_lock = load_lock(P1_LOCK_PATH)
    if not isinstance(parent_lock, Phase1TechnicalToolchainLock):
        raise TypeError("the phase-1 lock is not a Phase1TechnicalToolchainLock")
    if not (out_dir / "edit-source.mov").is_file():
        synthesize_edit_source(parent, parent_lock, out_dir)
    path = out_dir / "edit-source.mov"
    return MediaBinding(
        source_id=manifest.base.declared_media.source_id,
        path=str(path),
        sha256=sha256_file(path),
        duration_frames=manifest.base.declared_media.duration_frames,
    )


__all__ = [
    "LOCK_PATH",
    "MANIFEST_DIR",
    "compile_package",
    "declared_media",
    "lock_sha256",
    "manifest_for",
    "phase2_lock",
    "production_ir",
    "stale_lock_for",
    "synthesize_fixture_media",
    "wrong_media_presented",
]
