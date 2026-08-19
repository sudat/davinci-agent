"""The single-writer Clean Builder: promote verified bridge operations only.

One builder holds the exclusive Resolve build lease for the whole run.
Each build creates a FRESH versioned staging project (never reuses a
timeline, never touches non-owned projects), imports the package's declared
media, places every item via AppendToTimeline clipInfo at absolute record
frames, links the declared groups, read-back-verifies every item against
the package, renders to CompletionPercentage==100 with SelectAllFrames,
pairs the external mov_text subtitle, publishes the structured BuildOutput,
and disposes the staging project on every outcome — except a kill, which
leaves at most one owned orphan staging project that the next invocation
sweeps before rebuilding fresh. The builder writes NO Job State: it returns
artifacts only, and the lease/registry/tools authorities are injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.build.builder_apply import (
    apply_link_groups,
    ensure_track_layout,
    import_media,
    place_items,
    readback_verify,
)
from services.build.builder_models import (
    BuildFailure,
    BuildInterrupted,
    BuildOutput,
    LeaseHeld,
    SubtitleResult,
    readback_fingerprint,
)
from services.build.builder_recover import (
    sweep_orphan_stagings,
    verify_declared_media,
    verify_package_current,
)
from services.build.builder_render import render_timeline, run_subtitle_step
from services.build.builder_session import (
    create_staging,
    delete_staging,
    publish_build_output,
    staging_project_name,
)
from services.resolve_bridge.lifecycle import LifecycleError

if TYPE_CHECKING:
    from services.build.builder_models import BuilderWiring, BuildLease
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.connection import ProjectManagerApi, ResolveConnection


class CleanBuilder:
    """Builds a Resolve Package into a disposable staging timeline, once, verbatim."""

    def __init__(self, connection: ResolveConnection, wiring: BuilderWiring) -> None:
        self._connection = connection
        self._wiring = wiring

    def build(self, package: ResolvePackage, lease: BuildLease) -> BuildOutput:
        try:
            lease.acquire()
        except LeaseHeld as error:
            raise BuildFailure("lease-held", str(error)) from error
        try:
            return self._build_under_lease(package)
        finally:
            lease.release()

    def _build_under_lease(self, package: ResolvePackage) -> BuildOutput:
        verify_package_current(package, self._wiring.registry)
        manager = self._connection.project_manager()
        swept = sweep_orphan_stagings(manager)
        name = staging_project_name(package.content_hash)
        try:
            output = self._build_in_staging(manager, name, package, swept)
        except BuildInterrupted:
            raise
        except BaseException as error:  # failure-path cleanup, then re-raise
            self._dispose_staging(manager, name, error)
            raise
        self._dispose_staging(manager, name, None)
        if self._wiring.evidence_bundle is not None:
            try:
                publish_build_output(self._wiring.evidence_bundle, output)
            except OSError as error:
                raise BuildFailure("evidence-publish-failed", str(error)) from error
        return output

    def _build_in_staging(
        self,
        manager: ProjectManagerApi,
        name: str,
        package: ResolvePackage,
        swept: tuple[str, ...],
    ) -> BuildOutput:
        project, timeline = create_staging(manager, name, package)
        paths = verify_declared_media(package)
        pool = project.GetMediaPool()
        ensure_track_layout(timeline, package)
        media = import_media(pool, paths)
        handles = place_items(pool, package, media)
        apply_link_groups(timeline, package, handles)
        self._wiring.seams.after_place()
        rows = readback_verify(timeline, package, handles)
        self._wiring.seams.after_readback()
        render = render_timeline(
            project, package, self._wiring.render_dir, self._wiring.tools, self._wiring.timing
        )
        subtitle: SubtitleResult | None = (
            run_subtitle_step(
                self._wiring.tools,
                self._wiring.ffmpeg_bin,
                package,
                Path(render.output_path),
                self._wiring.render_dir,
            )
            if package.subtitle_step is not None
            else None
        )
        return BuildOutput(
            package_artifact_id=package.artifact_id,
            package_content_hash=package.content_hash,
            project_name=name,
            timeline_name=timeline.GetName(),
            timeline_fingerprint=readback_fingerprint(rows),
            swept_projects=swept,
            items=rows,
            render=render,
            subtitle=subtitle,
        )

    def _dispose_staging(
        self, manager: ProjectManagerApi, name: str, error: BaseException | None
    ) -> None:
        try:
            delete_staging(manager, name)
        except LifecycleError as cleanup_error:
            if error is None:
                raise BuildFailure("cleanup-failed", str(cleanup_error)) from cleanup_error
            raise cleanup_error from error


__all__ = ["CleanBuilder"]
