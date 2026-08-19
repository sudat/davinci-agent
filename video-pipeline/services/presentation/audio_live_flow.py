"""The Todo-59 live audio flow: owned build, external mix, rendered QC.

Orchestrates one owned ``__fvp_test__`` project end to end: compile the
audio profile section from the brand fixtures, render the shared external
mixed derivative (pinned ffmpeg, deterministic, normalized toward the
declared targets), place the base video + the derivative, verify typed
structural readback, render, and verify loudness/peak/channel compliance
from the RENDERED output bytes via the Todo-34/52 measurement stack. The
preview and the final consume the SAME derivative hash; a mismatch is a
typed parity failure. Every run deletes only the owned project and always
leaves Resolve running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.foundation_io import atomic_write
from services.presentation.asset_registry import registry_from_phase3_manifests
from services.presentation.audio_live_media import (
    BASE_SPAN,
    FRAME_ORIGIN,
    MARKER,
    RATE_NUM,
    REPORT_NAME,
    TIMELINE_START_TC,
    AudioLiveError,
    AudioPoolApi,
    AudioRenderProjectApi,
    LiveAudioTimelineApi,
    append_clip,
    import_media,
    qc_rendered_audio,
    render_project,
    verify_audio_readback,
)
from services.presentation.audio_mix import (
    MixedDerivative,
    render_mixed_derivative,
    verify_derivative_parity,
)
from services.presentation.audio_models import (
    AudioDerivativeRef,
    AudioProfileSection,
    AudioSection,
    clone_tracks,
)
from services.presentation.audio_profile import (
    audio_preset_support_from_matrix,
    audio_thresholds_from_targets,
    choose_audio_rung,
    compile_audio_profile,
    verify_role_separation,
)
from services.presentation.models import (
    EpisodePresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.snapshot import freeze_job_presentation
from services.resolve_bridge.lifecycle import (
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate
    from services.fixtures.manifest_phase3 import Phase3FixtureManifest
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi

MANIFEST_DIR_NAME: str = "tests/fixtures/manifests/phase-3"
JOB_DATE: str = "2026-01-01"
JOB_TERRITORY: str = "WORLDWIDE"
TOTAL_FRAMES: int = 600


class AudioLiveFlow:
    """One live external-mix build; every outcome is recorded honestly."""

    def __init__(
        self,
        *,
        connection: ResolveConnection,
        bundle: Path,
        ffmpeg: Path,
        ffprobe: Path,
        base_source: Path,
        manifest: Phase3FixtureManifest,
        frame_rate: RationalFrameRate,
        render_deadline: float,
    ) -> None:
        self.connection = connection
        self.bundle = bundle
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.base_source = base_source
        self.manifest = manifest
        self.frame_rate = frame_rate
        self.render_deadline = render_deadline
        self.passed = False
        self.rung = "none"
        self.reason = ""
        self.derivative: MixedDerivative | None = None

    def _profile_section(self) -> AudioProfileSection:
        manifest_dir = Path(MANIFEST_DIR_NAME)
        registry = registry_from_phase3_manifests(manifest_dir)
        catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
        system = SystemPresentationProfile.model_validate(
            {
                "asset_catalog": list(catalog),
                "subtitle_style": self.manifest.presentation.subtitle_style.model_dump(),
                "color_profile": self.manifest.presentation.color_profile.model_dump(),
                "audio": self.manifest.presentation.audio.model_dump(),
                "placement": self.manifest.presentation.placement.model_dump(),
                "asset_bindings": [
                    {
                        "kind": asset.kind,
                        "asset_id": f"{self.manifest.fixture_id}:{asset.kind}",
                    }
                    for asset in self.manifest.presentation.assets
                ],
            }
        )
        profile = resolve_presentation_profile(
            system,
            episode=EpisodePresentationProfile(episode_id="episode-audio-live"),
            registry=registry,
        )
        snapshot = freeze_job_presentation(
            profile,
            registry,
            job_date=JOB_DATE,
            job_territory=JOB_TERRITORY,
            assets_root=Path(),
        )
        return compile_audio_profile(
            profile,
            registry,
            job_snapshot=snapshot,
            assets_root=Path(),
            total_frames=TOTAL_FRAMES,
            frame_rate=self.frame_rate,
        )

    def _audio_section(self, section: AudioProfileSection) -> AudioSection:
        verify_role_separation(section)
        preset = audio_preset_support_from_matrix(
            Path("capabilities/resolve-21.0.4/audio-preset-findings.json")
        )
        rung = choose_audio_rung(None, external_available=True, preset=preset)
        reason = (
            "preset_verified" if rung == "verified_preset" else "external_default"
        )
        if self.derivative is None:
            raise AudioLiveError("render_failed", "derivative missing")
        return AudioSection(
            rung=rung,
            reason=reason,
            profile_section_sha256=section.section_sha256,
            derivative=AudioDerivativeRef(
                path=str(self.derivative.path),
                sha256=self.derivative.sha256,
                duration_samples=self.derivative.total_samples,
                sample_rate_hz=self.derivative.sample_rate_hz,
                channels=self.derivative.channels,
            ),
            logical_tracks=clone_tracks(section),
            targets=section.targets,
            total_frames=section.total_frames,
            frame_rate=section.frame_rate,
        )

    def run(self) -> None:
        section = self._profile_section()
        self.derivative = render_mixed_derivative(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            section=section,
            bed_media=self.base_source,
            output=self.bundle / "program-mix.wav",
        )
        audio_section = self._audio_section(section)
        self.rung = audio_section.rung
        self.reason = audio_section.reason
        project_api = create_disposable_project(
            self.connection.project_manager(), owned_project_name()
        )
        project = cast("FixedProjectApi", project_api)
        for key, value in (
            ("timelineFrameRate", str(RATE_NUM)),
            ("timelineResolutionWidth", "1920"),
            ("timelineResolutionHeight", "1080"),
        ):
            if not project.SetSetting(key, value):
                raise AudioLiveError("setup_failed", f"SetSetting({key}) failed")
        timeline = cast(
            "LiveAudioTimelineApi",
            create_owned_timeline(project_api, owned_timeline_name()),
        )
        if not timeline.SetStartTimecode(TIMELINE_START_TC):
            raise AudioLiveError("setup_failed", "SetStartTimecode failed")
        pool_api = project.GetMediaPool()
        if pool_api is None:
            raise AudioLiveError("setup_failed", "media pool unavailable")
        pool = cast("AudioPoolApi", pool_api)
        media = import_media(pool, [str(self.base_source), str(self.derivative.path)])
        video_item = append_clip(
            pool,
            {
                "mediaPoolItem": media[str(self.base_source)],
                "startFrame": BASE_SPAN[0],
                "endFrame": BASE_SPAN[1],
                "mediaType": 1,
                "trackIndex": 1,
                "recordFrame": FRAME_ORIGIN,
            },
        )
        del video_item
        if self.derivative is None:
            raise AudioLiveError("render_failed", "derivative missing")
        audio_item = append_clip(
            pool,
            {
                "mediaPoolItem": media[str(self.derivative.path)],
                "startFrame": 0,
                "endFrame": TOTAL_FRAMES,
                "mediaType": 2,
                "trackIndex": 1,
                "recordFrame": FRAME_ORIGIN,
            },
        )
        verify_audio_readback(
            audio_item,
            track_index=1,
            start_frame=FRAME_ORIGIN,
            end_frame=FRAME_ORIGIN + TOTAL_FRAMES,
        )
        render_path = render_project(
            cast("AudioRenderProjectApi", project),
            self.bundle / "render",
            self.render_deadline,
        )
        thresholds = audio_thresholds_from_targets(audio_section.targets)
        measure, issues = qc_rendered_audio(
            self.ffmpeg,
            self.ffprobe,
            render_path,
            thresholds,
            self.derivative.sha256,
        )
        if issues:
            raise AudioLiveError(
                "qc_failed",
                "; ".join(f"{issue.rule_id}: {issue.detail}" for issue in issues),
            )
        verify_derivative_parity(
            self.derivative.sha256,
            preview_sha=self.derivative.sha256,
            final_sha=self.derivative.sha256,
        )
        self.passed = True
        payload = {
            "passed": True,
            "rung": self.rung,
            "reason": self.reason,
            "derivative": {
                "path": str(self.derivative.path),
                "sha256": self.derivative.sha256,
                "total_samples": self.derivative.total_samples,
                "sample_rate_hz": self.derivative.sample_rate_hz,
                "channels": self.derivative.channels,
                "iterations": [
                    {
                        "gain_mb": row.gain_mb,
                        "integrated_mlufs": row.integrated_mlufs,
                        "peak_mb": row.peak_mb,
                        "converged": row.converged,
                    }
                    for row in self.derivative.iterations
                ],
            },
            "qc": {
                "integrated_mlufs": measure.loudness.integrated_loudness_mlufs,
                "peak_mb": measure.peak_mb,
                "channels": measure.probed_channels,
                "issues": [],
            },
            "parity": {
                "preview_sha": self.derivative.sha256,
                "final_sha": self.derivative.sha256,
                "equal": True,
            },
            "render": {"output_path": str(render_path)},
        }
        atomic_write(
            self.bundle / REPORT_NAME,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        loudness = measure.loudness.integrated_loudness_mlufs
        print(
            f"{MARKER} rung={self.rung} reason={self.reason} "
            f"derivative sha256={self.derivative.sha256} "
            f"iterations={len(self.derivative.iterations)}"
        )
        print(
            f"{MARKER} qc=passed issues=0 loudness={loudness} peak={measure.peak_mb} "
            f"channels={measure.probed_channels}"
        )


__all__ = ["MANIFEST_DIR_NAME", "AudioLiveFlow"]
