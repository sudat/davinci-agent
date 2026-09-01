"""Todo-48 acceptance: single-writer Clean Builder offline fault rig + live build.

Offline (``-m 'not resolve_live'``): the fake bridge exercises the verified
operation surface only — happy clean build with readback conformance and
published evidence, mid-build kill seams with fresh-restart recovery, lease
exclusion, non-owned refusal, patch-like structural mutation refusal, stale
package refusal, and the Builder/State authority guard.

Live (``-m resolve_live``): a real package compiled by the Todo-47 compiler
from the frozen Phase-0A manifest is built fresh in Resolve, read back
conformant, rendered to CompletionPercentage==100, subtitle-paired as
mov_text, restarted after a mid-build interruption, and lease-excluded.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from services.build.builder_models import (
    BuilderWiring,
    BuildFailure,
    BuildInterrupted,
    BuildOutput,
    BuildSeams,
    NoopSeams,
    RenderTiming,
)
from services.build.builder_recover import verify_package_current
from services.build.builder_render import PinnedBuildTools
from services.build.builder_session import (
    BUILD_PROJECT_PREFIX,
    create_staging,
    delete_staging,
)
from services.build.clean_builder import CleanBuilder
from services.foundation_io import canonical_model_bytes
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.lifecycle import NonOwnedResourceError, project_names
from services.resolve_bridge.readiness import load_host_report
from tests.build.fakes import FakeBuildManager, FakeBuildTools
from tests.build.support import (
    DictPackageRegistry,
    MemoryLease,
    SqliteLease,
    compile_build_package,
    fake_connection,
    killing_seams,
    manifest_0a,
    registry_for,
    write_offline_media,
)
from tests.resolve_locale import native_bridge_locale

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_adapter.models import ResolvePackage

BUILD_SERVICE_DIR: Final = Path("services/build")
VERIFIED_BRIDGE_CALLS: Final = frozenset(
    {
        # project manager
        "CreateProject", "SaveProject", "CloseProject", "DeleteProject",
        "GetCurrentProject", "GetProjectListInCurrentFolder", "LoadProject",
        # project
        "GetName", "GetMediaPool", "SetSetting", "GetSetting", "GetTimelineCount",
        "GetTimelineByIndex", "SetCurrentTimeline", "SetCurrentRenderFormatAndCodec",
        "SetRenderSettings", "AddRenderJob", "StartRendering", "StopRendering",
        "GetRenderJobStatus", "GetRenderJobList",
        # media pool
        "CreateEmptyTimeline", "ImportMedia", "AppendToTimeline",
        # timeline
        "GetTrackCount", "AddTrack", "DeleteTrack", "GetItemListInTrack",
        "SetClipsLinked", "SetStartTimecode", "GetStartTimecode",
        # timeline-item and media-pool-item readback
        "GetUniqueId", "GetStart", "GetEnd", "GetDuration", "GetSourceStartFrame",
        "GetTrackTypeAndIndex", "GetLinkedItems", "GetMediaPoolItem", "GetClipProperty",
        # connection
        "project_manager",
    }
)
FORBIDDEN_MUTATION_TOKENS: Final = (
    "MoveClips", "SetClipProperty", "SetClipColor", "SetClipSpeed", "SlipClip",
    "TrimClip", "SetClipStartFrame", "GetSourceEndFrame",
)


def staging_orphans(manager: FakeBuildManager) -> tuple[str, ...]:
    return tuple(
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(BUILD_PROJECT_PREFIX)
    )


def rehashed(package: ResolvePackage, **updates: object) -> ResolvePackage:
    """Re-apply a mutation and repair the package's content-hash chain."""

    mutated = package.model_copy(update=updates)
    zeroed = mutated.model_copy(update={"content_hash": "0" * 64})
    return mutated.model_copy(
        update={"content_hash": hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()}
    )


def run_offline_build(  # noqa: PLR0913 (fault-rig knobs mirror the Todo 15/16 pattern)
    tmp_path: Path,
    package: ResolvePackage,
    *,
    fault: str = "",
    status_fault: str = "",
    seams: BuildSeams | None = None,
    store: dict[str, str] | None = None,
    manager: FakeBuildManager | None = None,
) -> tuple[BuildOutput, FakeBuildManager, dict[str, str]]:
    bundle = tmp_path / "bundle"
    render_dir = tmp_path / "render"
    owned = (
        manager
        if manager is not None
        else FakeBuildManager(render_dir, fault=fault, status_fault=status_fault)
    )
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=render_dir,
        evidence_bundle=bundle,
        seams=seams if seams is not None else NoopSeams(),
        timing=RenderTiming(deadline_seconds=0.3, poll_seconds=0.01),
    )
    lease_store: dict[str, str] = store if store is not None else {}
    lease = MemoryLease(lease_store, "resolve-build:job-1", "builder-a")
    output = CleanBuilder(fake_connection(owned), wiring).build(package, lease)
    return output, owned, lease_store


@pytest.fixture
def offline_package(tmp_path: Path) -> ResolvePackage:
    media = write_offline_media(tmp_path / "media")
    return compile_build_package(media)


# ---------------------------------------------------------------- offline


def test_offline_happy_clean_build_publishes_and_cleans(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    output, manager, store = run_offline_build(tmp_path, offline_package)
    assert all(row.passed for row in output.items), [row.detail for row in output.items]
    assert len(output.items) == 8
    assert output.render.completion_percentage == 100
    assert Path(output.render.output_path).is_file()
    assert Path(output.render.output_path).read_bytes()
    assert output.subtitle is not None
    assert output.subtitle.codec == "mov_text"
    assert Path(output.subtitle.output_path).is_file()
    assert output.project_name.startswith(BUILD_PROJECT_PREFIX)
    assert output.project_name not in project_names(manager)
    assert not staging_orphans(manager)
    assert store == {}
    bundle = tmp_path / "bundle"
    recorded = (bundle / "build-output.json").read_bytes()
    assert type(output).model_validate_json(recorded) == output
    ledger = (bundle / "ledger.jsonl").read_text().splitlines()
    assert len(ledger) == 2
    assert (bundle / "head.json").is_file()


def test_offline_deterministic_fingerprint_across_builds(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    first, _, _ = run_offline_build(tmp_path / "one", offline_package)
    second, _, _ = run_offline_build(tmp_path / "two", offline_package)
    assert first.timeline_fingerprint == second.timeline_fingerprint
    assert first.render.output_sha256 == second.render.output_sha256
    assert first.items == second.items


def test_offline_mid_build_kill_seams_leave_orphans_recovered_fresh(tmp_path: Path) -> None:
    for seam in ("after_place", "after_readback"):
        work = tmp_path / seam
        media = write_offline_media(work / "media")
        package = compile_build_package(media)
        store: dict[str, str] = {}
        shared = FakeBuildManager(work / "render")
        with pytest.raises(BuildInterrupted):
            run_offline_build(work, package, seams=killing_seams(seam), store=store, manager=shared)
        orphans = staging_orphans(shared)
        assert orphans, f"{seam}: the killed build must leave its owned staging project"
        rebuilt, manager, _ = run_offline_build(work / "retry", package, manager=shared)
        assert rebuilt.project_name.startswith(BUILD_PROJECT_PREFIX)
        assert set(orphans) <= set(rebuilt.swept_projects)  # disposed, never resumed
        assert rebuilt.project_name not in staging_orphans(manager)
        assert not staging_orphans(manager)
        assert all(row.passed for row in rebuilt.items)
        assert store == {}


def test_offline_second_builder_while_lease_held_is_refused(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    store = {"resolve-build:job-1": "builder-a"}
    lease = MemoryLease(store, "resolve-build:job-1", "builder-b")
    wiring = BuilderWiring(
        registry=registry_for(offline_package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
    )
    manager = FakeBuildManager(tmp_path / "render")
    with pytest.raises(BuildFailure) as raised:
        CleanBuilder(fake_connection(manager), wiring).build(offline_package, lease)
    assert raised.value.code == "lease-held"
    assert not project_names(manager)
    assert store == {"resolve-build:job-1": "builder-a"}


def test_offline_non_owned_timeline_mutation_is_refused(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    manager = FakeBuildManager(tmp_path / "render")
    manager.CreateProject("Human Project")
    with pytest.raises(NonOwnedResourceError):
        delete_staging(manager, "Human Project")
    with pytest.raises(NonOwnedResourceError):
        create_staging(manager, "Human Project", offline_package)
    run_offline_build(tmp_path, offline_package)
    assert "Human Project" in manager.GetProjectListInCurrentFolder()
    assert not staging_orphans(manager)


def test_offline_patch_like_structural_mutation_is_refused() -> None:
    calls: set[str] = set()
    tokens: set[str] = set()
    for path in sorted(BUILD_SERVICE_DIR.glob("*.py")):
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr[0].isupper()
            ):
                calls.add(node.func.attr)
        for token in FORBIDDEN_MUTATION_TOKENS:
            if token in text:
                tokens.add(f"{path.name}:{token}")
    assert calls <= VERIFIED_BRIDGE_CALLS, sorted(calls - VERIFIED_BRIDGE_CALLS)
    assert tokens == set(), sorted(tokens)


def test_offline_stale_package_is_a_typed_refusal(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    with pytest.raises(BuildFailure) as unknown:
        verify_package_current(offline_package, DictPackageRegistry({}))
    assert unknown.value.code == "package-unknown"

    stale_registry = DictPackageRegistry({offline_package.artifact_id: "0" * 64})
    with pytest.raises(BuildFailure) as stale:
        verify_package_current(offline_package, stale_registry)
    assert stale.value.code == "package-stale"

    tampered = offline_package.model_copy(update={"content_hash": "ab" * 32})
    with pytest.raises(BuildFailure) as broken:
        verify_package_current(tampered, registry_for(offline_package))
    assert broken.value.code == "package-hash-invalid"

    wiring = BuilderWiring(
        registry=stale_registry,
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
    )
    manager = FakeBuildManager(tmp_path / "render")
    lease = MemoryLease({}, "resolve-build:job-1", "builder-a")
    with pytest.raises(BuildFailure) as refused:
        CleanBuilder(fake_connection(manager), wiring).build(offline_package, lease)
    assert refused.value.code == "package-stale"
    assert not project_names(manager)


def test_offline_record_below_origin_is_refused(offline_package: ResolvePackage) -> None:
    below = rehashed(
        offline_package,
        placements=tuple(
            placement.model_copy(
                update={
                    "clip_info": placement.clip_info.model_copy(update={"record_frame": 30})
                }
            )
            for placement in offline_package.placements
        ),
    )
    registry = DictPackageRegistry({below.artifact_id: below.content_hash})
    with pytest.raises(BuildFailure) as raised:
        verify_package_current(below, registry)
    assert raised.value.code == "record-below-origin"


def test_offline_readback_mismatch_is_a_typed_failure_with_cleanup(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    with pytest.raises(BuildFailure) as raised:
        run_offline_build(tmp_path, package, fault="missing_item")
    assert raised.value.code == "readback-mismatch"


def test_offline_false_render_complete_is_refused_bounded(
    tmp_path: Path, offline_package: ResolvePackage
) -> None:
    with pytest.raises(BuildFailure) as raised:
        run_offline_build(tmp_path, offline_package, status_fault="render_incomplete")
    assert raised.value.code == "render-incomplete"


def test_offline_media_hash_drift_is_a_typed_refusal(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    media["source"].write_bytes(b"tampered-bytes")
    with pytest.raises(BuildFailure) as raised:
        run_offline_build(tmp_path, package)
    assert raised.value.code == "media-hash-drift"


def test_offline_builder_writes_no_job_state() -> None:
    banned_roots: Final = ("services.job_runner", "services.review_command")
    banned_tokens: Final = ("StateStore", "StateLane", "ResolveBuildLane", "apply_transition")
    for path in sorted(BUILD_SERVICE_DIR.glob("*.py")):
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(banned_roots), f"{path}:{node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(banned_roots), f"{path}:{alias.name}"
        for token in banned_tokens:
            assert token not in text, f"{path}:{token}"


# ---------------------------------------------------------------- live


@dataclass(frozen=True, slots=True)
class LiveEnv:
    connection: ResolveConnection
    package: ResolvePackage
    manifest: Phase0AFixtureManifest
    ffmpeg: Path
    ffprobe: Path
    bundle: Path
    lease_db: Path


def _report_path(config: pytest.Config) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    evidence = config.getoption("--resolve-evidence")
    if evidence:
        candidate = Path(str(evidence)).resolve().parent / "resolve-host.json"
        if candidate.is_file():
            return candidate
    return None


def _media_bin(report_path: Path, name: str, env_key: str) -> Path:
    override = os.environ.get(env_key)
    return Path(override) if override else report_path.parent / "bootstrap/ffmpeg-7.1.1/bin" / name


@pytest.fixture(scope="session")
def live_connection(request: pytest.FixtureRequest) -> Iterator[ResolveConnection]:
    report_path = _report_path(request.config)
    if report_path is None or not report_path.is_file():
        pytest.skip(
            "live requires RESOLVE_HOST_REPORT or a resolve-host.json beside the attempt"
        )
    report = load_host_report(report_path)
    try:
        with native_bridge_locale():
            connection = launch_and_connect(report)
    except BridgeConnectionError as error:
        pytest.fail(f"live Resolve bridge unavailable and not launchable: {error}")
    yield connection
    try:
        manager = connection.project_manager()
        for name in project_names(manager):
            if name.startswith(BUILD_PROJECT_PREFIX):
                delete_staging(manager, name)
    except (BridgeConnectionError, TypeError):
        return


@pytest.fixture(scope="session")
def live_env(live_connection: ResolveConnection, request: pytest.FixtureRequest) -> LiveEnv:
    report_path = _report_path(request.config)
    assert report_path is not None
    media_dir = Path(
        os.environ.get("FVP_BUILD_MEDIA_DIR", str(report_path.parent / "phase-0a" / "fixture"))
    )
    media = {sid: media_dir / f"{sid}.mov" for sid in ("source", "intro", "outro")}
    missing = [str(path) for path in media.values() if not path.is_file()]
    if missing:
        pytest.fail(f"live fixture media missing: {missing}")
    ffmpeg = _media_bin(report_path, "ffmpeg", "FVP_FFMPEG_BIN")
    ffprobe = _media_bin(report_path, "ffprobe", "FVP_FFPROBE_BIN")
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        pytest.fail(f"pinned ffmpeg/ffprobe missing: {ffmpeg} / {ffprobe}")
    evidence = request.config.getoption("--resolve-evidence")
    bundle = Path(str(evidence)) / "build" if evidence else media_dir.parent / "build"
    bundle.mkdir(parents=True, exist_ok=True)
    return LiveEnv(
        connection=live_connection,
        package=compile_build_package(media),
        manifest=manifest_0a(),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        bundle=bundle,
        lease_db=bundle / "lease.sqlite3",
    )


def _live_builder(env: LiveEnv, *, seams: BuildSeams | None = None) -> CleanBuilder:
    wiring = BuilderWiring(
        registry=registry_for(env.package),
        tools=PinnedBuildTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe),
        ffmpeg_bin=env.ffmpeg,
        render_dir=env.bundle,
        evidence_bundle=env.bundle,
        seams=seams if seams is not None else NoopSeams(),
    )
    return CleanBuilder(env.connection, wiring)


def _lease_row_count(db_path: Path, resource: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(
            connection.execute(
                "select count(*) from leases where resource = ?", (resource,)
            ).fetchone()[0]
        )


@pytest.mark.resolve_live
def test_live_full_fresh_build_conforms_and_renders(live_env: LiveEnv) -> None:
    package = live_env.package
    resource = f"resolve-build:{package.artifact_id}"
    lease = SqliteLease(live_env.lease_db, resource, "clean-builder")
    output = _live_builder(live_env).build(package, lease)
    expected_render = live_env.manifest.expected.render_ffprobe

    assert all(row.passed for row in output.items), [row.detail for row in output.items]
    assert len(output.items) == 8
    assert output.items[0].record_start == 108000
    assert output.render.completion_percentage == 100
    assert Path(output.render.output_path).is_file()
    probe = output.render.probe
    assert probe.video_codec == expected_render.video.codec_name
    assert probe.width == expected_render.video.width
    assert probe.height == expected_render.video.height
    assert probe.r_frame_rate == expected_render.video.r_frame_rate
    assert probe.nb_frames == expected_render.video.nb_frames
    assert probe.audio_codec == expected_render.audio.codec_name
    assert probe.audio_sample_rate == int(expected_render.audio.sample_rate)
    assert probe.audio_channels == expected_render.audio.channels
    assert probe.duration is not None
    wanted = Fraction(
        expected_render.format.duration.num, expected_render.format.duration.den
    )
    assert wanted <= Fraction(probe.duration) <= wanted + Fraction(1, 2)
    assert output.subtitle is not None
    assert output.subtitle.codec == "mov_text"
    assert Path(output.subtitle.output_path).is_file()
    manager = live_env.connection.project_manager()
    assert not [n for n in project_names(manager) if n.startswith(BUILD_PROJECT_PREFIX)]
    assert (live_env.bundle / "build-output.json").is_file()
    assert (live_env.bundle / "ledger.jsonl").is_file()
    assert _lease_row_count(live_env.lease_db, resource) == 0
    print(f"\nlive render: {output.render.output_path} sha256={output.render.output_sha256}")
    assert output.subtitle is not None
    print(f"live subtitle: {output.subtitle.output_path} sha256={output.subtitle.output_sha256}")
    print(f"live fingerprint: {output.timeline_fingerprint}")


@pytest.mark.resolve_live
def test_live_restart_after_interrupt_rebuilds_fresh_and_cleans(live_env: LiveEnv) -> None:
    package = live_env.package
    resource = f"resolve-build:{package.artifact_id}-restart"
    lease = SqliteLease(live_env.lease_db, resource, "clean-builder")
    with pytest.raises(BuildInterrupted):
        _live_builder(live_env, seams=killing_seams("after_place")).build(package, lease)
    orphans = [
        n
        for n in project_names(live_env.connection.project_manager())
        if n.startswith(BUILD_PROJECT_PREFIX)
    ]
    assert orphans, "an interrupted build must leave its owned staging project behind"
    output = _live_builder(live_env).build(package, lease)
    assert all(row.passed for row in output.items)
    assert output.render.completion_percentage == 100
    assert set(orphans) <= set(output.swept_projects)
    assert not [
        n
        for n in project_names(live_env.connection.project_manager())
        if n.startswith(BUILD_PROJECT_PREFIX)
    ]


@pytest.mark.resolve_live
def test_live_lease_exclusion_refuses_second_builder(live_env: LiveEnv) -> None:
    package = live_env.package
    resource = f"resolve-build:{package.artifact_id}"
    holder = SqliteLease(live_env.lease_db, resource, "operator-a")
    holder.acquire()
    second = SqliteLease(live_env.lease_db, resource, "clean-builder")
    with pytest.raises(BuildFailure) as raised:
        _live_builder(live_env).build(package, second)
    assert raised.value.code == "lease-held"
    manager = live_env.connection.project_manager()
    assert not [n for n in project_names(manager) if n.startswith(BUILD_PROJECT_PREFIX)]
    assert _lease_row_count(live_env.lease_db, resource) == 1
    holder.release()
    assert _lease_row_count(live_env.lease_db, resource) == 0
