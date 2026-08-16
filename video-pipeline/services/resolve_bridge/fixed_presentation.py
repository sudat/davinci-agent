"""Spike orchestration: fixed subtitle, intro/outro, audio preset, and render.

Builds the 0A base cut in a fresh disposable timeline at timeline-absolute
frames (origin 01:00:00:00 = frame 108000, the coordinate space Resolve uses
for recordFrame, item readback, and render ranges), renders the disposable
timeline through the official render job API, and verifies the three fixed
elements: the fixed subtitle (strategy ladder with a version-bound direct-rung
deadlock registry), media-backed intro/outro identity/duration/link, and one
basic audio preset (aac / 48 kHz / stereo) checked on the rendered output.
Every run cleans up only the owned ``__fvp_test__`` project, including on
render or verification failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.resolve_bridge.base_cut import _append_pairs, _import_media, _link_pairs, read_back
from services.resolve_bridge.base_cut_compare import compare
from services.resolve_bridge.base_cut_plan import BaseCutError, ExpectedBaseCut
from services.resolve_bridge.fixed_presentation_elements import (
    audio_strategy,
    slate_rows,
    slates_all_ok,
    slates_strategy,
)
from services.resolve_bridge.fixed_presentation_models import (
    FRAME_ORIGIN,
    TIMELINE_START_TC,
    FixedPresentationMismatch,
    FixedPresentationReport,
    FixedProjectApi,
    FixedTimelineApi,
)
from services.resolve_bridge.fixed_presentation_render import compare_render, render_timeline
from services.resolve_bridge.fixed_presentation_subtitle import resolve_subtitle
from services.resolve_bridge.lifecycle import (
    cleanup_owned_projects,
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolApi,
        BaseCutRequest,
        BaseCutTimelineApi,
    )
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.fixed_presentation_render import MediaToolsApi

SLATE_MISMATCH_CODE: Final = "slate-mismatch"


class Mismatches:
    def __init__(self) -> None:
        self.rows: list[FixedPresentationMismatch] = []

    def add(self, code: str, detail: str) -> None:
        self.rows.append(FixedPresentationMismatch(code=code, detail=detail))


def shifted_request(request: BaseCutRequest, origin: int) -> BaseCutRequest:
    items = tuple(
        item.model_copy(
            update={
                "record_start": item.record_start + origin,
                "record_end": item.record_end + origin,
            }
        )
        for item in request.items
    )
    return request.model_copy(update={"items": items})


def shifted_expected(expected: ExpectedBaseCut, origin: int) -> ExpectedBaseCut:
    items = tuple(
        item.model_copy(
            update={
                "record_start": item.record_start + origin,
                "record_end": item.record_end + origin,
            }
        )
        for item in expected.items
    )
    return expected.model_copy(update={"items": items})


def ensure_fixed_layout(timeline: FixedTimelineApi) -> None:
    while timeline.GetTrackCount("video") > 1:
        if not timeline.DeleteTrack("video", timeline.GetTrackCount("video")):
            raise BaseCutError("DeleteTrack failed: video")
    while timeline.GetTrackCount("video") < 1:
        if not timeline.AddTrack("video"):
            raise BaseCutError("AddTrack failed: video")
    while timeline.GetTrackCount("audio") > 1:
        if not timeline.DeleteTrack("audio", timeline.GetTrackCount("audio")):
            raise BaseCutError("DeleteTrack failed: audio")
    while timeline.GetTrackCount("audio") < 1:
        if not timeline.AddTrack("audio", "stereo"):
            raise BaseCutError('AddTrack failed: audio "stereo"')
    while timeline.GetTrackCount("subtitle") < 1:
        if not timeline.AddTrack("subtitle"):
            raise BaseCutError("AddTrack failed: subtitle")


def apply_project_settings(project: FixedProjectApi, manifest: Phase0AFixtureManifest) -> None:
    recipe = manifest.recipe
    for key, value in (
        ("timelineFrameRate", str(recipe.source.frame_rate.num)),
        ("timelineResolutionWidth", str(recipe.source.width)),
        ("timelineResolutionHeight", str(recipe.source.height)),
    ):
        if not project.SetSetting(key, value):
            raise BaseCutError(f"SetSetting({key}) failed")


def run_fixed_presentation(
    connection: ResolveConnection,
    request: BaseCutRequest,
    expected: ExpectedBaseCut,
    manifest: Phase0AFixtureManifest,
    media: dict[str, str],
    tools: MediaToolsApi,
    render_dir: Path,
    srt_path: Path,
    *,
    direct_allowed: bool,
) -> tuple[FixedPresentationReport, str]:
    manager = connection.project_manager()
    mismatches = Mismatches()
    try:
        project_api = create_disposable_project(manager, owned_project_name())
        project = cast("FixedProjectApi", project_api)
        apply_project_settings(project, manifest)
        timeline = cast(
            "FixedTimelineApi", create_owned_timeline(project_api, owned_timeline_name())
        )
        if not timeline.SetStartTimecode(TIMELINE_START_TC):
            raise BaseCutError(f"SetStartTimecode({TIMELINE_START_TC}) failed")
        ensure_fixed_layout(timeline)
        fixed_pool = project.GetMediaPool()
        pool = cast("BaseCutMediaPoolApi", fixed_pool)
        shifted = shifted_request(request, FRAME_ORIGIN)
        media_items = _import_media(pool, shifted)
        handles = _append_pairs(pool, shifted, media_items)
        _link_pairs(cast("BaseCutTimelineApi", timeline), shifted, handles)
        snapshot = read_back(cast("BaseCutTimelineApi", timeline))
        outcome = compare(shifted_expected(expected, FRAME_ORIGIN), snapshot)
        for mismatch in outcome.mismatches:
            mismatches.add(mismatch.code, mismatch.detail)
        slates = slate_rows(snapshot, media, FRAME_ORIGIN)
        if not slates_all_ok(slates):
            mismatches.add(
                SLATE_MISMATCH_CODE,
                "; ".join(
                    f"{row.item_id}: media={row.media_ok} span={row.span_ok} link={row.link_ok}"
                    for row in slates
                ),
            )
        render = render_timeline(project, render_dir, manifest, tools)
        render_summary, render_mismatches = compare_render(render, manifest)
        for mismatch in render_mismatches:
            mismatches.add(mismatch.code, mismatch.detail)
        subtitle = resolve_subtitle(
            pool=fixed_pool,
            timeline=timeline,
            recipe=manifest.recipe.subtitle,
            rate_num=manifest.recipe.source.frame_rate.num,
            rate_den=manifest.recipe.source.frame_rate.den,
            srt_path=srt_path,
            render_output=Path(render.output_path),
            tools=tools,
            version_core=connection.binding.version_core,
            direct_allowed=direct_allowed,
            mismatches=mismatches,
        )
        passed = outcome.passed and subtitle.strategy.status == "verified" and not mismatches.rows
        report = FixedPresentationReport(
            schema_version="fixed-presentation-report-v1",
            fixture_id=manifest.fixture_id,
            strategy_table=(
                subtitle.strategy,
                slates_strategy(slates),
                audio_strategy(manifest, render_mismatches),
            ),
            base_cut_passed=outcome.passed,
            slates=slates,
            subtitle=subtitle,
            render=render,
            mismatches=tuple(mismatches.rows),
            passed=passed,
        )
        return report, render_summary
    finally:
        cleanup_owned_projects(manager)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        from services.resolve_bridge.fixed_presentation_faults import run_fault_cli  # noqa: PLC0415

        return run_fault_cli(Path(fault_fixture), arguments.manifest, arguments.fixture_dir)
    if arguments.report is None or arguments.render_dir is None:
        print("--report and --render-dir are required outside fault mode", file=sys.stderr)
        return 2
    from services.resolve_bridge.fixed_presentation_cli import run_cli  # noqa: PLC0415

    return run_cli(
        manifest_path=arguments.manifest,
        fixture_dir=arguments.fixture_dir,
        report_path=arguments.report,
        render_dir=arguments.render_dir,
        ffmpeg=arguments.ffmpeg,
        ffprobe=arguments.ffprobe,
        evidence=arguments.evidence,
    )


if __name__ == "__main__":
    raise SystemExit(main())
