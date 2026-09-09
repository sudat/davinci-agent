"""Live prepare canvas geometry: the project bootstrap applies the OUTPUT canvas.

The U13 measured gap: live ``prepare_project`` set fps only, so a vertical
finishing run never yielded a 1080x1920 project canvas, while
``apply_project_settings(geometry=...)`` lived in the spike path only.
Locked behaviors:

(a) landscape live prepare applies 1920x1080 + fps (fps payload
    byte-identical to the pre-geometry sequence);
(b) vertical output applies 1080x1920 + fps;
(c) an unknown output_id is a TYPED refusal before the raw transport is
    touched (never a silent landscape fallback, never a raw ValidationError);
(d) the spike path is unchanged (default landscape dims; explicit vertical
    geometry wins);
(e) the emitter bakes the resolved output_id into the prepare step params
    (default landscape keeps pre-output plans parsing).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.ir_models_v2 import PlacedClipV2, TimelineIrV2, VideoTrackV2
from services.fixtures.manifest import Phase0AFixtureManifest
from services.mcp_execution.live_adapter import LiveAdapterError, LiveMcpAdapter
from services.mcp_execution.placement_steps import prepare_step
from services.mcp_execution.plan_payloads import PrepareProjectParams
from services.mcp_execution.step_builders import CapabilityView
from services.outputs.geometry import VERTICAL_GEOMETRY
from services.resolve_bridge.fixed_presentation import apply_project_settings

TIMELINE_NAME = "ep-geom-timeline"
FPS_NUM = 30
FPS_DEN = 1
TIMELINE_START = 108000

_OK = {"success": True}


class FakeTransport:
    """Minimal fake bridge: records calls, replays canned outcomes."""

    REAL_TOOLS = frozenset(
        {"project_manager", "project_settings", "media_pool", "timeline"}
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        assert tool_name in self.REAL_TOOLS, f"logical surface leaked: {tool_name!r}"
        params = dict(normalized_params)
        self.calls.append((tool_name, action, params))
        outcome: object
        if (tool_name, action) == ("project_manager", "load"):
            outcome = {"success": False, "error": {"message": "missing", "code": "NOT_FOUND"}}
        elif (tool_name, action) == ("project_manager", "create"):
            outcome = {"name": TIMELINE_NAME, **_OK}
        elif (tool_name, action) == ("project_settings", "set_setting"):
            outcome = dict(_OK)
        elif (tool_name, action) == ("project_settings", "get_setting"):
            outcome = {"settings": "smart", **_OK}
        elif (tool_name, action) == ("media_pool", "create_timeline"):
            outcome = {"name": TIMELINE_NAME, "id": "geom-id", "created_new": True, **_OK}
        elif (tool_name, action) == ("timeline", "set_current"):
            outcome = dict(_OK)
        elif (tool_name, action) == ("timeline", "get_current"):
            outcome = {
                "name": TIMELINE_NAME,
                "id": "geom-id",
                "start_frame": TIMELINE_START,
                "end_frame": TIMELINE_START,
                "start_timecode": "01:00:00:00",
                **_OK,
            }
        else:
            raise AssertionError(f"unexpected raw call {(tool_name, action)}")
        return outcome


def _prepare_params(output_id: str | None = None) -> dict[str, object]:
    params: dict[str, object] = {
        "action": "prepare_project",
        "timeline_name": TIMELINE_NAME,
        "fps_num": FPS_NUM,
        "fps_den": FPS_DEN,
    }
    if output_id is not None:
        params["output_id"] = output_id
    return params


def _setting_calls(transport: FakeTransport) -> list[dict[str, object]]:
    return [
        call[2]
        for call in transport.calls
        if (call[0], call[1]) == ("project_settings", "set_setting")
    ]


class _SpikeProject:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def SetSetting(self, key: str, value: str) -> bool:  # noqa: N802 (vendor API name)
        self.settings[key] = value
        return True


def test_landscape_prepare_applies_frozen_canvas_plus_fps() -> None:
    transport = FakeTransport()
    adapter = LiveMcpAdapter(transport)
    adapter("prepare_project", "prepare_project", _prepare_params())

    settings = _setting_calls(transport)
    assert settings[0] == {"name": "timelineFrameRate", "value": "30"}
    assert settings[1] == {"name": "timelineResolutionWidth", "value": "1920"}
    assert settings[2] == {"name": "timelineResolutionHeight", "value": "1080"}


def test_landscape_default_parses_without_output_id() -> None:
    params = PrepareProjectParams.model_validate(_prepare_params())
    assert params.output_id == "landscape"


def test_vertical_prepare_applies_portrait_canvas_plus_fps() -> None:
    transport = FakeTransport()
    adapter = LiveMcpAdapter(transport)
    adapter("prepare_project", "prepare_project", _prepare_params("vertical"))

    settings = _setting_calls(transport)
    assert settings[0] == {"name": "timelineFrameRate", "value": "30"}
    assert settings[1] == {"name": "timelineResolutionWidth", "value": "1080"}
    assert settings[2] == {"name": "timelineResolutionHeight", "value": "1920"}


def test_unknown_output_id_refuses_typed_before_transport() -> None:
    transport = FakeTransport()
    adapter = LiveMcpAdapter(transport)
    with pytest.raises(LiveAdapterError):
        adapter("prepare_project", "prepare_project", _prepare_params("sideways"))
    assert transport.calls == []


def test_output_id_literal_rejects_garbage_at_model_boundary() -> None:
    with pytest.raises(ValidationError):
        PrepareProjectParams.model_validate(_prepare_params("sideways"))


def _minimal_ir(episode_id: str = "ep-geom") -> TimelineIrV2:
    rate = RationalFrameRate(num=30, den=1)
    clip = PlacedClipV2(
        item_id="itm-geom",
        source=SourceRef(
            source_id="src-geom",
            span=SourceFrameSpan(start_frame=0, end_frame=30, rate=rate),
        ),
        record_span=RecordFrameSpan(start_frame=0, end_frame=30),
        candidate_ref="cand-geom",
    )
    return TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id=episode_id,
        rate=rate,
        video_tracks=(VideoTrackV2(role="primary", track_id="vt-primary", items=(clip,)),),
    )


def _accepted_caps() -> CapabilityView:
    return CapabilityView({"project-timeline-creation": "accepted"}, {})


def test_prepare_step_defaults_to_landscape() -> None:
    step = prepare_step(_minimal_ir(), _accepted_caps())
    params = cast("PrepareProjectParams", step.normalized_params)
    assert params.output_id == "landscape"


def test_prepare_step_carries_vertical_output() -> None:
    step = prepare_step(_minimal_ir(), _accepted_caps(), "vertical")
    params = cast("PrepareProjectParams", step.normalized_params)
    assert params.output_id == "vertical"
    assert params.timeline_name == "ep-geom-timeline"


def test_spike_path_default_stays_landscape() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(
        Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json").read_bytes()
    )

    project = _SpikeProject()
    apply_project_settings(cast("Any", project), manifest)
    assert project.settings["timelineResolutionWidth"] == str(manifest.recipe.source.width)
    assert project.settings["timelineResolutionHeight"] == str(
        manifest.recipe.source.height
    )


def test_spike_path_explicit_vertical_geometry_wins() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(
        Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json").read_bytes()
    )

    project = _SpikeProject()
    apply_project_settings(cast("Any", project), manifest, geometry=VERTICAL_GEOMETRY)
    assert project.settings["timelineResolutionWidth"] == "1080"
    assert project.settings["timelineResolutionHeight"] == "1920"
