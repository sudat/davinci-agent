"""Todo-49 acceptance: the Phase-2 FIXED presentation through Package/Builder.

Offline (``-m 'not resolve_live'``): the presentation section routes
dialogue/ambient onto role-separated audio tracks, every asset carries
{path, sha256, license_ref} provenance (missing or drifting provenance is a
typed unapproved-asset refusal), and the scope guard rejects Phase-3 config —
style profiles/swaps, BGM/SE, Fusion/Fairlight keys, camera and channel
branches — with typed scope failures, all on fakes.

Live (``-m resolve_live``): a real package compiled with the presentation
baseline builds fresh in Resolve: dialogue audio reads back on track 1 and
ambient on track 2, the render decodes with the exact aac/48 kHz preset, the
subtitle demux round-trip proves the fixed cue text/timing from the rendered
bytes, intro/outro identity/duration/link read back, and the package lineage
preserves asset/license/analysis hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from services.analyze.analysis_models import DialogueAmbientSummary
from services.build.builder_models import BuilderWiring, BuildOutput, NoopSeams
from services.build.builder_render import (
    PinnedBuildTools,
    render_srt_bytes,
)
from services.build.builder_session import BUILD_PROJECT_PREFIX, delete_staging
from services.build.clean_builder import CleanBuilder
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineIrProduction,
    TimelineTrackProduction,
)
from services.foundation_io import canonical_model_bytes, sha256_file
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_adapter.models import RoleTrackMapEntry
from services.resolve_adapter.package import (
    PackageCompileRequest,
    compile_resolve_package,
)
from services.resolve_adapter.presentation_baseline import (
    PresentationBuildRequest,
    build_presentation_section,
)
from services.resolve_adapter.presentation_models import (
    AssetProvenance,
    AudioRoleEntry,
    FixedAudioPolicy,
    FixedRenderPreset,
    FixedSubtitleStyle,
    IntroOutroAsset,
    PresentationSection,
)
from services.resolve_adapter.presentation_scope import parse_presentation_section
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection
from services.resolve_bridge.fixed_presentation_tools import MediaTools
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.lifecycle import project_names
from services.resolve_bridge.readiness import load_host_report
from tests.build.fakes import FakeBuildManager, FakeBuildTools
from tests.build.support import (
    LOCK_PATH,
    MemoryLease,
    SqliteLease,
    compile_build_package,
    fake_connection,
    manifest_0a,
    media_bindings,
    phase2_lock,
    production_ir,
    registry_for,
    write_offline_media,
)
from tests.resolve_locale import native_bridge_locale

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage

FIXED_STYLE: Final = FixedSubtitleStyle(
    style_ref="style-fixed-0a", font_family="Helvetica Neue"
)
FIXED_PRESET: Final = FixedRenderPreset(
    preset_id="phase-0a-h264-aac-v1",
    audio_codec="aac",
    audio_sample_rate=48000,
    audio_channels=2,
)
LICENSE_INTRO: Final = "license-intro-fixture-v1"
LICENSE_OUTRO: Final = "license-outro-fixture-v1"
ANALYSIS_DIALOGUE: Final = DialogueAmbientSummary(
    dialogue_rms_mb=-1800,
    ambient_noise_floor_mb=-7200,
    dialogue_sample_count=960000,
    ambient_sample_count=0,
)
ANALYSIS_SILENT: Final = DialogueAmbientSummary(
    dialogue_rms_mb=-9000,
    ambient_noise_floor_mb=-9000,
    dialogue_sample_count=0,
    ambient_sample_count=48000,
)
INTRO_OUTRO_SOURCES: Final = frozenset({"intro", "outro"})


def _analysis_hash(summary: DialogueAmbientSummary) -> str:
    return hashlib.sha256(canonical_model_bytes(summary)).hexdigest()


def fixed_presentation(ir: TimelineIrProduction, paths: dict[str, Path]) -> PresentationSection:
    return build_presentation_section(
        PresentationBuildRequest(
            ir=ir,
            subtitle=FIXED_STYLE,
            audio=FixedAudioPolicy(
                strategy="dialogue-ambient-role-tracks-v1",
                roles=(
                    AudioRoleEntry(
                        source_id="source",
                        role="dialogue",
                        analysis_sha256=_analysis_hash(ANALYSIS_DIALOGUE),
                    ),
                    AudioRoleEntry(
                        source_id="intro",
                        role="ambient",
                        analysis_sha256=_analysis_hash(ANALYSIS_SILENT),
                    ),
                    AudioRoleEntry(
                        source_id="outro",
                        role="ambient",
                        analysis_sha256=_analysis_hash(ANALYSIS_SILENT),
                    ),
                ),
            ),
            render_preset=FIXED_PRESET,
            intro_outro=(
                IntroOutroAsset(
                    role="intro",
                    source_id="intro",
                    duration_frames=30,
                    asset=AssetProvenance(
                        path=str(paths["intro"]),
                        sha256=sha256_file(paths["intro"]),
                        license_ref=LICENSE_INTRO,
                    ),
                ),
                IntroOutroAsset(
                    role="outro",
                    source_id="outro",
                    duration_frames=30,
                    asset=AssetProvenance(
                        path=str(paths["outro"]),
                        sha256=sha256_file(paths["outro"]),
                        license_ref=LICENSE_OUTRO,
                    ),
                ),
            ),
        )
    )


def compile_presentation_package(
    paths: dict[str, Path], artifact_id: str = "resolve-package-p0a-presentation"
) -> ResolvePackage:
    ir = production_ir(manifest_0a())
    return compile_resolve_package(
        PackageCompileRequest(
            ir=ir,
            lock=phase2_lock(),
            lock_sha256=sha256_file(LOCK_PATH),
            declared_media=media_bindings(paths),
            artifact_id=artifact_id,
            intro_outro_source_ids=INTRO_OUTRO_SOURCES,
            presentation=fixed_presentation(ir, paths),
        )
    )


def ir_with_cue(
    ir: TimelineIrProduction, **cue_updates: object
) -> TimelineIrProduction:
    tracks: list[TimelineTrackProduction] = []
    for track in ir.tracks:
        if track.track.kind != "subtitle":
            tracks.append(track)
            continue
        items = [
            item.model_copy(update=cue_updates)
            if isinstance(item, SubtitleCueItem)
            else item
            for item in track.items
        ]
        tracks.append(TimelineTrackProduction(track=track.track, items=tuple(items)))
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(ir.rate))
    for track in tracks:
        digest.update(canonical_model_bytes(track))
    return ir.model_copy(
        update={"tracks": tuple(tracks), "content_hash": digest.hexdigest()}
    )


def run_fake_build(tmp_path: Path, package: ResolvePackage) -> BuildOutput:
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=FakeBuildTools(),
        ffmpeg_bin=tmp_path / "ffmpeg",
        render_dir=tmp_path / "render",
        evidence_bundle=tmp_path / "bundle",
        seams=NoopSeams(),
    )
    lease = MemoryLease({}, f"resolve-build:{package.artifact_id}", "todo-49")
    return CleanBuilder(fake_connection(FakeBuildManager(tmp_path / "render")), wiring).build(
        package, lease
    )


# ---------------------------------------------------------------- offline


def test_offline_presentation_routes_roles_and_renders(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    package = compile_presentation_package(media)

    audio_rows = {
        p.item_id: p.clip_info
        for p in package.placements
        if p.clip_info.track_type == "audio"
    }
    assert audio_rows["cut-001-a"].track_index == 1  # dialogue on the dialogue track
    assert audio_rows["cut-002-a"].track_index == 1
    assert audio_rows["intro-001-a"].track_index == 2  # ambient never conflated
    assert audio_rows["outro-001-a"].track_index == 2
    role_entries = [e for e in package.track_map if isinstance(e, RoleTrackMapEntry)]
    assert role_entries == [
        RoleTrackMapEntry(role="dialogue", resolve_track_index=1),
        RoleTrackMapEntry(role="ambient", resolve_track_index=2),
    ]
    assert package.render_job.audio_codec == "aac"
    assert package.render_job.audio_sample_rate == 48000

    assert package.presentation is not None
    lineage = {a.source_id: a for a in package.presentation.intro_outro}
    assert lineage["intro"].asset.license_ref == LICENSE_INTRO
    assert lineage["outro"].asset.license_ref == LICENSE_OUTRO
    assert lineage["intro"].asset.sha256 == sha256_file(media["intro"])
    assert lineage["outro"].asset.sha256 == sha256_file(media["outro"])
    roles = {r.source_id: r for r in package.presentation.audio.roles}
    assert roles["source"].analysis_sha256 == _analysis_hash(ANALYSIS_DIALOGUE)
    assert roles["intro"].role == "ambient"

    assert compile_presentation_package(media).content_hash == package.content_hash

    output = run_fake_build(tmp_path, package)
    rows = {row.item_id: row for row in output.items}
    assert all(row.passed for row in output.items), [r.detail for r in output.items]
    assert rows["cut-001-a"].track_index == 1
    assert rows["outro-001-a"].track_index == 2
    assert output.subtitle is not None
    assert output.subtitle.codec == "mov_text"
    assert Path(output.subtitle.output_path).is_file()


def test_offline_style_swap_is_typed_scope_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    ir = ir_with_cue(production_ir(manifest_0a()), style_ref="style-brand-a")
    with pytest.raises(PackageCompileError) as raised:
        build_presentation_section(
            PresentationBuildRequest(
                ir=ir,
                subtitle=FIXED_STYLE,
                audio=_roles_policy(),
                render_preset=FIXED_PRESET,
                intro_outro=_intro_outro_assets(media),
            )
        )
    assert raised.value.code == "phase3-scope-rejected"


def _roles_policy() -> FixedAudioPolicy:
    return FixedAudioPolicy(
        strategy="dialogue-ambient-role-tracks-v1",
        roles=(
            AudioRoleEntry(
                source_id="source",
                role="dialogue",
                analysis_sha256=_analysis_hash(ANALYSIS_DIALOGUE),
            ),
            AudioRoleEntry(
                source_id="intro", role="ambient", analysis_sha256=_analysis_hash(ANALYSIS_SILENT)
            ),
            AudioRoleEntry(
                source_id="outro", role="ambient", analysis_sha256=_analysis_hash(ANALYSIS_SILENT)
            ),
        ),
    )


def _intro_outro_assets(paths: dict[str, Path]) -> tuple[IntroOutroAsset, ...]:
    return fixed_presentation(production_ir(manifest_0a()), paths).intro_outro


@pytest.mark.parametrize(
    "phase3_key",
    [
        "style_profile",
        "bgm",
        "bgm_se",
        "se",
        "sound_effect",
        "fusion",
        "fusion_template",
        "fairlight",
        "fairlight_preset",
        "camera_preset",
        "camera",
    ],
)
def test_offline_phase3_keys_are_typed_scope_failures(
    tmp_path: Path, phase3_key: str
) -> None:
    media = write_offline_media(tmp_path / "media")
    payload = fixed_presentation(production_ir(manifest_0a()), media).model_dump(mode="json")
    payload[phase3_key] = {"profile": "brand-a"}
    with pytest.raises(PackageCompileError) as raised:
        parse_presentation_section(payload)
    assert raised.value.code == "phase3-scope-rejected"


def test_offline_channel_branch_is_typed_scope_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    payload = fixed_presentation(production_ir(manifest_0a()), media).model_dump(mode="json")
    payload["audio"]["channel_profile"] = "channel-loudness-v2"
    with pytest.raises(PackageCompileError) as raised:
        parse_presentation_section(payload)
    assert raised.value.code == "phase3-scope-rejected"


def test_offline_unapproved_asset_is_typed_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    payload = fixed_presentation(production_ir(manifest_0a()), media).model_dump(mode="json")
    del payload["intro_outro"][1]["asset"]["license_ref"]
    with pytest.raises(PackageCompileError) as raised:
        parse_presentation_section(payload)
    assert raised.value.code == "unapproved-asset"

    ir = production_ir(manifest_0a())
    stale_presentation = fixed_presentation(ir, media)
    media["intro"].write_bytes(b"drifted-bytes")  # stale provenance vs declared media
    with pytest.raises(PackageCompileError) as drifted:
        compile_resolve_package(
            PackageCompileRequest(
                ir=ir,
                lock=phase2_lock(),
                lock_sha256=sha256_file(LOCK_PATH),
                declared_media=media_bindings(media),
                artifact_id="resolve-package-p0a-stale-asset",
                intro_outro_source_ids=INTRO_OUTRO_SOURCES,
                presentation=stale_presentation,
            )
        )
    assert drifted.value.code == "unapproved-asset"


def test_offline_role_conflation_is_typed_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    payload = fixed_presentation(production_ir(manifest_0a()), media).model_dump(mode="json")
    payload["audio"]["dialogue_resolve_track"] = 1
    payload["audio"]["ambient_resolve_track"] = 1
    with pytest.raises(PackageCompileError) as raised:
        parse_presentation_section(payload)
    assert raised.value.code == "audio-role-conflation"


def test_offline_missing_role_is_typed_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    payload = fixed_presentation(production_ir(manifest_0a()), media).model_dump(mode="json")
    payload["audio"]["roles"] = [
        row for row in payload["audio"]["roles"] if row["source_id"] != "intro"
    ]
    sectionless_audio = FixedAudioPolicy.model_validate_json(json.dumps(payload["audio"]))
    with pytest.raises(PackageCompileError) as raised:
        build_presentation_section(
            PresentationBuildRequest(
                ir=production_ir(manifest_0a()),
                subtitle=FIXED_STYLE,
                audio=sectionless_audio,
                render_preset=FIXED_PRESET,
                intro_outro=_intro_outro_assets(media),
            )
        )
    assert raised.value.code == "audio-role-missing"


def test_offline_unsafe_cue_is_typed_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    ir = ir_with_cue(production_ir(manifest_0a()), safe_area=False)
    with pytest.raises(PackageCompileError) as raised:
        fixed_presentation(ir, media)
    assert raised.value.code == "subtitle-unsafe-area"


def test_offline_render_preset_mismatch_is_typed_failure(tmp_path: Path) -> None:
    media = write_offline_media(tmp_path / "media")
    ir = production_ir(manifest_0a())
    mismatched = FixedRenderPreset(
        preset_id="phase-0a-h264-aac-v1",
        audio_codec="aac",
        audio_sample_rate=44100,
        audio_channels=2,
    )
    section = build_presentation_section(
        PresentationBuildRequest(
            ir=ir,
            subtitle=FIXED_STYLE,
            audio=_roles_policy(),
            render_preset=mismatched,
            intro_outro=_intro_outro_assets(media),
        )
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(
            PackageCompileRequest(
                ir=ir,
                lock=phase2_lock(),
                lock_sha256=sha256_file(LOCK_PATH),
                declared_media=media_bindings(media),
                artifact_id="resolve-package-p0a-mismatch",
                intro_outro_source_ids=INTRO_OUTRO_SOURCES,
                presentation=section,
            )
        )
    assert raised.value.code == "render-preset-mismatch"


def test_offline_golden_path_compiles_unchanged_without_presentation(
    tmp_path: Path,
) -> None:
    media = write_offline_media(tmp_path / "media")
    plain = compile_build_package(media)
    assert plain.presentation is None
    assert all(
        p.clip_info.track_index == 1 for p in plain.placements
    )
    assert not [e for e in plain.track_map if isinstance(e, RoleTrackMapEntry)]


# ---------------------------------------------------------------- live


@dataclass(frozen=True, slots=True)
class LiveEnv:
    connection: ResolveConnection
    package: ResolvePackage
    ffmpeg: Path
    ffprobe: Path
    bundle: Path
    lease_db: Path
    media: dict[str, Path]


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


@pytest.fixture(scope="module")
def live_env(request: pytest.FixtureRequest) -> Iterator[LiveEnv]:
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
    media_dir = Path(
        os.environ.get("FVP_BUILD_MEDIA_DIR", str(report_path.parent / "phase-0a" / "fixture"))
    )
    media = {sid: media_dir / f"{sid}.mov" for sid in ("source", "intro", "outro")}
    missing = [str(path) for path in media.values() if not path.is_file()]
    if missing:
        pytest.fail(f"live fixture media missing: {missing}")
    ffmpeg = report_path.parent / "bootstrap/ffmpeg-7.1.1/bin/ffmpeg"
    ffprobe = report_path.parent / "bootstrap/ffmpeg-7.1.1/bin/ffprobe"
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        pytest.fail(f"pinned ffmpeg/ffprobe missing: {ffmpeg} / {ffprobe}")
    evidence = request.config.getoption("--resolve-evidence")
    bundle = Path(str(evidence)) / "build-presentation" if evidence else media_dir / "build"
    bundle.mkdir(parents=True, exist_ok=True)
    yield LiveEnv(
        connection=connection,
        package=compile_presentation_package(media),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        bundle=bundle,
        lease_db=bundle / "lease.sqlite3",
        media=media,
    )
    try:
        manager = connection.project_manager()
        for name in project_names(manager):
            if name.startswith(BUILD_PROJECT_PREFIX):
                delete_staging(manager, name)
    except (BridgeConnectionError, TypeError):
        return


def _assert_readback(output: BuildOutput, media: dict[str, Path]) -> None:
    assert all(row.passed for row in output.items), [row.detail for row in output.items]
    rows = {row.item_id: row for row in output.items}
    assert len(rows) == 8
    assert rows["cut-001-a"].track_index == 1  # dialogue on the dialogue track
    assert rows["cut-002-a"].track_index == 1
    assert rows["intro-001-a"].track_index == 2  # ambient on its own track
    assert rows["outro-001-a"].track_index == 2

    intro_row = rows["intro-001-v"]
    outro_row = rows["outro-001-v"]
    assert intro_row.record_start == 108000
    assert intro_row.record_end == 108030
    assert outro_row.record_start == 108630
    assert outro_row.record_end == 108660
    assert (intro_row.source_start, intro_row.source_end) == (0, 30)
    assert intro_row.media_path == str(media["intro"].resolve())
    assert outro_row.media_path == str(media["outro"].resolve())
    assert intro_row.linked_ids
    assert outro_row.linked_ids  # A/V link conformance


def _assert_render_preset(output: BuildOutput) -> None:
    assert output.render.completion_percentage == 100
    probe = output.render.probe
    assert (probe.audio_codec, probe.audio_sample_rate, probe.audio_channels) == (
        "aac", 48000, 2
    )
    assert (probe.video_codec, probe.width, probe.height) == ("h264", 1920, 1080)
    assert probe.r_frame_rate == "30/1"
    assert probe.nb_frames == "660"
    assert probe.duration is not None
    assert Fraction(probe.duration) >= Fraction(22)


def _assert_subtitle_round_trip(
    package: ResolvePackage, output: BuildOutput, env: LiveEnv
) -> None:
    assert output.subtitle is not None
    subtitled = Path(output.subtitle.output_path)
    assert subtitled.is_file()
    tools = MediaTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe)
    assert package.subtitle_step is not None
    expected_srt = render_srt_bytes(package.subtitle_step.cues)

    def normalize(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n").rstrip(b"\n")

    assert normalize(tools.demux_subtitle(subtitled)) == normalize(expected_srt)
    packets = tools.subtitle_packets(subtitled)
    assert any(
        abs(packet.pts - 6.0) < 1e-3 and abs(packet.duration - 2.0) < 1e-3
        for packet in packets
    ), packets


def _assert_lineage(package: ResolvePackage, media: dict[str, Path]) -> None:
    assert package.presentation is not None
    lineage = {a.source_id: a for a in package.presentation.intro_outro}
    assert lineage["intro"].asset.sha256 == sha256_file(media["intro"])
    assert lineage["outro"].asset.sha256 == sha256_file(media["outro"])
    assert lineage["intro"].asset.license_ref == LICENSE_INTRO
    assert lineage["outro"].asset.license_ref == LICENSE_OUTRO


@pytest.mark.resolve_live
def test_live_fixed_presentation_renders(live_env: LiveEnv) -> None:
    package = live_env.package
    resource = f"resolve-build:{package.artifact_id}"
    lease = SqliteLease(live_env.lease_db, resource, "todo-49-presentation")
    wiring = BuilderWiring(
        registry=registry_for(package),
        tools=PinnedBuildTools(ffmpeg_bin=live_env.ffmpeg, ffprobe_bin=live_env.ffprobe),
        ffmpeg_bin=live_env.ffmpeg,
        render_dir=live_env.bundle,
        evidence_bundle=live_env.bundle,
        seams=NoopSeams(),
    )
    output = CleanBuilder(live_env.connection, wiring).build(package, lease)

    _assert_readback(output, live_env.media)
    _assert_render_preset(output)
    _assert_subtitle_round_trip(package, output, live_env)
    _assert_lineage(package, live_env.media)

    manager = live_env.connection.project_manager()
    assert not [n for n in project_names(manager) if n.startswith(BUILD_PROJECT_PREFIX)]
    with sqlite3.connect(live_env.lease_db) as db:
        assert db.execute(
            "select count(*) from leases where resource = ?", (resource,)
        ).fetchone()[0] == 0
    print(f"\nlive render: {output.render.output_path} sha256={output.render.output_sha256}")
    assert output.subtitle is not None
    print(f"live subtitle: {output.subtitle.output_path} sha256={output.subtitle.output_sha256}")
    print(f"live fingerprint: {output.timeline_fingerprint}")


@pytest.mark.resolve_live
def test_live_presentation_scope_guard_has_no_phase3_dependency(
    live_env: LiveEnv,
) -> None:
    payload = live_env.package.presentation
    assert payload is not None
    document = payload.model_dump(mode="json")
    document["style_profile"] = {"ref": "style-brand-a"}
    with pytest.raises(PackageCompileError) as raised:
        parse_presentation_section(document)
    assert raised.value.code == "phase3-scope-rejected"
