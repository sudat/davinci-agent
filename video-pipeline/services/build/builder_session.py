"""Fresh versioned staging-project lifecycle and structured evidence publication.

Every build constructs a brand-new ``__fvp_test__build_<hash>`` project and
owned timeline — nothing is ever reused, and the namespace guard inherited
from the bridge lifecycle refuses to touch anything outside the disposable
``__fvp_test__`` namespace. The BuildOutput artifact is published through
the bridge evidence recorder (hash-chained attempt/completion events) plus a
canonical ``build-output.json``.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final, cast

from services.build.builder_models import BuildFailure, BuildOutput
from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.evidence_recorder import CliRunRecord, record_cli_run
from services.resolve_bridge.lifecycle import (
    LifecycleError,
    NonOwnedResourceError,
    create_disposable_project,
    create_owned_timeline,
    delete_owned_project,
    is_owned_project,
    owned_timeline_name,
    project_names,
)

if TYPE_CHECKING:
    from pathlib import Path

    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.connection import ProjectManagerApi
    from services.resolve_bridge.fixed_presentation_models import (
        FixedProjectApi,
        FixedTimelineApi,
    )

BUILD_PROJECT_PREFIX: Final = "__fvp_test__build_"
BUILD_OUTPUT_NAME: Final = "build-output.json"
OBSERVE_MARKER: Final = "clean-build-ok:"


def staging_project_name(content_hash: str) -> str:
    return f"{BUILD_PROJECT_PREFIX}{content_hash[:12]}"


def create_staging(
    manager: ProjectManagerApi, name: str, package: ResolvePackage
) -> tuple[FixedProjectApi, FixedTimelineApi]:
    """Create the fresh versioned staging project + timeline for one build."""

    if not is_owned_project(name):
        raise NonOwnedResourceError(f"refusing non-owned project creation: {name}")
    if name in project_names(manager):
        raise BuildFailure(
            "staging-not-fresh",
            f"staging project {name} already exists; a build never reuses a timeline",
        )
    if package.timeline.frame_rate.den != 1:
        raise BuildFailure(
            "unsupported-rate",
            f"verified timeline-rate operation covers integer fps only, got "
            f"{package.timeline.frame_rate.num}/{package.timeline.frame_rate.den}",
        )
    try:
        project_api = create_disposable_project(manager, name)
    except LifecycleError as error:
        raise BuildFailure("staging-setup-failed", str(error)) from error
    project = cast("FixedProjectApi", project_api)
    try:
        _apply_project_settings(project, package)
        timeline = cast(
            "FixedTimelineApi", create_owned_timeline(project_api, owned_timeline_name())
        )
        if not timeline.SetStartTimecode(package.timeline.start_timecode):
            raise BuildFailure(
                "staging-setup-failed",
                f"SetStartTimecode({package.timeline.start_timecode}) failed",
            )
        if timeline.GetStartTimecode() != package.timeline.start_timecode:
            raise BuildFailure(
                "staging-setup-failed",
                f"timeline start timecode readback mismatch: {timeline.GetStartTimecode()}",
            )
    except LifecycleError as error:
        raise BuildFailure("staging-setup-failed", str(error)) from error
    return project, timeline


def _apply_project_settings(project: FixedProjectApi, package: ResolvePackage) -> None:
    for key, value in (
        ("timelineFrameRate", str(package.timeline.frame_rate.num)),
        ("timelineResolutionWidth", str(package.timeline.width)),
        ("timelineResolutionHeight", str(package.timeline.height)),
    ):
        if not project.SetSetting(key, value):
            raise BuildFailure("staging-setup-failed", f"SetSetting({key}) failed")
        readback = project.GetSetting(key)
        try:
            matches = Fraction(readback) == Fraction(value)
        except (ValueError, ZeroDivisionError):
            matches = readback == value
        if not matches:
            raise BuildFailure(
                "staging-setup-failed", f"{key} readback {readback!r} != {value!r}"
            )


def delete_staging(manager: ProjectManagerApi, name: str) -> None:
    """Delete the owned staging project if present; never touches non-owned names."""

    if name in project_names(manager):
        delete_owned_project(manager, name)


def publish_build_output(bundle: Path, output: BuildOutput) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    atomic_write(bundle / BUILD_OUTPUT_NAME, canonical_model_bytes(output))
    subtitle_hash = output.subtitle.output_sha256 if output.subtitle is not None else "none"
    summary = (
        f"{OBSERVE_MARKER} package={output.package_artifact_id} project={output.project_name} "
        f"timeline={output.timeline_name} fingerprint={output.timeline_fingerprint} "
        f"items={len(output.items)} render={output.render.output_sha256} "
        f"subtitle={subtitle_hash}"
    )
    record_cli_run(
        bundle,
        CliRunRecord(
            argv=["clean-builder", output.package_artifact_id],
            exit_code=0,
            expected_exit=0,
            stdout=summary,
            stderr="",
            observe=OBSERVE_MARKER,
        ),
    )


__all__ = [
    "BUILD_PROJECT_PREFIX",
    "create_staging",
    "delete_staging",
    "publish_build_output",
    "staging_project_name",
]
