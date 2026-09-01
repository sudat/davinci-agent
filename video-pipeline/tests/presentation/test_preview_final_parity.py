"""Todo-61 acceptance: Preview/Final Presentation parity + invalidation.

Offline: the comparator compares preview vs final along the DECLARED
manifest-anchored dims (structural dims exactly; audio duration/loudness/
peak and color metadata/statistics under the declared Todo-59/60
tolerances; shared derivatives by identical hash), types every mismatch
(``unexplained_difference`` with diff payload, ``missing_shared_derivative``,
``derivative_hash_mismatch``), and REJECTS a byte-equality-only basis with
the typed ``invalid_comparison_basis``. Measured verdicts come from
rendered bytes via the pinned toolchain, never from container bytes.
Live: one Presentation Manifest renders preview AND final through the
real bridge; both consume the same shared derivatives; parity passes
under the declared tolerances.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.foundation_io import sha256_file
from services.presentation.asset_registry import registry_from_phase3_manifests
from services.presentation.audio_live_media import qc_rendered_audio
from services.presentation.audio_models import AudioTargets
from services.presentation.audio_profile import audio_thresholds_from_targets
from services.presentation.models import EpisodePresentationProfile
from services.presentation.parity import (
    DECLARED_PARITY_DIMS,
    ParityError,
    compare_parity,
    default_basis,
    tolerances_from_targets,
    validate_comparison_basis,
)
from services.presentation.parity_live_fixture import (
    item_observations,
    placement_observation,
)
from services.presentation.parity_live_media import probe_duration_ms
from services.presentation.parity_models import (
    AudioObservation,
    ColorObservation,
    ComparisonBasis,
    CueRegionObservation,
    ParitySide,
    ParityTolerances,
    PlacementObservation,
)
from services.presentation.profiles import resolve_presentation_profile
from services.preview.tools import load_pinned_tools
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import PROJECT_PREFIX
from services.resolve_bridge.readiness import load_host_report
from tests.presentation.test_manifest import (
    MANIFEST_DIR,
    _channel,
    _compile,
    _edit_plan,
    _system,
    _timeline_ir,
)

if TYPE_CHECKING:
    from services.presentation.manifest import PresentationManifest

RATE = RationalFrameRate(num=30, den=1)
TOTAL_FRAMES = 180
SAMPLES = TOTAL_FRAMES * 48000 // 30
MIX_SHA = "1" * 64
OVERLAY_SHA = "2" * 64


def _targets() -> AudioTargets:
    return AudioTargets(
        loudness_target_mlufs=-16000,
        loudness_tolerance_mlufs=1000,
        max_peak_mb=-600,
        channels=2,
        sample_rate_hz=48000,
    )


def _audio(
    *,
    duration_samples: int = SAMPLES,
    integrated: int | None = -16000,
    peak_mb: int = -1200,
    channels: int = 2,
) -> AudioObservation:
    return AudioObservation(
        duration_samples=duration_samples,
        integrated_mlufs=integrated,
        peak_mb=peak_mb,
        channels=channels,
    )


def _color(
    *,
    color_space: str = "bt709",
    region_overrides: dict[str, int] | None = None,
) -> ColorObservation:
    means = {"gray75": 191, "yellow": 170, "cyan": 134, "green": 112}
    if region_overrides:
        means.update(region_overrides)
    return ColorObservation(
        color_space=color_space,
        color_transfer="bt709",
        color_primaries="bt709",
        region_means=means,
    )


def _cues() -> tuple[CueRegionObservation, ...]:
    return (
        CueRegionObservation(
            item_id="st-1",
            text="first cue",
            lines=("first cue",),
            start_frame=0,
            end_frame=30,
            style_id="style-a",
            font_size_px=54,
            margin_bottom_px=120,
            primary_color_hex="#FFFFFF",
        ),
    )


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-a.json").read_bytes()
    )


@pytest.fixture(scope="module")
def manifest_a(brand_a: Phase3FixtureManifest) -> PresentationManifest:
    registry = registry_from_phase3_manifests(MANIFEST_DIR)
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    profile = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )
    return _compile(_edit_plan(brand_a), _timeline_ir(brand_a), profile, registry)


@dataclass(frozen=True, slots=True)
class SideOverride:
    """One side's deliberate deviation under test."""

    drop_derivative: str | None = None
    replace_derivative: tuple[str, str] | None = None
    audio: AudioObservation | None = None
    color: ColorObservation | None = None
    cues: tuple[CueRegionObservation, ...] | None = None


def _side(
    manifest: PresentationManifest, side: str, override: SideOverride | None = None
) -> ParitySide:
    override = override or SideOverride()
    derivatives: dict[str, str] = {"audio_mix": MIX_SHA, "overlay": OVERLAY_SHA}
    if override.drop_derivative is not None:
        derivatives.pop(override.drop_derivative, None)
    if override.replace_derivative is not None:
        derivatives[override.replace_derivative[0]] = override.replace_derivative[1]
    return ParitySide.model_validate(
        {
            "side": side,
            "manifest_sha256": manifest.manifest_sha256,
            "profile_snapshot_sha256": manifest.profile_snapshot_sha256,
            "items": item_observations(manifest),
            "assets": {
                kind: ref.sha256 for kind, ref in manifest.assets.items()
            },
            "placements": placement_observation(manifest),
            "cues": override.cues if override.cues is not None else _cues(),
            "derivatives": derivatives,
            "audio": override.audio if override.audio is not None else _audio(),
            "color": override.color if override.color is not None else _color(),
        }
    )


# ------------------------------------------------------------- comparator ----


def test_same_manifest_sides_pass(manifest_a: PresentationManifest) -> None:
    report = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_a, "final"),
        tolerances_from_targets(_targets()),
    )
    assert report.passed is True
    assert report.mismatches == ()
    assert report.dims_compared == DECLARED_PARITY_DIMS
    assert report.derivative_hashes == {"audio_mix": MIX_SHA, "overlay": OVERLAY_SHA}
    assert report.basis.container_bytes_only is False


def test_parity_is_manifest_anchored(manifest_a: PresentationManifest) -> None:
    brand_b = Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-b.json").read_bytes()
    )
    registry = registry_from_phase3_manifests(MANIFEST_DIR)
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    brand_a = Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-a.json").read_bytes()
    )
    profile_b = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )
    manifest_b = _compile(_edit_plan(brand_a), _timeline_ir(brand_a), profile_b, registry)
    report = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_b, "final"),
        tolerances_from_targets(_targets()),
    )
    assert report.passed is False
    dims = {mismatch.dim for mismatch in report.mismatches}
    assert "manifest_anchor" in dims
    assert "assets" in dims
    assert all(
        mismatch.code == "unexplained_difference" for mismatch in report.mismatches
    )
    diff = next(m for m in report.mismatches if m.dim == "manifest_anchor").diff
    assert diff["preview"] != diff["final"]


def test_missing_shared_derivative_is_typed(manifest_a: PresentationManifest) -> None:
    report = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_a, "final", SideOverride(replace_derivative=("audio_mix", "0" * 64))),
        tolerances_from_targets(_targets()),
    )
    assert report.passed is False
    assert report.codes() == ("derivative_hash_mismatch",)

    report = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_a, "final", SideOverride(drop_derivative="audio_mix")),
        tolerances_from_targets(_targets()),
    )
    assert report.codes() == ("missing_shared_derivative",)


def test_unexplained_difference_is_typed(manifest_a: PresentationManifest) -> None:
    drifted_cues = (
        CueRegionObservation(
            item_id="st-1",
            text="drifted cue",
            lines=("drifted cue",),
            start_frame=0,
            end_frame=30,
            style_id="style-a",
            font_size_px=54,
            margin_bottom_px=120,
            primary_color_hex="#FFFFFF",
        ),
    )
    report = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_a, "final", SideOverride(cues=drifted_cues)),
        tolerances_from_targets(_targets()),
    )
    assert report.passed is False
    cue_mismatch = next(m for m in report.mismatches if m.dim == "cues")
    assert cue_mismatch.code == "unexplained_difference"
    assert "first cue" in cue_mismatch.diff["preview"]
    assert "drifted cue" in cue_mismatch.diff["final"]


def test_audio_dims_are_toleranced(manifest_a: PresentationManifest) -> None:
    tolerances = tolerances_from_targets(_targets())
    ok = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(integrated=-16500))),
        _side(manifest_a, "final", SideOverride(audio=_audio(integrated=-15900))),
        tolerances,
    )
    assert ok.passed is True

    duration = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(duration_samples=SAMPLES))),
        _side(
            manifest_a,
            "final",
            SideOverride(
                audio=_audio(
                    duration_samples=(
                        SAMPLES + tolerances.audio_duration_tolerance_samples + 1
                    )
                )
            ),
        ),
        tolerances,
    )
    assert [m.dim for m in duration.mismatches] == ["audio_duration"]

    loudness = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(integrated=-16000))),
        _side(manifest_a, "final", SideOverride(audio=_audio(integrated=-14000))),
        tolerances,
    )
    assert {m.dim for m in loudness.mismatches} == {"audio_loudness"}

    unmeasured = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(integrated=None))),
        _side(manifest_a, "final"),
        tolerances,
    )
    assert {m.dim for m in unmeasured.mismatches} == {"audio_loudness"}

    peak = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(peak_mb=-500))),
        _side(manifest_a, "final", SideOverride(audio=_audio(peak_mb=-100))),
        tolerances,
    )
    assert {m.dim for m in peak.mismatches} == {"audio_peak"}

    channels = compare_parity(
        _side(manifest_a, "preview", SideOverride(audio=_audio(channels=1))),
        _side(manifest_a, "final"),
        tolerances,
    )
    assert {m.dim for m in channels.mismatches} == {"audio_loudness"}


def test_color_dims_are_toleranced(manifest_a: PresentationManifest) -> None:
    tolerances = tolerances_from_targets(_targets())
    ok = compare_parity(
        _side(manifest_a, "preview"),
        _side(
            manifest_a,
            "final",
            SideOverride(color=_color(region_overrides={"gray75": 195})),
        ),
        tolerances,
    )
    assert ok.passed is True

    metadata = compare_parity(
        _side(manifest_a, "preview"),
        _side(
            manifest_a,
            "final",
            SideOverride(color=_color(color_space="bt2020nc")),
        ),
        tolerances,
    )
    assert [m.dim for m in metadata.mismatches] == ["color_metadata"]

    statistics = compare_parity(
        _side(manifest_a, "preview"),
        _side(
            manifest_a,
            "final",
            SideOverride(
                color=_color(
                    region_overrides={
                        "gray75": 191 + tolerances.color_region_tolerance + 1
                    }
                )
            ),
        ),
        tolerances,
    )
    assert [m.dim for m in statistics.mismatches] == ["color_statistics"]


def test_byte_equality_only_basis_is_rejected_typed(manifest_a: PresentationManifest) -> None:
    byte_only = ComparisonBasis(dims=("manifest_anchor",), container_bytes_only=True)
    with pytest.raises(ParityError) as raised:
        validate_comparison_basis(byte_only)
    assert raised.value.code == "invalid_comparison_basis"

    flagged = compare_parity(
        _side(manifest_a, "preview"),
        _side(manifest_a, "final"),
        tolerances_from_targets(_targets()),
        basis=byte_only,
    )
    assert flagged.passed is False
    assert flagged.codes() == ("invalid_comparison_basis",)
    assert flagged.dims_compared == ()

    with pytest.raises(ParityError):
        validate_comparison_basis(ComparisonBasis(dims=("manifest_anchor",)))
    validate_comparison_basis(default_basis())


def test_tolerances_reuse_todo59_targets() -> None:
    tolerances = tolerances_from_targets(_targets())
    assert tolerances.audio_loudness_target_mlufs == -16000
    assert tolerances.audio_loudness_tolerance_mlufs == 1000
    assert tolerances.audio_max_peak_mb == -600
    assert tolerances.audio_channels == 2
    assert tolerances.color_region_tolerance == 6
    thresholds = audio_thresholds_from_targets(_targets())
    assert thresholds.loudness_min_mlufs == tolerances.audio_loudness_target_mlufs - (
        tolerances.audio_loudness_tolerance_mlufs
    )


def test_malformed_sides_and_tolerances_are_refused() -> None:
    with pytest.raises(ValidationError):
        ParityTolerances(
            audio_duration_tolerance_samples=0,
            audio_loudness_target_mlufs=-16000,
            audio_loudness_tolerance_mlufs=1000,
            audio_max_peak_mb=-600,
            audio_peak_tolerance_mb=200,
            audio_channels=2,
            color_region_tolerance=6,
        )
    with pytest.raises(ValidationError):
        ParitySide(
            side="preview",
            manifest_sha256="not-a-sha",
            profile_snapshot_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        PlacementObservation(
            intro_start_frame=10,
            intro_end_frame=5,
            outro_start_frame=20,
            outro_end_frame=30,
            safe_area_margin_px=0,
        )


# ------------------------------------------------- measured (pinned tools) ----


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


def _render_media(tools: Pinned, out: Path, *, gain_db: int = 0) -> Path:
    argv = (
        str(tools.ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "smptebars=size=320x180:rate=30",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=exprs=0.2*sin(2*PI*220*t):s=48000:d=2",
        "-t",
        "2",
        "-af",
        f"volume={gain_db}dB",
        "-c:v",
        "h264_videotoolbox",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out),
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return out


def _measured_side(
    tools: Pinned,
    manifest: PresentationManifest,
    media: Path,
    *,
    side: str,
) -> ParitySide:
    measure, _issues = qc_rendered_audio(
        tools.ffmpeg,
        tools.ffprobe,
        media,
        audio_thresholds_from_targets(_targets()),
        MIX_SHA,
    )
    audio = AudioObservation(
        duration_samples=probe_duration_ms(tools.ffprobe, media) * 48,
        integrated_mlufs=measure.loudness.integrated_loudness_mlufs,
        peak_mb=measure.peak_mb,
        channels=measure.probed_channels,
    )
    return _side(manifest, side, SideOverride(audio=audio, color=_color()))


def _declared_offline_tolerances() -> ParityTolerances:
    """Declared window for the synthetic 0.2-amplitude bed (not Todo-59 targets)."""

    return ParityTolerances(
        audio_duration_tolerance_samples=4800,
        audio_loudness_target_mlufs=-21000,
        audio_loudness_tolerance_mlufs=1000,
        audio_max_peak_mb=-600,
        audio_peak_tolerance_mb=200,
        audio_channels=2,
        color_region_tolerance=6,
    )


def test_measured_parity_comes_from_rendered_bytes(
    tools: Pinned, manifest_a: PresentationManifest, tmp_path: Path
) -> None:
    first = _render_media(tools, tmp_path / "a.mp4")
    second = _render_media(tools, tmp_path / "b.mp4")
    report = compare_parity(
        _measured_side(tools, manifest_a, first, side="preview"),
        _measured_side(tools, manifest_a, second, side="final"),
        _declared_offline_tolerances(),
    )
    assert report.passed is True, report.mismatches

    quiet = _render_media(tools, tmp_path / "quiet.mp4", gain_db=-9)
    drifted = compare_parity(
        _measured_side(tools, manifest_a, first, side="preview"),
        _measured_side(tools, manifest_a, quiet, side="final"),
        _declared_offline_tolerances(),
    )
    assert drifted.passed is False
    assert {m.dim for m in drifted.mismatches} & {"audio_loudness", "audio_peak"}


# ------------------------------------------------------------------- live ----

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)


def live_evidence_root() -> Path:
    return ATTEMPT / "resolve"


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
    missing = [str(path) for path in (ffmpeg, ffprobe) if not Path(path).is_file()]
    if missing:
        pytest.skip(f"live pinned tools missing: {missing}")
    return LiveEnv(
        report=report_path, evidence=evidence, ffmpeg=ffmpeg, ffprobe=ffprobe
    )


@pytest.fixture(scope="session")
def parity_live_run(
    live_env: LiveEnv,
) -> subprocess.CompletedProcess[str]:
    bundle = live_env.evidence / "parity-live"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.presentation.parity_live",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--ffmpeg",
        str(live_env.ffmpeg),
        "--ffprobe",
        str(live_env.ffprobe),
        "--timeout",
        "900",
    )
    return subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=1500
    )


@pytest.mark.resolve_live
def test_live_preview_final_parity_from_same_manifest(
    parity_live_run: subprocess.CompletedProcess[str],
) -> None:
    combined = parity_live_run.stdout + parity_live_run.stderr
    assert parity_live_run.returncode == 0, combined
    assert "parity-live: parity=PASS" in parity_live_run.stdout
    assert "parity-live: PASS" in parity_live_run.stdout
    report = json.loads(
        (live_evidence_root() / "parity-live" / "parity-live-report.json").read_bytes()
    )
    assert report["passed"] is True
    assert report["mismatches"] == []
    assert "manifest_sha256" in report
    bundle = live_evidence_root() / "parity-live"
    assert report["derivatives"]["audio_mix"] == sha256_file(
        bundle / "program-mix.wav"
    )
    assert report["derivatives"]["overlay"] == sha256_file(
        bundle / "overlay-media.mov"
    )
    assert Path(str(report["preview"]["render_path"])).is_file()
    assert Path(str(report["final"]["render_path"])).is_file()
    preview_audio = cast("dict[str, object]", report["preview"]["audio"])
    final_audio = cast("dict[str, object]", report["final"]["audio"])
    assert preview_audio["integrated_mlufs"] is not None
    assert final_audio["integrated_mlufs"] is not None
    assert abs(
        int(cast("int", preview_audio["duration_samples"]))
        - int(cast("int", final_audio["duration_samples"]))
    ) <= 4800


@pytest.mark.resolve_live
def test_live_cleanup_leaves_no_owned_projects(live_env: LiveEnv) -> None:
    connection = connect(load_host_report(live_env.report))
    manager = connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"parity-live leaked owned projects: {owned}"
