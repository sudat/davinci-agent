"""Todo-48 rig: real package compilation, leases, seams, and connections.

Offline and live tests share one compile path: a production Timeline IR
derived from the frozen Phase-0A fixture manifest is compiled through the
Todo-47 compiler against the frozen phase-2 toolchain lock, with declared
media bound to real files the builder can hash.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from services.build.builder_models import BuildInterrupted, LeaseHeld
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
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import canonical_model_bytes, sha256_file
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore
from services.resolve_adapter.models import MediaBinding
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.resolve_bridge.base_cut_faults import FakeBaseCutManager, FakeBaseCutResolve
from services.resolve_bridge.base_cut_plan import source_id_for
from services.resolve_bridge.connection import ResolveConnection, VersionBinding
from services.toolchain.models import Phase2ToolchainLock, load_lock

if TYPE_CHECKING:
    from services.build.builder_models import BuildSeams, PackageRegistry
    from services.resolve_adapter.models import ResolvePackage

MANIFEST_0A: Final = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
LOCK_PATH: Final = Path("config/toolchains/phase-2-v2.json")
IR_PRODUCER: Final = Producer(name="todo48-build-rig", version="1")
CUE_STYLE_REF: Final = "style-fixed-0a"
CUE_MIN_DURATION_FRAMES: Final = 15
MEDIA_DURATIONS: Final = {"source": 600, "intro": 30, "outro": 30}
OFFLINE_MEDIA: Final = {
    "source": b"offline-source-media",
    "intro": b"offline-intro-media",
    "outro": b"offline-outro-media",
}
ARTIFACT_ID: Final = "resolve-package-p0a-cfr30-fixed"


def manifest_0a() -> Phase0AFixtureManifest:
    return Phase0AFixtureManifest.model_validate_json(MANIFEST_0A.read_bytes())


def phase2_lock() -> Phase2ToolchainLock:
    lock = load_lock(LOCK_PATH)
    assert isinstance(lock, Phase2ToolchainLock)
    return lock


def production_ir(manifest: Phase0AFixtureManifest) -> TimelineIrProduction:
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    video: list[TimelineItem0C] = []
    audio: list[TimelineItem0C] = []
    for row in manifest.expected.readback.items:
        source_id = source_id_for(row.av_link_id)
        for kind, bucket in (("video", video), ("audio", audio)):
            bucket.append(
                TimelineItem0C(
                    item_id=f"{row.item_id}-{kind[0]}",
                    kind=cast("Literal['video', 'audio']", kind),
                    source=SourceRef(
                        source_id=source_id,
                        span=SourceFrameSpan(
                            start_frame=row.source_span.start_frame,
                            end_frame=row.source_span.end_frame,
                            rate=rate,
                        ),
                    ),
                    record_span=RecordFrameSpan(
                        start_frame=row.record_span.start_frame,
                        end_frame=row.record_span.end_frame,
                    ),
                    av_link_id=row.av_link_id,
                    subtitle_text=None,
                )
            )
    subtitle_recipe = manifest.recipe.subtitle
    assert subtitle_recipe is not None
    text = subtitle_recipe.text
    subtitle = SubtitleCueItem(
        item_id="sub-001",
        source=SourceRef(
            source_id="subtitle",
            span=SourceFrameSpan(start_frame=0, end_frame=60, rate=rate),
        ),
        record_span=RecordFrameSpan(
            start_frame=subtitle_recipe.record_span.start_frame,
            end_frame=subtitle_recipe.record_span.end_frame,
        ),
        text=text,
        lines=(text,),
        style_ref=CUE_STYLE_REF,
        safe_area=True,
        min_duration_frames=CUE_MIN_DURATION_FRAMES,
    )
    tracks = (
        TimelineTrackProduction(track=TrackRef0C(kind="video", index=1), items=tuple(video)),
        TimelineTrackProduction(track=TrackRef0C(kind="audio", index=2), items=tuple(audio)),
        TimelineTrackProduction(track=TrackRef0C(kind="subtitle", index=3), items=(subtitle,)),
    )
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(rate))
    for track in tracks:
        digest.update(canonical_model_bytes(track))
    return TimelineIrProduction(
        artifact_id="timeline-ir-p0a-cfr30-fixed",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash=digest.hexdigest(),
        producer=IR_PRODUCER,
        inputs=(),
        rate=rate,
        tracks=tracks,
    )


def media_bindings(paths: dict[str, Path]) -> tuple[MediaBinding, ...]:
    return tuple(
        MediaBinding(
            source_id=source_id,
            path=str(path),
            sha256=sha256_file(path),
            duration_frames=MEDIA_DURATIONS[source_id],
        )
        for source_id, path in sorted(paths.items())
    )


def write_offline_media(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for source_id, payload in OFFLINE_MEDIA.items():
        path = root / f"{source_id}.mov"
        path.write_bytes(payload)
        paths[source_id] = path
    return paths


def compile_build_package(
    paths: dict[str, Path], artifact_id: str = ARTIFACT_ID
) -> ResolvePackage:
    manifest = manifest_0a()
    return compile_resolve_package(
        PackageCompileRequest(
            ir=production_ir(manifest),
            lock=phase2_lock(),
            lock_sha256=sha256_file(LOCK_PATH),
            declared_media=media_bindings(paths),
            artifact_id=artifact_id,
            intro_outro_source_ids=frozenset({"intro", "outro"}),
        )
    )


class DictPackageRegistry:
    """Minimal PackageRegistry over a frozen artifact-id → content-hash map."""

    def __init__(self, hashes: dict[str, str]) -> None:
        self._hashes = dict(hashes)

    def expected_hash(self, artifact_id: str) -> str | None:
        return self._hashes.get(artifact_id)


def registry_for(package: ResolvePackage) -> PackageRegistry:
    return DictPackageRegistry({package.artifact_id: package.content_hash})


class MemoryLease:
    """Exclusive lease over one shared store; second holders are refused."""

    def __init__(self, store: dict[str, str], resource: str, holder: str) -> None:
        self._store = store
        self._resource = resource
        self._holder = holder

    def acquire(self) -> None:
        held = self._store.get(self._resource)
        if held is not None and held != self._holder:
            raise LeaseHeld(f"{self._resource} is held by {held}")
        self._store[self._resource] = self._holder

    def release(self) -> None:
        if self._store.get(self._resource) == self._holder:
            del self._store[self._resource]


class SqliteLease:
    """Live exclusive lease over the Todo-10 SQL lease table."""

    def __init__(self, db_path: Path, resource: str, holder: str, ttl_seconds: int = 3600) -> None:
        self._db_path = db_path
        self._resource = resource
        self._holder = holder
        self._ttl_seconds = ttl_seconds

    def acquire(self) -> None:
        now = int(time.time())
        with StateStore.open(self._db_path) as store:
            try:
                store.acquire_lease(
                    resource=self._resource,
                    holder=self._holder,
                    now=now,
                    ttl_seconds=self._ttl_seconds,
                )
            except StateStoreError as error:
                if error.code == "lease-held":
                    raise LeaseHeld(str(error)) from error
                raise

    def release(self) -> None:
        try:
            with StateStore.open(self._db_path) as store:
                store.release_lease(
                    resource=self._resource, holder=self._holder, now=int(time.time())
                )
        except StateStoreError:
            return


class KillingSeams:
    """Raises a simulated mid-build kill at the named seam."""

    def __init__(self, seam: str) -> None:
        self._seam = seam

    def after_place(self) -> None:
        if self._seam == "after_place":
            raise BuildInterrupted("kill simulated between place and readback")

    def after_readback(self) -> None:
        if self._seam == "after_readback":
            raise BuildInterrupted("kill simulated between readback and render")


def killing_seams(seam: str) -> BuildSeams:
    return KillingSeams(seam)


def fake_connection(manager: FakeBaseCutManager) -> ResolveConnection:
    return ResolveConnection(
        resolve=FakeBaseCutResolve(manager),
        binding=VersionBinding(
            product_name="DaVinci Resolve Studio",
            version_core="21.0.4",
            build_number=5,
            version_string="21.0.4.5",
        ),
    )
