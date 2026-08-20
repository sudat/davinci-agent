"""The Todo-61 live parity flow: one manifest, two renders, measured parity.

One owned ``__fvp_test__`` timeline built from the shared derivatives
over the declared-recipe bars base, rendered TWICE through the real
bridge (preview and final geometry) and compared along the DECLARED
manifest-anchored dimensions under the declared tolerances. The
comparator consumes measured bytes only; container byte equality is
never asserted. Every run deletes only the owned project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.contracts.primitives import RationalFrameRate
from services.foundation_io import atomic_write
from services.presentation.audio_live_media import qc_rendered_audio
from services.presentation.audio_mix import MixedDerivative, render_mixed_derivative
from services.presentation.audio_profile import (
    audio_thresholds_from_targets,
    compile_audio_profile,
)
from services.presentation.color_live_media import render_bars_source
from services.presentation.overlay_live_media import GEO, OVERLAY_SPAN
from services.presentation.overlay_render import (
    RenderedOverlay,
    render_transparent_overlay,
)
from services.presentation.parity import compare_parity, tolerances_from_targets
from services.presentation.parity_live_build import ParityTimelineBuilder
from services.presentation.parity_live_fixture import (
    compile_parity_fixture,
    cue_observations,
    item_observations,
    placement_observation,
)
from services.presentation.parity_live_media import (
    MARKER,
    ParityRenderProjectApi,
    probe_bar_region_means,
    probe_color_metadata,
    probe_duration_ms,
    render_with_settings,
)
from services.presentation.parity_models import (
    AudioObservation,
    ColorObservation,
    ParitySide,
)
from services.presentation.snapshot import (
    JobPresentationSnapshot,
    freeze_job_presentation,
)

if TYPE_CHECKING:
    from services.presentation.asset_registry import AssetEntry, RegistrySnapshot
    from services.presentation.audio_models import AudioProfileSection
    from services.presentation.models import ResolvedPresentationProfile
    from services.qc.models import AudioThresholds
    from services.resolve_bridge.connection import ResolveConnection

RATE_NUM = 30
TOTAL_FRAMES = 600
# HD only, measured: Resolve renders SD-sized delivers through its bt601
# (smpte170m) path, so an SD preview genuinely differs from the HD final.
PREVIEW_GEO = (1280, 720)
FINAL_GEO = (1920, 1080)
REPORT_NAME = "parity-live-report.json"
OVERLAY_ASSET_ID = "p3-brand-a:overlay"
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"
SIDE_GEOS: dict[str, tuple[int, int]] = {"preview": PREVIEW_GEO, "final": FINAL_GEO}


class ParityLiveFlow:
    """One live preview/final parity run; every outcome is recorded honestly."""

    def __init__(
        self,
        *,
        connection: ResolveConnection,
        bundle: Path,
        ffmpeg: Path,
        ffprobe: Path,
        render_deadline: float,
    ) -> None:
        self.connection = connection
        self.bundle = bundle
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.render_deadline = render_deadline
        self.passed = False

    def _section(
        self, profile: ResolvedPresentationProfile, registry: RegistrySnapshot
    ) -> AudioProfileSection:
        snapshot: JobPresentationSnapshot = freeze_job_presentation(
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
            frame_rate=RationalFrameRate(num=RATE_NUM, den=1),
        )

    def _overlay(self, entries: tuple[AssetEntry, ...]) -> RenderedOverlay:
        overlay_asset = next(
            entry for entry in entries if entry.asset_id == OVERLAY_ASSET_ID
        )
        return render_transparent_overlay(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            asset=Path(overlay_asset.path),
            asset_sha256=overlay_asset.sha256,
            region=GEO.region_for("top-right"),
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=RATE_NUM,
            duration_frames=OVERLAY_SPAN[1] - OVERLAY_SPAN[0],
            output=self.bundle / "overlay-media.mov",
        )

    def _measure(
        self,
        media: Path,
        thresholds: AudioThresholds,
        input_sha: str,
        geo: tuple[int, int],
    ) -> tuple[AudioObservation, ColorObservation]:
        measure, _issues = qc_rendered_audio(
            self.ffmpeg, self.ffprobe, media, thresholds, input_sha
        )
        audio = AudioObservation(
            duration_samples=probe_duration_ms(self.ffprobe, media) * 48,
            integrated_mlufs=measure.loudness.integrated_loudness_mlufs,
            peak_mb=measure.peak_mb,
            channels=measure.probed_channels,
        )
        color = ColorObservation(
            **probe_color_metadata(self.ffprobe, media),
            region_means=probe_bar_region_means(
                self.ffmpeg, media, width=geo[0], height=geo[1]
            ),
        )
        return audio, color

    def _sides(
        self,
        tables: dict[str, object],
        renders: dict[str, Path],
        thresholds: AudioThresholds,
        derivatives: dict[str, str],
    ) -> dict[str, ParitySide]:
        sides: dict[str, ParitySide] = {}
        for side, geo in SIDE_GEOS.items():
            audio, color = self._measure(
                renders[side], thresholds, derivatives["audio_mix"], geo
            )
            sides[side] = ParitySide.model_validate(
                {
                    "side": side,
                    "audio": audio,
                    "color": color,
                    "derivatives": derivatives,
                    **tables,
                }
            )
        return sides

    def run(self) -> None:
        plan = compile_parity_fixture()
        section = self._section(plan.profile, plan.registry)
        builder = ParityTimelineBuilder(
            connection=self.connection, ffmpeg=self.ffmpeg
        )
        bars = render_bars_source(
            ffmpeg_bin=self.ffmpeg,
            width=FINAL_GEO[0],
            height=FINAL_GEO[1],
            rate_num=RATE_NUM,
            seconds=TOTAL_FRAMES // RATE_NUM,
            output=self.bundle / "bars-source.mov",
        )
        bed = builder.bed_wav(self.bundle / "bed.wav")
        mix: MixedDerivative = render_mixed_derivative(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            section=section,
            bed_media=bed,
            output=self.bundle / "program-mix.wav",
        )
        overlay = self._overlay(plan.registry.entries)
        project = builder.build(bars, mix.path, overlay.path)
        renders = {
            side: self._render(project, side, geo) for side, geo in SIDE_GEOS.items()
        }
        thresholds = audio_thresholds_from_targets(section.targets)
        derivatives = {"audio_mix": mix.sha256, "overlay": overlay.sha256}
        tables: dict[str, object] = {
            "manifest_sha256": plan.manifest.manifest_sha256,
            "profile_snapshot_sha256": plan.profile.profile_snapshot_sha256,
            "items": item_observations(plan.manifest),
            "assets": {kind: ref.sha256 for kind, ref in plan.manifest.assets.items()},
            "placements": placement_observation(plan.manifest),
            "cues": cue_observations(plan.manifest, plan.timeline_ir),
        }
        sides = self._sides(tables, renders, thresholds, derivatives)
        report = compare_parity(
            sides["preview"], sides["final"], tolerances_from_targets(section.targets)
        )
        self.passed = report.passed
        payload = {
            "passed": report.passed,
            "manifest_sha256": report.manifest_sha256,
            "derivatives": report.derivative_hashes,
            "dims_compared": list(report.dims_compared),
            "mismatches": [
                mismatch.model_dump(mode="json") for mismatch in report.mismatches
            ],
            "preview": self._side_payload(sides["preview"], renders["preview"]),
            "final": self._side_payload(sides["final"], renders["final"]),
        }
        atomic_write(
            self.bundle / REPORT_NAME,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        print(f"{MARKER} manifest sha256={report.manifest_sha256}")
        print(
            f"{MARKER} derivatives "
            + " ".join(
                f"{key}={value}" for key, value in report.derivative_hashes.items()
            )
        )
        preview_audio = sides["preview"].audio
        final_audio = sides["final"].audio
        preview_loudness = preview_audio.integrated_mlufs if preview_audio else None
        final_loudness = final_audio.integrated_mlufs if final_audio else None
        print(
            f"{MARKER} preview loudness={preview_loudness} "
            f"peak={preview_audio.peak_mb if preview_audio else None} | "
            f"final loudness={final_loudness} "
            f"peak={final_audio.peak_mb if final_audio else None}"
        )
        print(
            f"{MARKER} parity={'PASS' if report.passed else 'FAIL'} "
            f"mismatches={len(report.mismatches)}"
        )

    def _render(self, project: object, side: str, geo: tuple[int, int]) -> Path:
        return render_with_settings(
            cast("ParityRenderProjectApi", project),
            self.bundle / f"render-{side}",
            width=geo[0],
            height=geo[1],
            custom_name=f"fvp-parity-{side}",
            deadline_seconds=self.render_deadline,
        )

    def _side_payload(self, side: ParitySide, media: Path) -> dict[str, object]:
        return {
            "render_path": str(media),
            "audio": side.audio.model_dump(mode="json") if side.audio else {},
            "color": side.color.model_dump(mode="json") if side.color else {},
        }


__all__ = ["REPORT_NAME", "ParityLiveFlow"]
