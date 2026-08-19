"""Guarded Fusion apply for the Todo-58 live overlay driver.

The Text+ insertion and published-control writes run in a CHILD process
under a bounded watchdog (the 0A AppendToTimeline+SRT deadlock finding): a
stall is recorded as an honest timeout fallback, never a fake success.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.foundation_io import atomic_write
from services.presentation.overlay_live_media import LiveTimelineApi, OverlayLiveError
from services.resolve_bridge.connection import connect
from services.resolve_bridge.readiness import load_host_report
from services.resolve_bridge.title_probe import run_guarded_child

if TYPE_CHECKING:
    from services.resolve_bridge.title_probe_models import (
        FusionCompApi,
        FusionToolApi,
        TitleItemApi,
    )

FUSION_WATCHDOG_SECONDS: Final = 240.0
PUBLISHED_CONTROL_COUNT: Final = 3
FUSION_CHILD_MODULE: Final = "services.presentation.overlay_live"


def _fusion_child(
    report_path: Path, result_path: Path, project_name: str, timeline_name: str
) -> int:
    connection = connect(load_host_report(report_path))
    manager = connection.project_manager()
    project = manager.LoadProject(project_name)
    if project is None:
        raise OverlayLiveError(f"cannot reload owned project {project_name}")
    timeline = None
    for index in range(1, project.GetTimelineCount() + 1):
        candidate = project.GetTimelineByIndex(index)
        if candidate is not None and candidate.GetName() == timeline_name:
            timeline = candidate
            break
    if timeline is None or not project.SetCurrentTimeline(timeline):
        raise OverlayLiveError(f"cannot set owned timeline {timeline_name}")
    live_timeline = cast("LiveTimelineApi", timeline)
    item_api = live_timeline.InsertFusionTitleIntoTimeline("Text+")
    if item_api is None:
        raise OverlayLiveError("InsertFusionTitleIntoTimeline('Text+') returned None")
    item = cast("TitleItemApi", item_api)
    comp = item.GetFusionCompByIndex(1) if item.GetFusionCompCount() >= 1 else None
    if comp is None:
        raise OverlayLiveError("placed title has no fusion composition")
    tool_api = cast("FusionCompApi", comp).FindToolByID("TextPlus")
    if tool_api is None:
        raise OverlayLiveError("TextPlus tool not found in composition")
    tool = cast("FusionToolApi", tool_api)
    controls: list[dict[str, str]] = []
    for control, value in (
        ("StyledText", "Chapter One"),
        ("Size", 0.083),
        ("Center", (0.1, 0.9)),
    ):
        tool.SetInput(control, value)
        controls.append(
            {"control": control, "readback": repr(tool.GetInput(control))[:200]}
        )
    payload = {
        "placed": True,
        "record_start": int(item.GetStart(False)),
        "record_end": int(item.GetEnd(False)),
        "duration": int(item.GetDuration(False)),
        "controls": controls,
    }
    atomic_write(
        result_path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    return 0


def apply_fusion_guarded(
    *,
    host_report: Path,
    bundle: Path,
    project_name: str,
    timeline_name: str,
) -> dict[str, object]:
    """Run the fusion apply in a watchdog child; classify the outcome honestly."""

    result_path = bundle / "fusion-child-result.json"
    argv: tuple[str, ...] = (
        sys.executable,
        "-m",
        FUSION_CHILD_MODULE,
        "--child-fusion",
        "--report",
        str(host_report),
        "--result-json",
        str(result_path),
        "--project-name",
        project_name,
        "--timeline-name",
        timeline_name,
    )
    outcome = run_guarded_child(
        argv, timeout_seconds=FUSION_WATCHDOG_SECONDS, result_path=result_path
    )
    if outcome != "completed":
        return {
            "status": "timeout" if outcome == "timeout" else "fallback",
            "detail": f"guarded fusion child outcome={outcome}",
        }
    payload: object = json.loads(result_path.read_bytes())
    controls = payload.get("controls", []) if isinstance(payload, dict) else []
    verified = isinstance(controls, list) and len(controls) == PUBLISHED_CONTROL_COUNT
    return {
        "status": "verified" if verified else "fallback",
        "record": payload,
        "controls": controls,
    }


__all__ = ["apply_fusion_guarded"]
