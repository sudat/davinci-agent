"""Live Phase-2 gate rig: fault seams, lease, and the live connection rig.

The fault seams wrap ONLY the delegated Resolve surface the manifest faults
declare: a counting media pool interrupts the build after the placement
group containing the declared item count (the live-verified pair cadence
makes groups atomic, so the recorded ``placed_items`` is the first group
boundary at or after the declared count), and a lying ``GetRenderJobStatus``
reports the declared false-complete payload while every other call stays
truthful. All projects are owned ``__fvp_test__`` stagings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from services.build.builder_models import (
    BuilderWiring,
    BuildFailure,
    BuildInterrupted,
    RenderTiming,
)
from services.build.builder_render import PinnedBuildTools
from services.resolve_bridge.lifecycle import project_names

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.connection import ProjectManagerApi, ResolveConnection
    from services.resolve_bridge.fixed_presentation_models import FixedMediaPoolApi

BUILD_PREFIX: Final = "__fvp_test__build_"


class CountingPool:
    """Delegating pool that raises the kill seam at the declared placement."""

    def __init__(self, pool: FixedMediaPoolApi, interrupt_after_placed_items: int) -> None:
        self._pool = pool
        self._interrupt_after = interrupt_after_placed_items
        self.placed_items = 0
        self.interrupted = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._pool, name)

    def CreateEmptyTimeline(self, name: str) -> object:  # noqa: N802
        return self._pool.CreateEmptyTimeline(name)

    def ImportMedia(self, paths: list[str]) -> list[object]:  # noqa: N802
        return list(self._pool.ImportMedia(paths))

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[object]:  # noqa: N802
        added: list[object] = list(self._pool.AppendToTimeline(clip_infos))
        self.placed_items += len(clip_infos)
        if not self.interrupted and self.placed_items >= self._interrupt_after:
            self.interrupted = True
            raise BuildInterrupted(
                f"kill seam after {self.placed_items} placed items "
                f"(declared interrupt after {self._interrupt_after})"
            )
        return added


class _ProjectSurface(Protocol):
    def GetName(self) -> str: ...  # noqa: N802

    def GetMediaPool(self) -> object: ...  # noqa: N802

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...  # noqa: N802


class _ResolveSurface(Protocol):
    def GetProductName(self) -> str: ...  # noqa: N802

    def GetVersion(self) -> list[int | str]: ...  # noqa: N802

    def GetVersionString(self) -> str: ...  # noqa: N802


class FaultProject:
    """Delegating project; optionally lies about render job status."""

    def __init__(
        self,
        project: _ProjectSurface,
        *,
        lying_status: dict[str, object] | None = None,
        pool_wrapper: CountingPool | None = None,
    ) -> None:
        self._project = project
        self._lying_status = lying_status
        self._pool_wrapper = pool_wrapper
        self.status_polls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._project, name)

    def GetName(self) -> str:  # noqa: N802
        name = self._project.GetName()
        return str(name)

    def GetMediaPool(self) -> object:  # noqa: N802
        if self._pool_wrapper is not None:
            return self._pool_wrapper
        return self._project.GetMediaPool()

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]:  # noqa: N802
        if self._lying_status is not None:
            self.status_polls += 1
            return dict(self._lying_status)
        status = self._project.GetRenderJobStatus(job_id)
        if not isinstance(status, dict):
            raise BuildFailure("render-status-invalid", f"status payload malformed: {status!r}")
        return status


class FaultManager:
    """Delegating manager whose created projects carry the fault seams."""

    def __init__(
        self,
        manager: ProjectManagerApi,
        *,
        lying_status: dict[str, object] | None = None,
        interrupt_after_placed_items: int | None = None,
    ) -> None:
        self._manager = manager
        self._lying_status = lying_status
        self._interrupt_after = interrupt_after_placed_items
        self.counting_pool: CountingPool | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._manager, name)

    def CreateProject(self, project_name: str) -> object:  # noqa: N802
        created = self._manager.CreateProject(project_name)
        if created is None:
            return None
        pool_wrapper = None
        if self._interrupt_after is not None:
            pool = created.GetMediaPool()
            if pool is None:
                raise BuildFailure(
                    "staging-setup-failed", f"{project_name}: media pool unavailable"
                )
            pool_wrapper = CountingPool(
                cast("FixedMediaPoolApi", pool), self._interrupt_after
            )
            self.counting_pool = pool_wrapper
        return FaultProject(
            cast("_ProjectSurface", created),
            lying_status=self._lying_status,
            pool_wrapper=pool_wrapper,
        )


class FaultResolve:
    """Resolve wrapper exposing the fault manager."""

    def __init__(self, resolve: _ResolveSurface, manager: FaultManager) -> None:
        self._resolve = resolve
        self._manager = manager

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve, name)

    def GetProductName(self) -> str:  # noqa: N802
        return str(self._resolve.GetProductName())

    def GetVersion(self) -> list[int | str]:  # noqa: N802
        version = self._resolve.GetVersion()
        return list(version) if isinstance(version, list) else []

    def GetVersionString(self) -> str:  # noqa: N802
        return str(self._resolve.GetVersionString())

    def GetProjectManager(self) -> object:  # noqa: N802
        return self._manager


class DictRegistry:
    def __init__(self, hashes: dict[str, str]) -> None:
        self._hashes = dict(hashes)

    def expected_hash(self, artifact_id: str) -> str | None:
        return self._hashes.get(artifact_id)


@dataclass(frozen=True, slots=True)
class LiveRig:
    connection: ResolveConnection
    ffmpeg_bin: Path
    ffprobe_bin: Path
    lease_db: Path

    def tools(self) -> PinnedBuildTools:
        return PinnedBuildTools(ffmpeg_bin=self.ffmpeg_bin, ffprobe_bin=self.ffprobe_bin)

    def wiring(
        self, package: ResolvePackage, evidence_bundle: Path, *, timing: RenderTiming | None = None
    ) -> BuilderWiring:
        return BuilderWiring(
            registry=DictRegistry({package.artifact_id: package.content_hash}),
            tools=self.tools(),
            ffmpeg_bin=self.ffmpeg_bin,
            render_dir=evidence_bundle / "render",
            evidence_bundle=evidence_bundle,
            timing=timing,
        )


def owned_build_projects(connection: ResolveConnection) -> tuple[str, ...]:
    manager = connection.project_manager()
    return tuple(sorted(name for name in project_names(manager) if name.startswith(BUILD_PREFIX)))



__all__ = [
    "BUILD_PREFIX",
    "CountingPool",
    "DictRegistry",
    "FaultManager",
    "FaultProject",
    "FaultResolve",
    "LiveRig",
    "_ProjectSurface",
    "owned_build_projects",
]
