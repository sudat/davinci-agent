"""Todo-59 acceptance: profile-driven BGM/SE and deterministic audio fallbacks.

Offline: the audio profile section compiles from the resolved profile + the
rights-gated registry (expired / unapproved / wrong-use music is a typed
failure), keeps the four logical roles on distinct tracks (dialogue
processing on the ambient track is typed), renders the shared EXTERNAL
MIXED DERIVATIVE deterministically (byte-identical double renders, A/B
asset-hash drift per the frozen golden dimensions, iterative integer-mB
normalization toward the declared targets), enforces preview/final parity
on the SAME derivative hash, maps targets onto the Todo-52 audio checks
(peak / loudness / channel violations are typed QC failures), and the
ladder chooser refuses unverified presets and unavailable mix plugins.
Live: the Fairlight preset probe records the honest apply+readback verdict,
and the end-to-end external-mix build places the derivative, renders, and
verifies loudness/peak/channel compliance from the RENDERED output bytes.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from services.analyze.analysis_models import LoudnessSummary
from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.foundation_io import canonical_model_bytes
from services.presentation.asset_registry import (
    AssetEntry,
    RegistrySnapshot,
    register_assets,
    registry_from_phase3_manifests,
)
from services.presentation.audio_live_media import LiveAudioPolicy
from services.presentation.audio_mix import (
    AudioMixError,
    MixedDerivative,
    render_mixed_derivative,
    require_mix_filters,
    verify_derivative_parity,
)
from services.presentation.audio_models import (
    AudioAnchor,
    AudioDerivativeRef,
    AudioProfileSection,
    AudioSection,
    LogicalAudioTrack,
    ProcessingAssignment,
)
from services.presentation.audio_profile import (
    DEFAULT_BGM_GAIN_MB,
    DEFAULT_FADE_FRAMES,
    DEFAULT_SE_GAIN_MB,
    PRESET_FINDING,
    AudioProfileError,
    PresetSupport,
    audio_preset_support_from_matrix,
    audio_thresholds_from_targets,
    choose_audio_rung,
    compile_audio_profile,
    verify_role_separation,
)
from services.presentation.models import (
    EpisodePresentationProfile,
    ResolvedPresentationProfile,
)
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.snapshot import freeze_job_presentation
from services.preview.tools import load_pinned_tools
from services.qc.checks.audio_checks import (
    AudioMeasure,
    evaluate_audio_measurements,
)
from services.resolve_adapter.audio_section import (
    audio_media_bindings,
    audio_placements,
    require_audio_role_separation,
)
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_adapter.models import MediaBinding
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.resolve_bridge.audio_preset_probe import (
    derive_preset_findings,
    preset_capability_row,
)
from services.resolve_bridge.audio_preset_probe_models import (
    AudioPresetProbeReport,
    MethodProbe,
    SettingProbe,
)
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import PROJECT_PREFIX
from services.resolve_bridge.readiness import load_host_report
from services.resolve_bridge.title_probe import append_matrix_findings
from services.spike.gate_models import (
    ApiFindingEntry,
    EvidenceRef,
)
from tests.presentation.test_manifest import _channel, _system
from tests.resolve_adapter.support import (
    declared_media,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
GOLDEN_PATH = Path("tests/goldens/reference/phase-3/expected.json")
ASSETS_ROOT = Path()
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"
RATE = RationalFrameRate(num=30, den=1)
TOTAL_FRAMES = 180
SEAL_FIELDS = frozenset({"section_sha256", "profile_snapshot_sha256"})
SHA = "a" * 64
PROBE_SHA = "b" * 64


# ------------------------------------------------------------------ helpers --


def _registry() -> RegistrySnapshot:
    return registry_from_phase3_manifests(MANIFEST_DIR)


@pytest.fixture(scope="module")
def registry() -> RegistrySnapshot:
    return _registry()


@pytest.fixture(scope="module")
def catalog(registry: RegistrySnapshot) -> tuple[str, ...]:
    return tuple(sorted(entry.asset_id for entry in registry.entries))


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-a.json").read_bytes()
    )


@pytest.fixture(scope="module")
def brand_b() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-b.json").read_bytes()
    )


@pytest.fixture(scope="module")
def profile_a(
    brand_a: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )


@pytest.fixture(scope="module")
def profile_b(
    brand_a: Phase3FixtureManifest,
    brand_b: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )


def _section(
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    *,
    compile_registry: RegistrySnapshot | None = None,
    total_frames: int = TOTAL_FRAMES,
    frame_rate: RationalFrameRate = RATE,
) -> AudioProfileSection:
    snapshot = freeze_job_presentation(
        profile,
        registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=ASSETS_ROOT,
    )
    return compile_audio_profile(
        profile,
        compile_registry if compile_registry is not None else registry,
        job_snapshot=snapshot,
        assets_root=ASSETS_ROOT,
        total_frames=total_frames,
        frame_rate=frame_rate,
    )


@pytest.fixture(scope="module")
def section_a(
    profile_a: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> AudioProfileSection:
    return _section(profile_a, registry)


@pytest.fixture(scope="module")
def section_b(
    profile_b: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> AudioProfileSection:
    return _section(profile_b, registry)


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    if isinstance(node, dict):
        for key in sorted(node):
            leaves.update(_flatten(node[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            leaves.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        leaves[prefix] = node
    return leaves


# ------------------------------------------------------------- compile A/B ----


def test_compile_section_carries_assets_anchors_gains_fades(
    section_a: AudioProfileSection, brand_a: Phase3FixtureManifest
) -> None:
    golden = json.loads(GOLDEN_PATH.read_bytes())
    expected = cast(
        "dict[str, str]", golden["fixtures"]["p3-brand-a"]["assets"]
    )
    assert section_a.assets["tone"].sha256 == expected["tone"]
    assert section_a.assets["se"].sha256 == expected["se"]
    assert section_a.assets["tone"].asset_id == "p3-brand-a:tone"
    assert section_a.assets["se"].asset_id == "p3-brand-a:se"

    music = next(t for t in section_a.logical_tracks if t.role == "music")
    sfx = next(t for t in section_a.logical_tracks if t.role == "sfx")
    assert music.anchors[0].record_span == RecordFrameSpan(start_frame=0, end_frame=90)
    assert music.anchors[0].gain_mb == DEFAULT_BGM_GAIN_MB
    assert music.anchors[0].fade_in_frames == DEFAULT_FADE_FRAMES
    assert music.anchors[0].fade_out_frames == DEFAULT_FADE_FRAMES
    assert sfx.anchors[0].record_span == RecordFrameSpan(start_frame=90, end_frame=105)
    assert sfx.anchors[0].gain_mb == DEFAULT_SE_GAIN_MB
    assert sfx.anchors[0].fade_in_frames == 0
    assert section_a.ducking.declared is True
    assert section_a.ducking.trigger_role == "dialogue"
    assert section_a.ducking.applied_in == "external_mix"
    assert section_a.targets.channels == 2
    assert section_a.targets.sample_rate_hz == 48000
    assert section_a.verify_hash() is True
    verify_role_separation(section_a)


def test_double_compile_is_byte_identical(
    profile_a: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> None:
    assert _section(profile_a, registry).canonical_bytes() == _section(
        profile_a, registry
    ).canonical_bytes()


def test_approved_ab_audio_sections_differ_only_in_declared_dims(
    section_a: AudioProfileSection, section_b: AudioProfileSection
) -> None:
    golden = json.loads(GOLDEN_PATH.read_bytes())
    expected_a = cast("dict[str, str]", golden["fixtures"]["p3-brand-a"]["assets"])
    expected_b = cast("dict[str, str]", golden["fixtures"]["p3-brand-b"]["assets"])
    assert section_a.assets["tone"].sha256 == expected_a["tone"]
    assert section_b.assets["tone"].sha256 == expected_b["tone"]
    assert section_a.assets["se"].sha256 != section_b.assets["se"].sha256

    leaves_a = _flatten(section_a.model_dump(mode="json"))
    leaves_b = _flatten(section_b.model_dump(mode="json"))
    diffs = {
        path
        for path in set(leaves_a) | set(leaves_b)
        if leaves_a.get(path) != leaves_b.get(path)
    } - SEAL_FIELDS
    assert diffs == {
        "assets.tone.asset_id",
        "assets.tone.path",
        "assets.tone.sha256",
        "assets.se.asset_id",
        "assets.se.path",
        "assets.se.sha256",
    }, diffs
    assert section_a.section_sha256 != section_b.section_sha256
    assert section_a.targets == section_b.targets
    assert section_a.ducking == section_b.ducking


# ----------------------------------------------------------------- rights ----


def _with_entry(registry: RegistrySnapshot, entry: AssetEntry) -> RegistrySnapshot:
    others = tuple(e for e in registry.entries if e.asset_id != entry.asset_id)
    return register_assets(*others, entry)


def _tone_entry(registry: RegistrySnapshot) -> AssetEntry:
    return next(e for e in registry.entries if e.asset_id == "p3-brand-a:tone")


def test_expired_unapproved_wrong_use_music_is_typed(
    profile_a: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> None:
    entry = _tone_entry(registry)
    expired = entry.model_copy(update={"expiry_date": "2025-12-31"})
    with pytest.raises(AudioProfileError) as raised:
        _section(profile_a, registry, compile_registry=_with_entry(registry, expired))
    assert raised.value.code == "rights_expired"

    unapproved = entry.model_copy(update={"approved": False})
    with pytest.raises(AudioProfileError) as raised:
        _section(profile_a, registry, compile_registry=_with_entry(registry, unapproved))
    assert raised.value.code == "rights_unapproved"

    wrong_use = entry.model_copy(update={"usage": "se"})
    with pytest.raises(AudioProfileError) as raised:
        _section(profile_a, registry, compile_registry=_with_entry(registry, wrong_use))
    assert raised.value.code == "rights_wrong_use"

    with pytest.raises(AudioProfileError) as raised:
        _section(
            profile_a,
            registry,
            compile_registry=_with_entry(
                registry, entry.model_copy(update={"sha256": "c" * 64})
            ),
        )
    assert raised.value.code == "rights_changed_bytes"


# -------------------------------------------------------------- separation ----


def test_dialogue_processing_on_ambient_is_typed(
    section_a: AudioProfileSection,
) -> None:
    conflated = section_a.model_copy(
        update={
            "assignments": (
                ProcessingAssignment(
                    kind="loudness_normalization",
                    role="dialogue",
                    on_track_role="ambient",
                ),
            )
        }
    )
    with pytest.raises(AudioProfileError) as raised:
        verify_role_separation(conflated)
    assert raised.value.code == "role_conflation"

    ducked_ambient = section_a.model_copy(
        update={
            "ducking": {
                "declared": True,
                "trigger_role": "ambient",
                "applied_in": "external_mix",
            }
        }
    )
    with pytest.raises(AudioProfileError) as raised:
        verify_role_separation(ducked_ambient)
    assert raised.value.code == "role_conflation"

    verify_role_separation(section_a)


def test_logical_track_conflation_in_models_is_refused(
    section_a: AudioProfileSection,
) -> None:
    conflated_tracks = (
        LogicalAudioTrack(role="dialogue", resolve_track_index=3),
        LogicalAudioTrack(role="ambient", resolve_track_index=3),
        LogicalAudioTrack(role="music", resolve_track_index=3),
        LogicalAudioTrack(role="sfx", resolve_track_index=4),
    )
    with pytest.raises(ValidationError):
        AudioProfileSection.model_validate(
            section_a.model_copy(
                update={"logical_tracks": conflated_tracks}
            ).model_dump()
        )
    with pytest.raises(ValidationError):
        AudioSection(
            rung="manual_fallback",
            reason="explicit",
            profile_section_sha256=section_a.section_sha256,
            logical_tracks=conflated_tracks,
            targets=section_a.targets,
            total_frames=section_a.total_frames,
            frame_rate=section_a.frame_rate,
        )


# ---------------------------------------------------------------- anchors ----


def test_anchor_bounds_and_inexact_frames_are_typed(
    profile_a: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> None:
    with pytest.raises(AudioProfileError) as raised:
        _section(profile_a, registry, total_frames=50)
    assert raised.value.code == "anchor_out_of_timeline"

    with pytest.raises(AudioProfileError) as raised:
        _section(
            profile_a,
            registry,
            frame_rate=RationalFrameRate(num=30000, den=1001),
        )
    assert raised.value.code == "anchor_frames_inexact"


# ------------------------------------------------------------------ ladder ----


def test_unverified_preset_is_typed() -> None:
    unverified = PresetSupport(verified=False, evidence_sha=None)
    with pytest.raises(AudioProfileError) as raised:
        choose_audio_rung(
            "verified_preset", external_available=True, preset=unverified
        )
    assert raised.value.code == "unverified_preset"

    assert (
        choose_audio_rung(None, external_available=True, preset=unverified)
        == "external_mix_derivative"
    )
    assert (
        choose_audio_rung(
            "manual_fallback", external_available=False, preset=unverified
        )
        == "manual_fallback"
    )
    verified = PresetSupport(verified=True, evidence_sha=PROBE_SHA)
    assert (
        choose_audio_rung(
            "verified_preset", external_available=False, preset=verified
        )
        == "verified_preset"
    )


def test_unavailable_plugin_is_typed() -> None:
    with pytest.raises(AudioProfileError) as raised:
        choose_audio_rung(
            "external_mix_derivative",
            external_available=False,
            preset=audio_preset_support_from_matrix(Path("/nonexistent.json")),
        )
    assert raised.value.code == "plugin_unavailable"

    with pytest.raises(AudioMixError) as raised:
        require_mix_filters(frozenset({"amix", "volume"}), ducking=False)
    assert raised.value.code == "plugin_unavailable"
    with pytest.raises(AudioMixError) as raised:
        require_mix_filters(
            frozenset({"amix", "volume", "afade", "apad", "atrim", "aformat", "ebur128"}),
            ducking=True,
        )
    assert raised.value.code == "plugin_unavailable"
    require_mix_filters(
        frozenset(
            {"amix", "volume", "afade", "apad", "atrim", "aformat", "ebur128"}
        ),
        ducking=False,
    )


def test_preset_support_from_matrix_fails_closed(tmp_path: Path) -> None:
    assert audio_preset_support_from_matrix(Path("/nonexistent.json")).verified is False

    def _finding(name: str, *, verified: bool) -> ApiFindingEntry:
        return ApiFindingEntry(
            finding=name,
            api_available=True,
            live_verified=verified,
            evidence_refs=(
                EvidenceRef(path="audio-preset-probe/report.json", sha256=PROBE_SHA),
            ),
            limitations="probe limits",
        )

    partial = tmp_path / "partial.json"
    append_matrix_findings(
        partial, (_finding("fairlight-audio-setting-roundtrip", verified=True),)
    )
    assert audio_preset_support_from_matrix(partial).verified is False

    verified = tmp_path / "verified.json"
    append_matrix_findings(verified, (_finding(PRESET_FINDING, verified=True),))
    support = audio_preset_support_from_matrix(verified)
    assert support.verified is True
    assert support.evidence_sha == PROBE_SHA

    downgraded = tmp_path / "down.json"
    append_matrix_findings(downgraded, (_finding(PRESET_FINDING, verified=False),))
    assert audio_preset_support_from_matrix(downgraded).verified is False


# ------------------------------------------------- external mixed derivative --


@dataclass(frozen=True, slots=True)
class Pinned:
    ffmpeg: Path
    ffprobe: Path


@pytest.fixture(scope="module")
def tools() -> Pinned:
    try:
        pinned = load_pinned_tools(Path("config/toolchains/phase-0c-v2.json"))
    except Exception as error:  # noqa: BLE001 (skip on any toolchain failure)
        pytest.skip(f"pinned toolchain unavailable: {error}")
    return Pinned(ffmpeg=pinned.ffmpeg, ffprobe=pinned.ffprobe)


def _bed_wav(tools: Pinned, out_dir: Path) -> Path:
    bed = out_dir / "bed.wav"
    argv = (
        str(tools.ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=exprs=0.2*sin(2*PI*220*t):s=48000:d=6",
        "-c:a",
        "pcm_s16le",
        "-map_metadata",
        "-1",
        str(bed),
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return bed


def _render(tools: Pinned, section: AudioProfileSection, bed: Path, out: Path) -> MixedDerivative:
    return render_mixed_derivative(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        section=section,
        bed_media=bed,
        output=out,
    )


def test_derivative_render_is_deterministic_and_normalizes(
    tools: Pinned, section_a: AudioProfileSection, tmp_path: Path
) -> None:
    bed = _bed_wav(tools, tmp_path)
    first = _render(tools, section_a, bed, tmp_path / "mix-a.wav")
    second = _render(tools, section_a, bed, tmp_path / "mix-a-again.wav")
    assert first.sha256 == second.sha256
    assert first.sample_rate_hz == 48000
    assert first.channels == section_a.targets.channels
    assert first.total_samples == TOTAL_FRAMES * 48000 * RATE.den // RATE.num

    targets = section_a.targets
    assert first.integrated_mlufs is not None
    assert (
        abs(first.integrated_mlufs - targets.loudness_target_mlufs)
        <= targets.loudness_tolerance_mlufs
    )
    assert first.peak_mb <= targets.max_peak_mb
    assert len(first.iterations) >= 1
    assert first.iterations[-1].converged is True


def test_ab_derivative_hashes_differ(
    tools: Pinned,
    section_a: AudioProfileSection,
    section_b: AudioProfileSection,
    tmp_path: Path,
) -> None:
    bed = _bed_wav(tools, tmp_path)
    derivative_a = _render(tools, section_a, bed, tmp_path / "mix-a.wav")
    derivative_b = _render(tools, section_b, bed, tmp_path / "mix-b.wav")
    assert derivative_a.sha256 != derivative_b.sha256
    assert derivative_a.argv != derivative_b.argv


def test_derivative_missing_bed_is_typed(
    tools: Pinned, section_a: AudioProfileSection, tmp_path: Path
) -> None:
    with pytest.raises(AudioMixError) as raised:
        _render(
            tools, section_a, tmp_path / "no-such-bed.wav", tmp_path / "mix.wav"
        )
    assert raised.value.code == "bed_missing"


def test_preview_final_derivative_mismatch_is_typed(
    tools: Pinned, section_a: AudioProfileSection, tmp_path: Path
) -> None:
    bed = _bed_wav(tools, tmp_path)
    derivative = _render(tools, section_a, bed, tmp_path / "mix.wav")
    verify_derivative_parity(
        derivative.sha256, preview_sha=derivative.sha256, final_sha=derivative.sha256
    )
    with pytest.raises(AudioMixError) as raised:
        verify_derivative_parity(
            derivative.sha256,
            preview_sha=derivative.sha256,
            final_sha="c" * 64,
        )
    assert raised.value.code == "derivative_mismatch"


# ------------------------------------------------------- Todo-52 QC mapping ----


def _measure(
    *,
    peak_mb: int = -6000,
    integrated: int | None = -16000,
    channels: int = 2,
) -> AudioMeasure:
    return AudioMeasure(
        peak_sample=16000,
        peak_mb=peak_mb,
        clipped_samples=0,
        loudness=LoudnessSummary(
            method="ebur128" if integrated is not None else "rms_fallback",
            honest_label=(
                "itu_r_bs_1770_ebur128" if integrated is not None else "rms_based_not_bs1770"
            ),
            integrated_loudness_mlufs=integrated,
            rms_mean_mb=-20000,
        ),
        silence_spans=(),
        probed_channels=channels,
    )


def test_peak_loudness_channel_violations_are_typed(
    section_a: AudioProfileSection,
) -> None:
    thresholds = audio_thresholds_from_targets(section_a.targets)
    assert thresholds.expected_channels == section_a.targets.channels
    assert thresholds.loudness_min_mlufs == (
        section_a.targets.loudness_target_mlufs - section_a.targets.loudness_tolerance_mlufs
    )
    policy = LiveAudioPolicy(threshold_version="audio-test-targets-v1", audio=thresholds)
    inputs = (SHA,)

    assert evaluate_audio_measurements(_measure(), policy, inputs) == ()
    rules = {
        issue.rule_id
        for issue in evaluate_audio_measurements(
            _measure(peak_mb=-100), policy, inputs
        )
    }
    assert rules == {"audio_peak_over"}
    rules = {
        issue.rule_id
        for issue in evaluate_audio_measurements(
            _measure(integrated=-8000), policy, inputs
        )
    }
    assert rules == {"audio_loudness_out_of_range"}
    rules = {
        issue.rule_id
        for issue in evaluate_audio_measurements(_measure(channels=1), policy, inputs)
    }
    assert rules == {"audio_channels_mismatch"}
    rules = {
        issue.rule_id
        for issue in evaluate_audio_measurements(
            _measure(integrated=None), policy, inputs
        )
    }
    assert rules == {"audio_loudness_unmeasured"}


# --------------------------------------------------------- package wiring ----


def _audio_section(section: AudioProfileSection, derivative: MixedDerivative) -> AudioSection:
    return AudioSection(
        rung="external_mix_derivative",
        reason="external_default",
        profile_section_sha256=section.section_sha256,
        derivative=AudioDerivativeRef(
            path=str(derivative.path),
            sha256=derivative.sha256,
            duration_samples=derivative.total_samples,
            sample_rate_hz=derivative.sample_rate_hz,
            channels=derivative.channels,
        ),
        logical_tracks=section.logical_tracks,
        targets=section.targets,
        total_frames=section.total_frames,
        frame_rate=section.frame_rate,
    )


def test_package_carries_audio_section_and_replaces_base_audio(
    section_a: AudioProfileSection, tools: Pinned, tmp_path: Path
) -> None:
    manifest = load_p2_manifest("p2-partial-build-restart")
    ir = ir_for(manifest)
    bed = _bed_wav(tools, tmp_path)
    derivative = _render(
        tools, section_a, bed, tmp_path / "program-mix.wav"
    )
    audio = _audio_section(section_a, derivative)
    bindings = audio_media_bindings(audio)
    assert len(bindings) == 1
    assert bindings[0].source_id == "audio.mix.derivative"
    assert MediaBinding.model_validate(bindings[0].model_dump(mode="json"))

    placements = audio_placements(audio, frame_origin=108000)
    assert len(placements) == 1
    assert placements[0].clip_info.track_type == "audio"
    assert placements[0].clip_info.track_index == 3
    assert placements[0].clip_info.record_frame == 108000

    require_audio_role_separation(audio)
    with pytest.raises(PackageCompileError) as raised:
        require_audio_role_separation(
            audio.model_copy(
                update={
                    "logical_tracks": (
                        LogicalAudioTrack(role="dialogue", resolve_track_index=3),
                        LogicalAudioTrack(role="ambient", resolve_track_index=3),
                        LogicalAudioTrack(role="music", resolve_track_index=3),
                        LogicalAudioTrack(role="sfx", resolve_track_index=4),
                    )
                }
            )
        )
    assert raised.value.code == "audio-role-conflation"

    declared = declared_media(manifest)
    request = PackageCompileRequest(
        ir=ir,
        lock=phase2_lock(),
        lock_sha256=lock_sha256(),
        declared_media=declared,
        presented_media=(*declared, *bindings),
        artifact_id="resolve-package-audio-test",
    )
    legacy = compile_resolve_package(request)
    assert legacy.audio is None
    with_audio = compile_resolve_package(
        dataclasses.replace(request, audio_section=audio)
    )
    assert with_audio.audio is not None
    assert with_audio.audio.rung == "external_mix_derivative"
    audio_rows = [
        p for p in with_audio.placements if p.clip_info.track_type == "audio"
    ]
    assert {p.item_id for p in audio_rows} == {"audio.mix.derivative"}
    assert audio_rows[0].clip_info.record_frame == 108000
    assert audio_rows[0].clip_info.track_index == 3
    base_audio = [
        p
        for p in legacy.placements
        if p.clip_info.track_type == "audio" and p.item_id.startswith("a")
    ]
    assert base_audio, "fixture IR carries base audio placements"
    assert {
        p.item_id for p in with_audio.placements if p.item_id in {b.item_id for b in base_audio}
    } == set()
    with pytest.raises(PackageCompileError):
        compile_resolve_package(
            dataclasses.replace(request, audio_section=audio, presented_media=declared)
        )


def test_malformed_audio_sections_are_refused(section_a: AudioProfileSection) -> None:
    with pytest.raises(ValidationError):
        AudioSection(
            rung="external_mix_derivative",
            reason="external_default",
            profile_section_sha256=section_a.section_sha256,
            derivative=None,
            logical_tracks=section_a.logical_tracks,
            targets=section_a.targets,
            total_frames=section_a.total_frames,
            frame_rate=section_a.frame_rate,
        )
    with pytest.raises(ValidationError):
        AudioSection(
            rung="verified_preset",
            reason="profile_declared_preset",
            profile_section_sha256=section_a.section_sha256,
            derivative=None,
            preset_name=None,
            preset_probe_sha256=None,
            logical_tracks=section_a.logical_tracks,
            targets=section_a.targets,
            total_frames=section_a.total_frames,
            frame_rate=section_a.frame_rate,
        )
    with pytest.raises(ValidationError):
        AudioAnchor(
            item_id="bad",
            role="music",
            asset_kind="tone",
            record_span=RecordFrameSpan(start_frame=0, end_frame=10),
            offset_samples=0,
            gain_mb=DEFAULT_BGM_GAIN_MB,
            fade_in_frames=10,
            fade_out_frames=10,
        )


# ------------------------------------------------------------ probe honest ----


def _probe_report(
    *,
    applied: bool,
    readback: bool,
    methods: tuple[MethodProbe, ...],
) -> AudioPresetProbeReport:
    return AudioPresetProbeReport(
        schema_version="audio-preset-probe-report-v1",
        binding="21.0.4.5",
        preset_rung="fixed_track_preset" if applied else "none",
        applied=applied,
        readback_verified=readback,
        methods=methods,
        settings=(
            SettingProbe(
                setting="timelineAudioOutputChannels",
                value="2",
                set_roundtrip=readback,
            ),
        ),
        steps=(),
    )


def test_preset_probe_findings_derive_honestly() -> None:
    no_surface = _probe_report(
        applied=False,
        readback=False,
        methods=(MethodProbe(name="ApplyTrackPreset", present=False),),
    )
    findings = derive_preset_findings(no_surface, report_sha=PROBE_SHA)
    preset = next(f for f in findings if f.finding == PRESET_FINDING)
    assert preset.api_available is False
    assert preset.live_verified is False

    callable_no_readback = _probe_report(
        applied=True,
        readback=False,
        methods=(MethodProbe(name="NormalizeAudioTrackLevels", present=True),),
    )
    findings = derive_preset_findings(callable_no_readback, report_sha=PROBE_SHA)
    preset = next(f for f in findings if f.finding == PRESET_FINDING)
    assert preset.api_available is True
    assert preset.live_verified is False
    setting = next(f for f in findings if f.finding != PRESET_FINDING)
    assert setting.live_verified is False

    verified = _probe_report(
        applied=True,
        readback=True,
        methods=(MethodProbe(name="ApplyTrackPreset", present=True),),
    )
    findings = derive_preset_findings(verified, report_sha=PROBE_SHA)
    assert all(f.live_verified for f in findings)
    row = preset_capability_row(
        verified,
        (EvidenceRef(path="audio-preset-probe/report.json", sha256=PROBE_SHA),),
    )
    assert row.capability == "fairlight_audio_preset"
    assert row.live_verified is True

    assert AudioPresetProbeReport.model_validate_json(
        canonical_model_bytes(verified)
    ).applied is True


# ------------------------------------------------------------------- live ----


ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
EVIDENCE_NAME = "resolve"


def live_evidence_root() -> Path:
    return ATTEMPT / EVIDENCE_NAME


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


@dataclass(frozen=True, slots=True)
class LiveEnv:
    report: Path
    evidence: Path
    ffmpeg: Path
    ffprobe: Path
    fixture_dir: Path


@pytest.fixture(scope="session")
def live_env(request: pytest.FixtureRequest) -> Iterator[LiveEnv]:
    report_path = _report_path(request.config)
    if report_path is None:
        pytest.skip("resolve host report not found: pass --resolve-evidence")
    evidence = Path(str(request.config.getoption("--resolve-evidence"))) if (
        request.config.getoption("--resolve-evidence")
    ) else None
    if evidence is None:
        pytest.skip("live requires --resolve-evidence")
    if request.config.getoption("--exclusive-resolve-lease"):
        lock_path = evidence / "resolve-lease.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pytest.skip("exclusive resolve lease held by another session")
        print(f"exclusive resolve lease acquired: {lock_path}")
        try:
            yield _live_env(report_path, evidence)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        return
    yield _live_env(report_path, evidence)


def _live_env(report_path: Path, evidence: Path) -> LiveEnv:
    ffmpeg = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffmpeg"
    ffprobe = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffprobe"
    fixture_dir = evidence.parent / "phase-0a/fixture"
    missing = [
        str(path)
        for path in (ffmpeg, ffprobe, fixture_dir / "source.mov")
        if not Path(path).is_file()
    ]
    if missing:
        pytest.skip(f"live pinned tools or fixture media missing: {missing}")
    return LiveEnv(
        report=report_path,
        evidence=evidence,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        fixture_dir=fixture_dir,
    )


@dataclass(frozen=True, slots=True)
class ProbeRun:
    stdout: str
    report: dict[str, object]


@pytest.fixture(scope="session")
def preset_probe_run(live_env: LiveEnv) -> ProbeRun:
    bundle = live_env.evidence / "audio-preset-probe"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.resolve_bridge.audio_preset_probe",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--timeout",
        "240",
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        (bundle / "audio-preset-probe-report.json").read_bytes()
    )
    return ProbeRun(stdout=result.stdout, report=report)


@pytest.fixture(scope="session")
def audio_live_run(
    live_env: LiveEnv, preset_probe_run: ProbeRun
) -> subprocess.CompletedProcess[str]:
    bundle = live_env.evidence / "audio-live"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.presentation.audio_live",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--ffmpeg",
        str(live_env.ffmpeg),
        "--ffprobe",
        str(live_env.ffprobe),
        "--base-source",
        str(live_env.fixture_dir / "source.mov"),
        "--timeout",
        "900",
    )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1200)


@pytest.mark.resolve_live
def test_live_audio_preset_probe_records_honest_capability(
    preset_probe_run: ProbeRun,
) -> None:
    assert "audio-preset-probe: preset_supported=" in preset_probe_run.stdout
    report = preset_probe_run.report
    supported = report["preset_supported"] is True
    if supported:
        assert report["applied"] is True
        assert report["readback_verified"] is True
        methods = cast("list[dict[str, object]]", report["methods"])
        assert any(bool(row["present"]) for row in methods)
    else:
        assert not (
            report["applied"] is True and report["readback_verified"] is True
        )
    assert isinstance(report["steps"], list)
    findings = live_evidence_root() / "audio-preset-findings.json"
    if supported and findings.is_file():
        rows = json.loads(findings.read_bytes())
        findings_rows = cast("list[dict[str, object]]", rows["findings"])
        assert any(
            row["finding"] == PRESET_FINDING and row["live_verified"]
            for row in findings_rows
        )


@pytest.mark.resolve_live
def test_live_audio_external_mix_end_to_end(
    audio_live_run: subprocess.CompletedProcess[str],
) -> None:
    result = audio_live_run
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "audio-live: rung=external_mix_derivative" in result.stdout
    assert "audio-live: qc=passed" in result.stdout
    assert "audio-live: PASS" in result.stdout
    report = json.loads(
        (live_evidence_root() / "audio-live" / "audio-live-report.json").read_bytes()
    )
    assert report["passed"] is True
    assert report["qc"]["issues"] == []
    parity = report["parity"]
    assert parity["preview_sha"] == parity["final_sha"]
    assert parity["preview_sha"] == report["derivative"]["sha256"]
    assert Path(str(report["render"]["output_path"])).is_file()


@pytest.mark.resolve_live
def test_live_cleanup_leaves_no_owned_projects(live_env: LiveEnv) -> None:
    connection = connect(load_host_report(live_env.report))
    manager = connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"audio-live leaked owned projects: {owned}"
