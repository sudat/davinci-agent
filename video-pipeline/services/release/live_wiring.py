"""Production wiring for the live replay seams (Todo 67 / F3).

Connects the real bridge (bounded launch), the real restart machinery,
the Phase-3 parity builder and renderer over the pinned media toolchain,
and the ffprobe/ffmpeg measurement stack. Everything the flow touches
live is wired here and nowhere else.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.contracts.primitives import RationalFrameRate
from services.foundation_io import sha256_file
from services.job_runner.gate_p3_scan import repo_root
from services.presentation.audio_mix import render_mixed_derivative
from services.presentation.audio_profile import compile_audio_profile
from services.presentation.color_live_media import render_bars_source
from services.presentation.overlay_live_media import GEO, OVERLAY_SPAN
from services.presentation.overlay_render import RenderedOverlay, render_transparent_overlay
from services.presentation.parity_live_build import ParityTimelineBuilder
from services.presentation.parity_live_media import render_with_settings
from services.release.live_builds import ConnectionLike, interruptible_connection
from services.release.live_flow import LiveSeams
from services.release.live_media import (
    anchor_extraction_argv,
    extract_anchor_frames,
    measure_render_policy,
)
from services.release.live_models import (
    PROFILE_SNAPSHOT_IDS,
    ProfileBuildResult,
    RestartObservation,
)
from services.release.live_plan import frozen_ab_plan
from services.toolchain.models import load_lock

if TYPE_CHECKING:
    from services.job_runner.gate_p3_ab import AbPlan
    from services.presentation.models import ResolvedPresentationProfile
    from services.resolve_bridge.connection import ProjectApi, VersionBinding
    from services.resolve_bridge.models import ResolveHostReport

FINAL_GEO = (1920, 1080)
RATE_NUM = 30
TOTAL_FRAMES = 600
OVERLAY_KIND = "overlay"
PHASE3_LOCK = Path("config/toolchains/phase-3-v1.json")


def resolve_host_report(out: Path) -> Path:
    """RESOLVE_HOST_REPORT override, else the attempt report beside ``out``."""

    import os  # noqa: PLC0415

    override = os.environ.get("RESOLVE_HOST_REPORT")
    candidates = [Path(override)] if override else []
    candidates.append(out.parent / "resolve-host.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no resolve host report: set RESOLVE_HOST_REPORT or place resolve-host.json "
        f"beside the output dir (looked at: {[str(c) for c in candidates]})"
    )


def pinned_media_bins() -> tuple[Path, Path]:
    """The locked ffmpeg/ffprobe binaries from the frozen phase-3 toolchain."""

    lock = load_lock(repo_root() / PHASE3_LOCK)
    return Path(lock.ffmpeg.ffmpeg.path), Path(lock.ffmpeg.ffprobe.path)


@dataclass
class _Media:
    bars: Path
    mix_path: Path
    overlay_path: Path
    mix_sha256: str
    overlay_sha256: str


class _MediaKit:
    """Lazily compiled, cached per-profile build media (bars/bed/mix/overlay)."""

    def __init__(self, *, ffmpeg: Path, ffprobe: Path, media_root: Path) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._media_root = media_root
        self._cache: dict[str, _Media] = {}
        self._bars: Path | None = None

    def _bars_source(self) -> Path:
        if self._bars is None:
            self._bars = render_bars_source(
                ffmpeg_bin=self._ffmpeg,
                width=FINAL_GEO[0],
                height=FINAL_GEO[1],
                rate_num=RATE_NUM,
                seconds=TOTAL_FRAMES // RATE_NUM,
                output=self._media_root / "bars-source.mov",
            )
        return self._bars

    def media(self, snapshot_id: str, connection: ConnectionLike) -> _Media:
        cached = self._cache.get(snapshot_id)
        if cached is not None:
            return cached
        plan = frozen_ab_plan()
        work = self._media_root / snapshot_id
        work.mkdir(parents=True, exist_ok=True)
        profile = plan.profiles[snapshot_id]
        section = compile_audio_profile(
            profile,
            plan.registry,
            job_snapshot=plan.snapshots[snapshot_id],
            assets_root=Path(),
            total_frames=TOTAL_FRAMES,
            frame_rate=RationalFrameRate(num=RATE_NUM, den=1),
        )
        builder = ParityTimelineBuilder(connection=connection, ffmpeg=self._ffmpeg)  # type: ignore[arg-type]
        mix = render_mixed_derivative(
            ffmpeg_bin=self._ffmpeg,
            ffprobe_bin=self._ffprobe,
            section=section,
            bed_media=builder.bed_wav(work / "bed.wav"),
            output=work / "program-mix.wav",
        )
        overlay = cast("RenderedOverlay", self._overlay(plan, profile, snapshot_id, work))
        media = _Media(
            bars=self._bars_source(),
            mix_path=mix.path,
            overlay_path=overlay.path,
            mix_sha256=mix.sha256,
            overlay_sha256=overlay.sha256,
        )
        self._cache[snapshot_id] = media
        return media

    def _overlay(
        self,
        plan: AbPlan,
        profile: ResolvedPresentationProfile,
        snapshot_id: str,
        work: Path,
    ) -> object:
        binding = next(b for b in profile.asset_bindings if b.kind == OVERLAY_KIND)
        entry = plan.registry.entry_for(binding.asset_id)
        if entry is None:
            raise RuntimeError(f"overlay asset missing from registry: {binding.asset_id}")
        asset = Path(entry.path)
        if not asset.is_file():
            asset = repo_root() / entry.path
        if sha256_file(asset) != entry.sha256:
            raise RuntimeError(f"overlay asset bytes drifted: {asset}")
        return render_transparent_overlay(
            ffmpeg_bin=self._ffmpeg,
            ffprobe_bin=self._ffprobe,
            asset=asset,
            asset_sha256=entry.sha256,
            region=GEO.region_for(plan.manifests[snapshot_id].placement.overlay_anchor),
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=RATE_NUM,
            duration_frames=OVERLAY_SPAN[1] - OVERLAY_SPAN[0],
            output=work / "overlay-media.mov",
        )


def _binding_same(before: VersionBinding, after: VersionBinding) -> bool:
    return (
        after.product_name == before.product_name
        and after.version_core == before.version_core
        and after.build_number == before.build_number
    )


def _restart_seam(
    report: ResolveHostReport, relaunch_timeout: float
) -> Callable[[ConnectionLike], tuple[RestartObservation, ConnectionLike]]:
    def restart(
        connection: ConnectionLike,
    ) -> tuple[RestartObservation, ConnectionLike]:
        from services.resolve_bridge.connection import (  # noqa: PLC0415
            BridgeConnectionError,
            connect,
        )
        from services.spike.restart import quit_and_wait, relaunch_and_connect  # noqa: PLC0415

        before = connection.binding
        quit_and_wait()
        probe_error = ""
        try:
            connect(report)
        except (BridgeConnectionError, TypeError, AttributeError) as error:
            probe_error = f"{type(error).__name__}: {error}"
        started = time.monotonic()
        relaunched = relaunch_and_connect(report, relaunch_timeout)
        observation = RestartObservation(
            quit_method="apple-event-quit",
            down_probe_error=probe_error,
            relaunch_seconds=int(time.monotonic() - started),
            binding_same=_binding_same(before, relaunched.binding),
        )
        return observation, relaunched

    return restart


def production_seams(
    *,
    host_report_path: Path,
    ffmpeg: Path,
    ffprobe: Path,
    out_root: Path,
    render_deadline: float = 900.0,
    relaunch_timeout: float = 240.0,
) -> LiveSeams:
    """Wire every live seam to the real bridge and pinned toolchain."""

    from services.resolve_bridge.launch import launch_and_connect  # noqa: PLC0415
    from services.resolve_bridge.lifecycle import cleanup_owned_projects  # noqa: PLC0415
    from services.resolve_bridge.readiness import load_host_report  # noqa: PLC0415

    report = load_host_report(host_report_path)
    media = _MediaKit(ffmpeg=ffmpeg, ffprobe=ffprobe, media_root=out_root / "evidence" / "media")

    def injection_build(connection: ConnectionLike, interrupt_at: int | None) -> str:
        kit = media.media(PROFILE_SNAPSHOT_IDS["a"], connection)
        effective: ConnectionLike = connection
        if interrupt_at is not None:
            effective = interruptible_connection(connection, interrupt_at=interrupt_at)
        builder = ParityTimelineBuilder(connection=effective, ffmpeg=ffmpeg)
        project = builder.build(kit.bars, kit.mix_path, kit.overlay_path)
        return str(cast("ProjectApi", project).GetName())

    def profile_build(connection: ConnectionLike, profile: str, work: Path) -> ProfileBuildResult:
        snapshot = PROFILE_SNAPSHOT_IDS[profile]
        kit = media.media(snapshot, connection)
        builder = ParityTimelineBuilder(connection=connection, ffmpeg=ffmpeg)
        project = builder.build(kit.bars, kit.mix_path, kit.overlay_path)
        rendered = render_with_settings(
            project,
            work / "render",
            width=FINAL_GEO[0],
            height=FINAL_GEO[1],
            custom_name=f"fvp-f3-{snapshot}-final",
            deadline_seconds=render_deadline,
        )
        return ProfileBuildResult(
            render_path=rendered,
            derivatives={"audio_mix": kit.mix_sha256, "overlay": kit.overlay_sha256},
        )

    return LiveSeams(
        connect=lambda: launch_and_connect(report),
        restart=_restart_seam(report, relaunch_timeout),
        injection_build=injection_build,
        profile_build=profile_build,
        measure=lambda source: measure_render_policy(ffprobe, source),
        anchors=lambda source, out_dir, indices: extract_anchor_frames(
            ffmpeg, source, out_dir, indices
        ),
        anchor_argv=lambda source, out_dir, indices: anchor_extraction_argv(
            ffmpeg, source, out_dir, indices
        ),
        now=lambda: int(time.time()),
        cleanup=lambda connection: cleanup_owned_projects(
            connection.project_manager()
        ),
    )


__all__ = ["pinned_media_bins", "production_seams", "resolve_host_report"]
