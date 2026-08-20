"""LIVE A/B snapshot builds for the Phase-3 gate (Todo 62).

The SAME approved fixture Timeline IR builds and renders twice — snapshot A
then snapshot B — through the real bridge and the pinned media toolchain:
per snapshot the brand's audio profile compiles, the shared external mix
derivative and transparent overlay render from the registry assets, the
owned ``__fvp_test__`` timeline is placed with structural readback, and
both geometries (preview, final) render and get measured. Every failure is
typed and blocking — there is no manual fallback anywhere on this path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.contracts.primitives import RationalFrameRate
from services.foundation_io import atomic_write, sha256_file
from services.job_runner.gate_p3_ab import JOB_DATE, JOB_TERRITORY, AbPlan, structure_rows
from services.job_runner.gate_p3_models import (
    PARITY_REPORT_NAME,
    P3BuildObservation,
    P3PresentationHashes,
    P3QcRow,
    P3RenderBinding,
    SideName,
    SnapshotId,
)
from services.presentation.asset_registry import AssetRightsError, check_rights
from services.presentation.audio_live_media import qc_rendered_audio
from services.presentation.audio_mix import render_mixed_derivative
from services.presentation.audio_profile import (
    audio_thresholds_from_targets,
    compile_audio_profile,
)
from services.presentation.color_live_media import render_bars_source
from services.presentation.overlay_live_media import GEO, OVERLAY_SPAN
from services.presentation.overlay_render import render_transparent_overlay
from services.presentation.parity import compare_parity, tolerances_from_targets
from services.presentation.parity_live_build import ParityTimelineBuilder
from services.presentation.parity_live_fixture import (
    cue_observations,
    item_observations,
    placement_observation,
)
from services.presentation.parity_live_media import (
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
from services.resolve_bridge.lifecycle import cleanup_owned_projects

if TYPE_CHECKING:
    from services.presentation.audio_models import AudioProfileSection
    from services.qc.models import AudioThresholds
    from services.resolve_bridge.connection import ResolveConnection

MARKER: Final = "phase3-gate"
ROUTE: Final = "external-derivative"
SNAPSHOT_IDS: Final[tuple[SnapshotId, SnapshotId]] = ("p3-brand-a", "p3-brand-b")
RATE_NUM: Final = 30
TOTAL_FRAMES: Final = 600
PREVIEW_GEO: Final[tuple[int, int]] = (1280, 720)
FINAL_GEO: Final[tuple[int, int]] = (1920, 1080)
SIDE_GEOS: Final[dict[SideName, tuple[int, int]]] = {
    "preview": PREVIEW_GEO,
    "final": FINAL_GEO,
}


class P3LiveError(Exception):
    """Typed live A/B failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class P3LiveDriver:
    """Drive both snapshot builds; record every outcome honestly."""

    def __init__(  # noqa: PLR0913 (driver wiring: bridge + plan + tools + bound renders)
        self,
        *,
        connection: ResolveConnection,
        plan: AbPlan,
        builds_root: Path,
        ffmpeg: Path,
        ffprobe: Path,
        render_deadline: float,
    ) -> None:
        self.connection = connection
        self.plan = plan
        self.builds_root = builds_root
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.render_deadline = render_deadline

    def _repo_path(self, relative: str) -> Path:
        from services.job_runner.gate_p3_scan import repo_root  # noqa: PLC0415

        candidate = Path(relative)
        return candidate if candidate.is_file() else repo_root() / candidate

    def _rights_reasons(self, snapshot_id: SnapshotId) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for binding in self.plan.profiles[snapshot_id].asset_bindings:
            try:
                check_rights(
                    self.plan.registry,
                    binding.asset_id,
                    usage=binding.kind,
                    territory=JOB_TERRITORY,
                    at_job=JOB_DATE,
                )
            except AssetRightsError as error:
                reasons[binding.asset_id] = str(error.reason)
            else:
                reasons[binding.asset_id] = ""
        return reasons

    def _section(self, snapshot_id: SnapshotId) -> AudioProfileSection:
        return compile_audio_profile(
            self.plan.profiles[snapshot_id],
            self.plan.registry,
            job_snapshot=self.plan.snapshots[snapshot_id],
            assets_root=Path(),
            total_frames=TOTAL_FRAMES,
            frame_rate=RationalFrameRate(num=RATE_NUM, den=1),
        )

    def _overlay(self, snapshot_id: SnapshotId, work: Path) -> tuple[Path, str, str]:
        """Returns (rendered path, rendered sha, registry asset sha consumed)."""

        profile = self.plan.profiles[snapshot_id]
        binding = next(b for b in profile.asset_bindings if b.kind == "overlay")
        entry = self.plan.registry.entry_for(binding.asset_id)
        if entry is None:
            raise P3LiveError("stale-asset", f"overlay asset missing: {binding.asset_id}")
        asset = self._repo_path(entry.path)
        if sha256_file(asset) != entry.sha256:
            raise P3LiveError("stale-asset", f"overlay asset bytes drifted: {asset}")
        rendered = render_transparent_overlay(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            asset=asset,
            asset_sha256=entry.sha256,
            region=GEO.region_for(self.plan.manifests[snapshot_id].placement.overlay_anchor),
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=RATE_NUM,
            duration_frames=OVERLAY_SPAN[1] - OVERLAY_SPAN[0],
            output=work / "overlay-media.mov",
        )
        return rendered.path, rendered.sha256, entry.sha256

    def _measure(
        self, media: Path, geo: tuple[int, int], thresholds: AudioThresholds
    ) -> tuple[AudioObservation, ColorObservation, tuple[str, ...]]:
        measure, issues = qc_rendered_audio(self.ffmpeg, self.ffprobe, media, thresholds, "")
        audio = AudioObservation(
            duration_samples=probe_duration_ms(self.ffprobe, media) * 48,
            integrated_mlufs=measure.loudness.integrated_loudness_mlufs,
            peak_mb=measure.peak_mb,
            channels=measure.probed_channels,
        )
        color = ColorObservation(
            **probe_color_metadata(self.ffprobe, media),
            region_means=probe_bar_region_means(self.ffmpeg, media, width=geo[0], height=geo[1]),
        )
        return audio, color, tuple(str(issue) for issue in issues)

    def _drive_snapshot(self, snapshot_id: SnapshotId, bars: Path) -> P3BuildObservation:
        work = self.builds_root / snapshot_id
        work.mkdir(parents=True, exist_ok=True)
        manifest = self.plan.manifests[snapshot_id]
        section = self._section(snapshot_id)
        thresholds = audio_thresholds_from_targets(section.targets)
        builder = ParityTimelineBuilder(connection=self.connection, ffmpeg=self.ffmpeg)
        bed = builder.bed_wav(work / "bed.wav")
        mix = render_mixed_derivative(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            section=section,
            bed_media=bed,
            output=work / "program-mix.wav",
        )
        overlay_path, _overlay_rendered_sha, overlay_registry_sha = self._overlay(
            snapshot_id, work
        )
        project = cast("ParityRenderProjectApi", builder.build(bars, mix.path, overlay_path))
        renders: list[P3RenderBinding] = []
        sides: dict[SideName, ParitySide] = {}
        qc_rows: list[P3QcRow] = []
        for side, geo in SIDE_GEOS.items():
            rendered = render_with_settings(
                project,
                work / f"render-{side}",
                width=geo[0],
                height=geo[1],
                custom_name=f"fvp-p3-{snapshot_id}-{side}",
                deadline_seconds=self.render_deadline,
            )
            renders.append(
                P3RenderBinding(side=side, path=str(rendered), sha256=sha256_file(rendered))
            )
            audio, color, issues = self._measure(rendered, geo, thresholds)
            qc_rows.append(
                P3QcRow(
                    side=side,
                    integrated_mlufs=audio.integrated_mlufs,
                    peak_mb=audio.peak_mb,
                    channels=audio.channels,
                    issues=issues,
                )
            )
            sides[side] = ParitySide.model_validate(
                {
                    "side": side,
                    "manifest_sha256": manifest.manifest_sha256,
                    "profile_snapshot_sha256": manifest.profile_snapshot_sha256,
                    "items": [row.model_dump(mode="json") for row in item_observations(manifest)],
                    "assets": {kind: ref.sha256 for kind, ref in manifest.assets.items()},
                    "placements": placement_observation(manifest).model_dump(mode="json"),
                    "cues": [
                        row.model_dump(mode="json")
                        for row in cue_observations(manifest, self.plan.timeline_ir)
                    ],
                    "derivatives": {
                        "audio_mix": mix.sha256,
                        "overlay": _overlay_rendered_sha,
                    },
                    "audio": audio.model_dump(mode="json"),
                    "color": color.model_dump(mode="json"),
                }
            )
        report = compare_parity(
            sides["preview"], sides["final"], tolerances_from_targets(section.targets)
        )
        payload = {
            "passed": report.passed,
            "manifest_sha256": report.manifest_sha256,
            "derivatives": report.derivative_hashes,
            "dims_compared": list(report.dims_compared),
            "mismatches": [mismatch.model_dump(mode="json") for mismatch in report.mismatches],
            "preview": sides["preview"].model_dump(mode="json"),
            "final": sides["final"].model_dump(mode="json"),
        }
        atomic_write(
            work / PARITY_REPORT_NAME,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        print(
            f"{MARKER} build {snapshot_id} parity={'PASS' if report.passed else 'FAIL'} "
            f"qc_issues={sum(len(row.issues) for row in qc_rows)} "
            f"manifest={manifest.manifest_sha256[:12]}"
        )
        presentation = P3PresentationHashes(
            manifest_sha256=manifest.manifest_sha256,
            profile_snapshot_sha256=manifest.profile_snapshot_sha256,
            asset_sha256={kind: ref.sha256 for kind, ref in manifest.assets.items()},
        )
        return P3BuildObservation(
            snapshot_id=snapshot_id,
            work_dir=str(work),
            route=ROUTE,
            structure=structure_rows(self.plan.timeline_ir),
            presentation=presentation,
            renders=tuple(renders),
            parity_passed=report.passed,
            qc=tuple(qc_rows),
            readback_verified=True,
            used_asset_sha256={"overlay": overlay_registry_sha, "audio_mix": mix.sha256},
            rights_reasons=self._rights_reasons(snapshot_id),
        )

    def drive(self) -> tuple[P3BuildObservation, ...]:
        self.builds_root.mkdir(parents=True, exist_ok=True)
        bars = render_bars_source(
            ffmpeg_bin=self.ffmpeg,
            width=FINAL_GEO[0],
            height=FINAL_GEO[1],
            rate_num=RATE_NUM,
            seconds=TOTAL_FRAMES // RATE_NUM,
            output=self.builds_root / "bars-source.mov",
        )
        observations: list[P3BuildObservation] = []
        try:
            observations.extend(
                self._drive_snapshot(snapshot_id, bars) for snapshot_id in SNAPSHOT_IDS
            )
        finally:
            try:
                cleanup_owned_projects(self.connection.project_manager())
            except Exception as error:  # noqa: BLE001 (cleanup must not mask failures)
                print(f"{MARKER} cleanup warning: {error}")
        return tuple(observations)


__all__ = ["MARKER", "ROUTE", "SNAPSHOT_IDS", "P3LiveDriver", "P3LiveError"]
