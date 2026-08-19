"""Restart semantics: input currency checks and orphan staging recovery.

A build re-invoked after interruption never resumes a partial timeline: the
fresh invocation first disposes every leftover owned build-namespace project
(the interrupted run can only ever have left ``__fvp_test__build_*``
projects — never a human timeline). Before touching Resolve at all, the
package must be exactly the compiled artifact of record and every declared
media file must still hash to its binding.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import BuildFailure
from services.build.builder_session import BUILD_PROJECT_PREFIX
from services.foundation_io import canonical_model_bytes, sha256_file
from services.resolve_bridge.lifecycle import (
    LifecycleError,
    delete_owned_project,
    project_names,
)

if TYPE_CHECKING:
    from services.build.builder_models import PackageRegistry
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.connection import ProjectManagerApi

ZERO_HASH: Final = "0" * 64


def verify_declared_media(package: ResolvePackage) -> dict[str, Path]:
    """Hash every declared media file before import; drift is a typed refusal."""

    paths: dict[str, Path] = {}
    for binding in package.inputs_view.declared_media:
        path = Path(binding.path)
        if not path.is_file():
            raise BuildFailure("media-missing", f"declared media missing: {path}")
        digest = sha256_file(path)
        if digest != binding.sha256:
            raise BuildFailure(
                "media-hash-drift",
                f"{binding.source_id}: on-disk sha256 {digest} != declared "
                f"{binding.sha256} (identical duration is not sufficient)",
            )
        paths[binding.source_id] = path
    for placement in package.placements:
        if placement.clip_info.media_source_id not in paths:
            raise BuildFailure(
                "media-binding-missing",
                f"{placement.item_id} needs unbound media source "
                f"{placement.clip_info.media_source_id}",
            )
    return paths


def verify_package_current(package: ResolvePackage, registry: PackageRegistry) -> None:
    """Refuse packages that are not exactly the compiled artifact of record."""

    zeroed = package.model_copy(update={"content_hash": ZERO_HASH})
    digest = hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()
    if digest != package.content_hash:
        raise BuildFailure(
            "package-hash-invalid",
            f"{package.artifact_id}: content hash chain broken "
            f"(recomputed {digest}, declared {package.content_hash})",
        )
    expected = registry.expected_hash(package.artifact_id)
    if expected is None:
        raise BuildFailure(
            "package-unknown",
            f"{package.artifact_id} is absent from the compiled package registry",
        )
    if expected != package.content_hash:
        raise BuildFailure(
            "package-stale",
            f"{package.artifact_id}: registry pins {expected} but package declares "
            f"{package.content_hash}; rebuild from the current package",
        )
    _verify_absolute_frames(package)


def _verify_absolute_frames(package: ResolvePackage) -> None:
    origin = package.timeline.frame_origin
    ceiling = origin + package.render_job.extent_frames
    for placement in package.placements:
        info = placement.clip_info
        if info.record_frame < origin:
            raise BuildFailure(
                "record-below-origin",
                f"{placement.item_id}: record frame {info.record_frame} is below the "
                f"timeline origin {origin}; clips below the origin are invisible to render",
            )
        if info.record_frame + (info.end_frame - info.start_frame) > ceiling:
            raise BuildFailure(
                "record-beyond-extent",
                f"{placement.item_id}: record span exceeds the declared render extent "
                f"{package.render_job.extent_frames} frames",
            )


def sweep_orphan_stagings(manager: ProjectManagerApi) -> tuple[str, ...]:
    """Delete every leftover owned build project; single-writer lease guarantees safety."""

    deleted: list[str] = []
    for name in sorted(project_names(manager)):
        if not name.startswith(BUILD_PROJECT_PREFIX):
            continue
        try:
            delete_owned_project(manager, name)
        except LifecycleError as error:
            raise BuildFailure(
                "orphan-sweep-failed", f"could not dispose owned staging {name}: {error}"
            ) from error
        deleted.append(name)
    return tuple(deleted)


__all__ = ["sweep_orphan_stagings", "verify_declared_media", "verify_package_current"]
