"""Todo-50 acceptance: item-level Package↔Resolve conformance and drift blocking.

Offline (``-m 'not resolve_live'``): the fake-bridge rig proves every item is
verified individually (a totals-only conformance table is structurally
rejected by the model and verdict pass flags are recomputed, never trusted),
malformed readback tables are refused, all six fault shapes classify as their
own drift kind and block without overwriting the prior timeline, real
(fake-API) timeline edits block the guard, a conforming orphan passes the
gate and rebuilds fresh, and a human mutation of the system's own recorded
staging timeline blocks the full builder.

Live (``-m resolve_live````): a real fixture package build emits a zero-delta
conformance table with the before/after fingerprints recorded; mutated
expectations detect each fault kind against a REAL orphan staging timeline
without overwriting it; and a REAL bridge edit of an owned staging timeline
is classified as human_mutation and the rebuild is blocked.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest
from pydantic import ValidationError

from services.build.builder_models import (
    BuilderWiring,
    BuildFailure,
    BuildInterrupted,
    BuildOutput,
    NoopSeams,
    RenderTiming,
)
from services.build.builder_render import PinnedBuildTools
from services.build.builder_session import delete_staging, staging_project_name
from services.build.clean_builder import CleanBuilder
from services.build.conformance import (
    ConformanceChecker,
    capture_readback,
    expected_fingerprint,
)
from services.build.conformance_capture import ReadbackRow, TimelineReadback
from services.build.conformance_models import ConformanceTable, ItemVerdict
from services.build.drift import DRIFT_ROUTES, DriftDetector, DriftReport, StagingDriftGuard
from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.models import LinkGroup
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ProjectManagerApi,
    ResolveConnection,
)
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.lifecycle import project_names
from services.resolve_bridge.readiness import load_host_report
from tests.build.fakes import FakeBuildManager, FakeBuildTools
from tests.build.support import (
    MemoryLease,
    SqliteLease,
    compile_build_package,
    fake_connection,
    killing_seams,
    registry_for,
    write_offline_media,
)
from tests.resolve_locale import native_bridge_locale

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.base_cut_models import BaseCutTimelineApi

FAULT_KINDS: Final = (
    "item_media_mismatch",
    "off_by_one",
    "wrong_track",
    "wrong_link",
    "missing_item",
    "extra_item",
)
ZERO_HASH: Final = "0" * 64


def rehashed(package: ResolvePackage, **updates: object) -> ResolvePackage:
    """Apply a mutation and repair the package's content-hash chain."""

    mutated = package.model_copy(update=updates)
    zeroed = mutated.model_copy(update={"content_hash": ZERO_HASH})
    return mutated.model_copy(
        update={"content_hash": hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()}
    )


def _placement_index(package: ResolvePackage, item_id: str) -> int:
    return next(i for i, p in enumerate(package.placements) if p.item_id == item_id)


def _fault_package(package: ResolvePackage, kind: str) -> ResolvePackage:
    """Mutate the fixture expectations by exactly one fault shape."""

    placements = list(package.placements)
    if kind == "item_media_mismatch":
        # intro-001-v demands outro media: identical 30-frame duration, wrong file
        index = _placement_index(package, "intro-001-v")
        info = placements[index].clip_info.model_copy(update={"media_source_id": "outro"})
        placements[index] = placements[index].model_copy(
            update={"media_source_id": "outro", "clip_info": info}
        )
    elif kind == "off_by_one":
        index = _placement_index(package, "cut-001-v")
        info = placements[index].clip_info.model_copy(update={"start_frame": 1})
        placements[index] = placements[index].model_copy(update={"clip_info": info})
    elif kind == "wrong_track":
        index = _placement_index(package, "cut-001-a")
        info = placements[index].clip_info.model_copy(update={"track_index": 2})
        placements[index] = placements[index].model_copy(update={"clip_info": info})
    elif kind == "wrong_link":
        members = {g.av_link_id: list(g.item_ids) for g in package.link_groups}
        members["av-cut-001"].remove("cut-001-v")
        members["av-cut-001"].append("cut-002-v")
        members["av-cut-002"].remove("cut-002-v")
        members["av-cut-002"].append("cut-001-v")
        return rehashed(
            package,
            link_groups=tuple(
                LinkGroup(av_link_id=link_id, item_ids=tuple(sorted(ids)))
                for link_id, ids in sorted(members.items())
            ),
        )
    elif kind == "missing_item":
        index = _placement_index(package, "outro-001-v")
        ghost = placements[index].model_copy(update={"item_id": "ghost-001-v"})
        ghost = ghost.model_copy(
            update={
                "clip_info": ghost.clip_info.model_copy(update={"record_frame": 108400})
            }
        )
        placements.append(ghost)
    elif kind == "extra_item":
        placements.pop(_placement_index(package, "cut-002-v"))
    else:  # pragma: no cover - the parametrize list is closed
        raise AssertionError(kind)
    return rehashed(package, placements=tuple(placements))


def complete_build(
    work: Path,
    package: ResolvePackage,
    *,
    prior_fingerprint: str | None = None,
    manager: FakeBuildManager | None = None,
) -> tuple[BuildOutput, FakeBuildManager]:
    owned = manager if manager is not None else FakeBuildManager(work / "render")
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=work / "ffmpeg",
        render_dir=work / "render",
        evidence_bundle=work / "bundle",
        seams=NoopSeams(),
        timing=RenderTiming(deadline_seconds=0.3, poll_seconds=0.01),
        prior_conformance_fingerprint=prior_fingerprint,
    )
    lease = MemoryLease({}, f"resolve-build:{package.artifact_id}", "todo-50")
    output = CleanBuilder(fake_connection(owned), wiring).build(package, lease)
    return output, owned


def kill_build_at_readback(
    work: Path, package: ResolvePackage, manager: FakeBuildManager
) -> str:
    """Leave the package's owned staging timeline behind, fully placed."""

    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=work / "ffmpeg",
        render_dir=work / "render",
        seams=killing_seams("after_readback"),
        timing=RenderTiming(deadline_seconds=0.3, poll_seconds=0.01),
    )
    lease = MemoryLease({}, f"resolve-build:{package.artifact_id}", "todo-50")
    with pytest.raises(BuildInterrupted):
        CleanBuilder(fake_connection(manager), wiring).build(package, lease)
    orphan = staging_project_name(package.content_hash)
    assert orphan in project_names(manager)
    return orphan


def _load_timeline(manager: ProjectManagerApi, name: str) -> BaseCutTimelineApi:
    project = manager.LoadProject(name)
    assert project is not None
    timeline = project.GetTimelineByIndex(1)
    assert timeline is not None
    assert project.SetCurrentTimeline(timeline)
    return cast("BaseCutTimelineApi", timeline)


def _capture(
    manager: ProjectManagerApi, name: str, sha256_of: Callable[[Path], str]
) -> TimelineReadback:
    return capture_readback(_load_timeline(manager, name), sha256_of)


def _row(**overrides: object) -> ReadbackRow:
    base: dict[str, object] = {
        "unique_id": "uid-1",
        "kind": "video",
        "track_index": 1,
        "record_start": 0,
        "record_end": 30,
        "source_start": 0,
        "source_duration": 30,
        "media_path": "/media/source.mov",
        "media_sha256": "a" * 64,
        "linked_ids": (),
    }
    return ReadbackRow.model_validate({**base, **overrides})


# ---------------------------------------------------------------- offline


def test_offline_totals_only_conformance_table_is_rejected(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    output, _ = complete_build(tmp_path, compile_build_package(media))
    payload = output.conformance.model_dump()
    payload.pop("items")  # a totals-only table carries aggregates and no item verdicts
    with pytest.raises(ValidationError):
        ConformanceTable.model_validate(payload)
    with pytest.raises(ValidationError):
        ConformanceTable.model_validate({**payload, "items": []})


def test_offline_malformed_readback_tables_are_rejected() -> None:
    with pytest.raises(ValidationError):  # empty record span
        _row(record_end=0)
    with pytest.raises(ValidationError):  # non-integer strict frame
        ReadbackRow.model_validate(
            {**_row().model_dump(), "record_start": 1.5, "record_end": 30}
        )
    with pytest.raises(ValidationError):  # duplicate unique ids
        TimelineReadback(timeline_name="t", rows=(_row(), _row()))
    assert _row(record_end=31).record_end == 31  # well-formed rows still pass


def test_offline_build_output_carries_zero_delta_conformance_table(
    tmp_path: Path,
) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    output, _ = complete_build(tmp_path, package)

    table = output.conformance
    assert isinstance(table, ConformanceTable)
    assert table.all_passed
    assert table.timeline_name.startswith("__fvp_test__tl__")
    assert len(table.items) == 8 == table.expected_items == table.observed_items
    assert table.missing_item_ids == ()
    assert table.extra_rows == ()
    assert table.total_record_delta_frames == 0
    for verdict in table.items:
        assert verdict.passed
        assert verdict.observed
        assert verdict.faults == ()
        assert verdict.media_match
        assert verdict.span_match
        assert verdict.track_match
        assert verdict.link_match
        assert verdict.record_start_delta_frames == 0
        assert verdict.record_end_delta_frames == 0
        assert verdict.source_start_delta_frames == 0
        assert verdict.source_end_delta_frames == 0
    assert table.observed_fingerprint == table.expected_fingerprint
    detector = DriftDetector()
    assert detector.check(table) is None
    assert detector.check(table, recorded_fingerprint=table.observed_fingerprint) is None
    recorded = (tmp_path / "bundle" / "build-output.json").read_bytes()
    assert BuildOutput.model_validate_json(recorded) == output
    assert [row.item_id for row in output.items] == [v.item_id for v in table.items]
    assert all(row.passed for row in output.items)


def test_offline_verdict_flags_are_recomputed_not_trusted(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    output, _ = complete_build(tmp_path, compile_build_package(media))
    lying = output.conformance.items[0].model_dump()
    lying["media_match"] = False
    lying["media_path_observed"] = "/media/other.mov"
    lying["media_sha256_observed"] = "b" * 64
    lying["passed"] = True  # a lying caller must not be able to force success
    lying["faults"] = ()
    with pytest.raises(ValidationError):
        ItemVerdict.model_validate(lying)
    honest = output.conformance.items[0].model_dump()
    honest.update(
        media_match=False,
        media_path_observed="/media/other.mov",
        media_sha256_observed="b" * 64,
        faults=("item_media_mismatch",),
        passed=False,
        detail="lying",
    )
    verdict = ItemVerdict.model_validate(honest)
    assert not verdict.passed
    assert verdict.faults == ("item_media_mismatch",)


def test_offline_conformance_tables_are_deterministic(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    first, _ = complete_build(tmp_path / "one", package)
    second, _ = complete_build(tmp_path / "two", package)
    without_staging_name = {"timeline_name": "-"}  # per-staging uuid differs by design
    first_stable = first.conformance.model_copy(update=without_staging_name)
    second_stable = second.conformance.model_copy(update=without_staging_name)
    assert first_stable == second_stable
    assert first.conformance.observed_fingerprint == second.conformance.observed_fingerprint


@pytest.mark.parametrize("kind", FAULT_KINDS)
def test_offline_drift_fault_shapes_block_without_overwrite(
    tmp_path: Path, kind: str
) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    manager = FakeBuildManager(tmp_path / "render")
    orphan = kill_build_at_readback(tmp_path, package, manager)
    sha256_of = FakeBuildTools().sha256
    before = _capture(manager, orphan, sha256_of)

    faulted = _fault_package(package, kind)
    table = ConformanceChecker().verify(faulted, before)
    assert not table.all_passed
    report = DriftDetector().check(table, recorded_fingerprint=None)
    assert report is not None
    assert report.kind == kind
    assert report.routes == DRIFT_ROUTES
    assert report.expected_fingerprint != report.observed_fingerprint
    assert report.item_faults, f"{kind}: the report must name the faulted items"

    wiring = BuilderWiring(
        registry=registry_for(faulted),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
    )
    with pytest.raises(BuildFailure) as raised:
        StagingDriftGuard().enforce(manager, faulted, wiring, project_name=orphan)
    assert raised.value.code == "drift-blocked"
    assert DriftReport.model_validate_json(raised.value.detail).kind == kind
    assert orphan in project_names(manager)  # blocked means never overwritten
    assert _capture(manager, orphan, sha256_of) == before


def test_offline_guard_blocks_real_timeline_edits(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    manager = FakeBuildManager(tmp_path / "render")
    orphan = kill_build_at_readback(tmp_path, package, manager)
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
    )

    timeline = _load_timeline(manager, orphan)
    video = timeline.GetItemListInTrack("video", 1)[0]
    audio = timeline.GetItemListInTrack("audio", 1)[0]
    assert timeline.SetClipsLinked([video, audio], False)  # noqa: FBT003 (Resolve API flag)

    with pytest.raises(BuildFailure) as raised:
        StagingDriftGuard().enforce(manager, package, wiring)
    assert raised.value.code == "drift-blocked"
    assert DriftReport.model_validate_json(raised.value.detail).kind == "wrong_link"
    assert orphan in project_names(manager)

    recorded = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
        prior_conformance_fingerprint=expected_fingerprint(package),
    )
    with pytest.raises(BuildFailure) as human:
        StagingDriftGuard().enforce(manager, package, recorded)
    assert DriftReport.model_validate_json(human.value.detail).kind == "human_mutation"


def test_offline_conforming_orphan_passes_gate_and_rebuilds_fresh(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    manager = FakeBuildManager(tmp_path / "render")
    orphan = kill_build_at_readback(tmp_path, package, manager)
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
    )
    StagingDriftGuard().enforce(manager, package, wiring)  # untouched: passes
    output, manager = complete_build(tmp_path / "retry", package, manager=manager)
    assert output.conformance.all_passed
    assert set(output.swept_projects) >= {orphan}
    assert orphan not in project_names(manager)


def test_offline_human_mutation_blocks_full_builder_without_overwrite(
    tmp_path: Path,
) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_build_package(media)
    manager = FakeBuildManager(tmp_path / "render")
    orphan = kill_build_at_readback(tmp_path, package, manager)

    timeline = _load_timeline(manager, orphan)
    video = timeline.GetItemListInTrack("video", 1)[0]
    audio = timeline.GetItemListInTrack("audio", 1)[0]
    assert timeline.SetClipsLinked([video, audio], False)  # noqa: FBT003 (Resolve API flag)

    with pytest.raises(BuildFailure) as raised:
        complete_build(
            tmp_path / "retry",
            package,
            prior_fingerprint=expected_fingerprint(package),
            manager=manager,
        )
    assert raised.value.code == "drift-blocked"
    report = DriftReport.model_validate_json(raised.value.detail)
    assert report.kind == "human_mutation"
    assert report.routes == DRIFT_ROUTES
    assert orphan in project_names(manager)  # the drifted timeline was not touched


# ---------------------------------------------------------------- live


@dataclass(frozen=True, slots=True)
class LiveEnv:
    connection: ResolveConnection
    package: ResolvePackage
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
            if name.startswith("__fvp_test__build_"):
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
    bundle = Path(str(evidence)) / "conformance" if evidence else media_dir.parent / "conformance"
    bundle.mkdir(parents=True, exist_ok=True)
    return LiveEnv(
        connection=live_connection,
        package=compile_build_package(media, artifact_id="resolve-package-todo50-conformance"),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        bundle=bundle,
        lease_db=bundle / "lease.sqlite3",
    )


def _live_wiring(
    env: LiveEnv, *, prior_fingerprint: str | None = None, seams: object = None
) -> BuilderWiring:
    return BuilderWiring(
        registry=registry_for(env.package),
        tools=PinnedBuildTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe),
        ffmpeg_bin=env.ffmpeg,
        render_dir=env.bundle,
        evidence_bundle=env.bundle,
        seams=cast("NoopSeams", seams if seams is not None else NoopSeams()),
        prior_conformance_fingerprint=prior_fingerprint,
    )


def _live_sha(env: LiveEnv) -> Callable[[Path], str]:
    return PinnedBuildTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe).sha256


@pytest.mark.resolve_live
def test_live_build_reports_zero_delta_conformance_and_fingerprints(live_env: LiveEnv) -> None:
    package = live_env.package
    lease = SqliteLease(live_env.lease_db, f"resolve-build:{package.artifact_id}", "todo-50")
    output = CleanBuilder(live_env.connection, _live_wiring(live_env)).build(package, lease)

    table = output.conformance
    assert table.all_passed
    assert len(table.items) == 8 == table.expected_items == table.observed_items
    assert table.missing_item_ids == ()
    assert table.extra_rows == ()
    assert table.total_record_delta_frames == 0
    for verdict in table.items:
        assert verdict.passed
        assert verdict.faults == ()
        assert verdict.record_start_delta_frames == 0
        assert verdict.record_end_delta_frames == 0
        assert verdict.source_start_delta_frames == 0
        assert verdict.source_end_delta_frames == 0
    assert table.observed_fingerprint == table.expected_fingerprint
    print(f"\nlive conformance: expected={table.expected_fingerprint}")
    print(f"live conformance: observed={table.observed_fingerprint}")
    detector = DriftDetector()
    assert detector.check(table) is None  # untouched own build: no drift
    assert detector.check(table, recorded_fingerprint=table.observed_fingerprint) is None
    (live_env.bundle / "conformance-table.json").write_bytes(canonical_model_bytes(table))
    manager = live_env.connection.project_manager()
    assert not [n for n in project_names(manager) if n.startswith("__fvp_test__build_")]


@pytest.mark.resolve_live
def test_live_drift_fault_shapes_block_without_overwrite(live_env: LiveEnv) -> None:
    package = live_env.package
    manager = live_env.connection.project_manager()
    lease = SqliteLease(live_env.lease_db, f"resolve-build:{package.artifact_id}-drift", "todo-50")
    orphan = staging_project_name(package.content_hash)
    sha256_of = _live_sha(live_env)
    reports: dict[str, object] = {}
    try:
        with pytest.raises(BuildInterrupted):
            CleanBuilder(
                live_env.connection,
                _live_wiring(live_env, seams=killing_seams("after_readback")),
            ).build(package, lease)
        assert orphan in project_names(manager)

        before = _capture(manager, orphan, sha256_of)
        happy = ConformanceChecker().verify(package, before)
        assert happy.all_passed, "an untouched own build must conform exactly"
        assert DriftDetector().check(happy) is None

        for kind in FAULT_KINDS:
            faulted = _fault_package(package, kind)
            table = ConformanceChecker().verify(faulted, before)
            report = DriftDetector().check(table, recorded_fingerprint=None)
            assert report is not None
            assert report.kind == kind, f"{kind}: {report.item_faults}"
            assert report.routes == DRIFT_ROUTES
            reports[kind] = json.loads(report.model_dump_json())
            with pytest.raises(BuildFailure) as raised:
                StagingDriftGuard().enforce(
                    manager, faulted, _live_wiring(live_env), project_name=orphan
                )
            assert raised.value.code == "drift-blocked"
            assert DriftReport.model_validate_json(raised.value.detail).kind == kind
            assert orphan in project_names(manager)

        assert _capture(manager, orphan, sha256_of) == before  # never overwritten
        (live_env.bundle / "drift-reports.json").write_text(
            json.dumps(reports, indent=1, sort_keys=True)
        )
    finally:
        delete_staging(manager, orphan)


@pytest.mark.resolve_live
def test_live_human_mutation_blocks_rebuild_without_overwrite(live_env: LiveEnv) -> None:
    package = live_env.package
    manager = live_env.connection.project_manager()
    lease = SqliteLease(live_env.lease_db, f"resolve-build:{package.artifact_id}-human", "todo-50")
    orphan = staging_project_name(package.content_hash)
    sha256_of = _live_sha(live_env)
    try:
        with pytest.raises(BuildInterrupted):
            CleanBuilder(
                live_env.connection,
                _live_wiring(live_env, seams=killing_seams("after_readback")),
            ).build(package, lease)
        assert orphan in project_names(manager)

        recorded = ConformanceChecker().verify(package, _capture(manager, orphan, sha256_of))
        assert recorded.all_passed
        recorded_fingerprint = recorded.observed_fingerprint  # the system's own last write

        timeline = _load_timeline(manager, orphan)  # a REAL human edit via the bridge
        video = timeline.GetItemListInTrack("video", 1)[0]
        audio = timeline.GetItemListInTrack("audio", 1)[0]
        assert timeline.SetClipsLinked([video, audio], False)  # noqa: FBT003 (Resolve API flag)
        mutated_fingerprint = ConformanceChecker().verify(
            package, _capture(manager, orphan, sha256_of)
        ).observed_fingerprint
        assert mutated_fingerprint != recorded_fingerprint

        with pytest.raises(BuildFailure) as raised:
            CleanBuilder(
                live_env.connection, _live_wiring(live_env, prior_fingerprint=recorded_fingerprint)
            ).build(package, lease)
        assert raised.value.code == "drift-blocked"
        report = DriftReport.model_validate_json(raised.value.detail)
        assert report.kind == "human_mutation"
        assert report.routes == DRIFT_ROUTES
        assert report.recorded_fingerprint == recorded_fingerprint
        assert report.observed_fingerprint == mutated_fingerprint

        assert orphan in project_names(manager)  # blocked: the timeline was not touched
        after = ConformanceChecker().verify(
            package, _capture(manager, orphan, sha256_of)
        ).observed_fingerprint
        assert after == mutated_fingerprint
        (live_env.bundle / "human-mutation-report.json").write_text(report.model_dump_json())
    finally:
        delete_staging(manager, orphan)
