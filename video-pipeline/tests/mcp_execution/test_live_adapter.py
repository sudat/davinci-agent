"""TDD live adapter: logical -> pinned Resolve MCP surfaces (V44).

Probe evidence is the only source of truth for param shapes and verification.
The pinned server previously answered ``Unknown tool: prepare_project`` because
the compiled plan emitted LOGICAL surfaces. The adapter must translate before
any raw ``tools/call`` dispatch and never fake success for unsupported rungs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, cast, get_args

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.audio_finishing import (
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    AudioOpRequestV1,
    build_audio_plan,
)
from services.creative_plan.color_finishing import (
    ColorFactsV1,
    ColorIssueV1,
    ColorPlanPolicy,
    build_color_plan,
)
from services.creative_plan.ir_models_v2 import PlacedClipV2, TimelineIrV2, VideoTrackV2
from services.mcp_execution.audio_measurement import MeasuredAudio, default_audio_measurement
from services.mcp_execution.color_measurement import default_frame_comparison
from services.mcp_execution.live_adapter import (
    SUPPORTED_SURFACES,
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveMcpAdapter,
)
from services.mcp_execution.live_handlers import (
    HANDLERS,
    audio,
    color,
    common,
    placement,
    render,
    subtitle,
)
from services.mcp_execution.live_handlers.common import LiveSessionContext
from services.mcp_execution.live_handlers.placement import (
    PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
)
from services.mcp_execution.live_handlers.subtitle_card import (
    SUBTITLE_CARD_SWITCH_TIMEOUT_SECONDS,
    SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS,
)
from services.mcp_execution.plan_models import ToolSurface
from services.mcp_execution.plan_payloads import (
    AudioStageParams,
    AudioStateReadback,
    ColorParams,
    GradeReadback,
    ImportReadback,
    PlacementReadback,
    ProjectReadback,
    SetTransformReadback,
)
from services.mcp_execution.plan_steps import _PRESET_STAGES, audio_plan_steps, color_steps
from services.mcp_execution.readback import verify_readback
from services.mcp_execution.step_builders import CapabilityView
from services.qc.tools import load_qc_tools

TIMELINE_NAME = "ep-457dfac97989568e-timeline"
FPS_NUM = 30
FPS_DEN = 1
TIMELINE_START = 108000  # probe-reported 01:00:00:00 @30fps

PROBE_CREATE_PROJECT = {"name": TIMELINE_NAME, "success": True}
PROBE_SET_FPS = {"success": True}
PROBE_GET_PERF_CACHE = {"settings": "smart", "success": True}
PROBE_CREATE_TIMELINE = {
    "name": TIMELINE_NAME,
    "id": "92c8eb30-d8dd-497e-bf65-53ed681bc133",
    "created_new": True,
    "success": True,
}
PROBE_SET_CURRENT = {"success": True}
PROBE_SET_CURRENT_MISSING = {
    "success": False,
    "error": {"message": "no timeline with that name", "code": "NOT_FOUND"},
}
PROBE_LOAD_OK = {"success": True}
PROBE_LOAD_MISSING = {
    "success": False,
    "error": {"message": "project not found", "code": "NOT_FOUND"},
}
PROBE_GET_CURRENT = {
    "name": TIMELINE_NAME,
    "id": "92c8eb30-d8dd-497e-bf65-53ed681bc133",
    "start_frame": TIMELINE_START,
    "end_frame": TIMELINE_START,
    "start_timecode": "01:00:00:00",
    "success": True,
}
PROBE_IMPORT = {
    "imported": 1,
    "clips": [
        {
            "name": "src-001.mp4",
            "id": "clip-22701fc7-51c5-4ed9-94a2-ee06beedf58a",
            "file_path": "/media/src-001.mp4",
        }
    ],
    "success": True,
}
PROBE_APPEND = {
    "count": 1,
    "items": [{"name": "src-001.mp4", "timeline_item_id": "ti-a04e0cb3"}],
    "success": True,
    "verification_status": "readback_verified",
}
PROBE_VOICE_SET = {"success": True}
PROBE_VOICE_GET = {"amount": 60, "isEnabled": True, "success": True}


class ScriptedTransport:
    """Fake raw McpTransportFn that asserts real tool names only.

    Records the per-call operation deadline (``timeout_seconds``) so tests
    can pin operation-specific transport budgets."""

    REAL_TOOLS = frozenset(
        {
            "project_manager",
            "project_settings",
            "media_pool",
            "timeline",
            "timeline_item",
            "fusion_comp",
            "resolve_control",
            "render",
            "timeline_item_color",
        }
    )

    def __init__(self, script: Mapping[tuple[str, str], list[dict[str, Any]]]) -> None:
        self._script: dict[tuple[str, str], list[dict[str, Any]]] = {
            k: list(v) for k, v in script.items()
        }
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.timeouts: list[float | None] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        if tool_name not in self.REAL_TOOLS:
            raise AssertionError(f"raw call used logical surface {tool_name!r} (must map)")
        if tool_name == "prepare_project":
            raise AssertionError("raw call must never use logical name prepare_project")
        self.calls.append((tool_name, action, dict(normalized_params)))
        self.timeouts.append(timeout_seconds)
        key = (tool_name, action)
        if key not in self._script or not self._script[key]:
            raise AssertionError(f"unexpected raw call {key} with {normalized_params}")
        queue = self._script[key]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


def _prepare_params() -> dict[str, object]:
    return {
        "action": "prepare_project",
        "timeline_name": TIMELINE_NAME,
        "fps_num": FPS_NUM,
        "fps_den": FPS_DEN,
    }


def _import_params(source_id: str = "src-001") -> dict[str, object]:
    return {"action": "import_media", "source_id": source_id}


def _place_clip_params(  # noqa: PLR0913
    *,
    item_id: str = "itm-001",
    source_id: str = "src-001",
    src_start: int = 10,
    src_end: int = 70,
    rec_start: int = 0,
    rec_end: int = 60,
    track_role: str = "primary",
    av_link_id: str | None = None,
) -> dict[str, object]:
    return {
        "action": "place_clip",
        "item_id": item_id,
        "source": {
            "source_id": source_id,
            "span": {"start_frame": src_start, "end_frame": src_end, "rate": {"num": 30, "den": 1}},
        },
        "record_span": {"start_frame": rec_start, "end_frame": rec_end},
        "track_role": track_role,
        "av_link_id": av_link_id,
    }


def _place_audio_params(  # noqa: PLR0913
    *,
    item_id: str = "aud-001",
    source_id: str = "src-002",
    src_start: int = 0,
    src_end: int = 60,
    rec_start: int = 0,
    rec_end: int = 60,
    av_link_id: str | None = None,
) -> dict[str, object]:
    return {
        "action": "place_audio",
        "item_id": item_id,
        "source": {
            "source_id": source_id,
            "span": {"start_frame": src_start, "end_frame": src_end, "rate": {"num": 30, "den": 1}},
        },
        "record_span": {"start_frame": rec_start, "end_frame": rec_end},
        "audio_role": "dialogue",
        "av_link_id": av_link_id,
    }


def _place_overlay_params(  # noqa: PLR0913
    *,
    item_id: str = "ovl-001",
    source_id: str = "src-001",
    src_start: int = 10,
    src_end: int = 70,
    rec_start: int = 0,
    rec_end: int = 60,
    track_role: str = "still",
    track_index: int | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {
        "action": "place_overlay",
        "item_id": item_id,
        "source": {
            "source_id": source_id,
            "span": {
                "start_frame": src_start,
                "end_frame": src_end,
                "rate": {"num": 30, "den": 1},
            },
        },
        "record_span": {"start_frame": rec_start, "end_frame": rec_end},
        "track_role": track_role,
    }
    if track_index is not None:
        params["track_index"] = track_index
    return params


def _voice_params() -> dict[str, object]:
    return {
        "action": "apply_voice_isolation",
        "effect_kind": "voice_isolation",
        "stage": None,
        "target_item_id": None,
        "note": "",
    }


def _source_span(start: int, end: int) -> SourceFrameSpan:
    return SourceFrameSpan(start_frame=start, end_frame=end, rate=RationalFrameRate(num=30, den=1))


def _record_span(start: int, end: int) -> RecordFrameSpan:
    return RecordFrameSpan(start_frame=start, end_frame=end)


def _get_clip_infos(call: tuple[str, str, dict[str, object]]) -> list[dict[str, object]]:
    params = call[2]
    raw = params.get("clip_infos")
    assert isinstance(raw, list)
    return [cast("dict[str, object]", item) for item in raw if isinstance(item, dict)]


def _get_paths(call: tuple[str, str, dict[str, object]]) -> list[str]:
    params = call[2]
    raw = params.get("paths")
    assert isinstance(raw, list)
    return [str(p) for p in raw]


def _append_calls(
    transport: ScriptedTransport,
) -> list[tuple[str, str, dict[str, object]]]:
    return [c for c in transport.calls if (c[0], c[1]) == ("media_pool", "append_to_timeline")]


def _make_prepared_adapter(
    script_overrides: Mapping[tuple[str, str], list[dict[str, Any]]] | None = None,
    *,
    media_frame_counts: Mapping[str, int] | None = None,
) -> tuple[LiveMcpAdapter, ScriptedTransport]:
    base: dict[tuple[str, str], list[dict[str, Any]]] = {
        ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
        ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
        ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
        ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
        ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
        ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
        ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
    }
    if script_overrides:
        base.update({k: list(v) for k, v in script_overrides.items()})
    transport = ScriptedTransport(base)
    adapter = LiveMcpAdapter(
        transport,
        media_paths={"src-001": "/media/src-001.mp4", "src-002": "/media/src-002.mp4"},
        media_frame_counts=media_frame_counts,
    )
    adapter("prepare_project", "prepare_project", _prepare_params())
    transport.calls.clear()
    return adapter, transport


# ---------------------------------------------------------------- prepare


def test_prepare_project_maps_to_real_tool_sequence_and_readback() -> None:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={"src-001": "/media/src-001.mp4"})
    result = adapter("prepare_project", "prepare_project", _prepare_params())

    assert transport.calls[0] == ("project_manager", "load", {"name": TIMELINE_NAME})
    assert transport.calls[1] == ("project_manager", "create", {"name": TIMELINE_NAME})
    assert transport.calls[2][0] == "project_settings"
    assert transport.calls[2][1] == "set_setting"
    second_params = transport.calls[2][2]
    assert second_params["name"] == "timelineFrameRate"
    assert second_params["value"] == "30"
    # DESIGN §11: the fresh project also pins perfRenderCacheMode=smart,
    # write + readback-verified (loud typed failure when refused).
    assert transport.calls[3][0] == "project_settings"
    assert transport.calls[3][1] == "set_setting"
    assert transport.calls[3][2] == {"name": "perfRenderCacheMode", "value": "smart"}
    assert transport.calls[4] == (
        "project_settings",
        "get_setting",
        {"name": "perfRenderCacheMode"},
    )
    assert transport.calls[5] == ("media_pool", "create_timeline", {"name": TIMELINE_NAME})
    assert transport.calls[6] == ("timeline", "set_current", {"name": TIMELINE_NAME})
    assert transport.calls[7][0] == "timeline"
    assert transport.calls[7][1] == "get_current"
    assert all(tool != "prepare_project" for tool, _, _ in transport.calls)
    expected = ProjectReadback(kind="project", project_name=TIMELINE_NAME, timeline_frame_rate="30")
    verification = verify_readback(expected, result)
    assert verification.matched, verification.mismatches


def test_prepare_project_preserves_coordinate_start() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    result = adapter("append_to_timeline", "place_clip", _place_clip_params())
    assert transport.calls[0][0] == "timeline"
    assert transport.calls[0][1] == "get_items_in_track"
    clip_infos = _get_clip_infos(_append_calls(transport)[0])
    assert clip_infos[0]["record_frame"] == TIMELINE_START
    assert clip_infos[0]["record_frame_mode"] == "absolute"
    assert clip_infos[0]["start_frame"] == 10
    assert clip_infos[0]["end_frame"] == 70
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-001",
        source_span=_source_span(10, 70),
        record_span=_record_span(0, 60),
    )
    assert verify_readback(expected, result).matched


# ---- Task 5 repair 9: placement reconciliation is BOUNDED — per-track scan
# + targeted source-frame readback. The whole-timeline source_range_report
# (294 occurrences on the representative timeline) stopped completing even
# at a 900 s deadline, so placement must never issue it again.


def _row(item_id: str, abs_start: int, abs_end: int) -> dict[str, object]:
    """One ``get_items_in_track`` row: ABSOLUTE record start/end (end
    exclusive) — record spans only; source spans arrive from the targeted
    ``timeline_item.get_source_*_frame`` readback, never from this scan."""
    return {
        "name": f"clip-{item_id}",
        "id": f"ti-{item_id}",
        "start": abs_start,
        "end": abs_end,
        "duration": abs_end - abs_start,
    }


def _rows_for(
    items: tuple[tuple[str, tuple[int, int], tuple[int, int]], ...],
) -> list[dict[str, object]]:
    return [
        _row(item_id, TIMELINE_START + rec[0], TIMELINE_START + rec[1])
        for item_id, _src, rec in items
    ]


def _track_listing(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"items": [dict(row) for row in rows]}


def _frame(value: int) -> dict[str, object]:
    return {"frame": value}


def _frame_queues(
    items: tuple[tuple[str, tuple[int, int], tuple[int, int]], ...],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Per-item (start, end) frame readbacks in row order."""
    starts = [_frame(src[0]) for _item_id, src, _rec in items]
    ends = [_frame(src[1]) for _item_id, src, _rec in items]
    return starts, ends


_IDEMPOTENT_SPANS: tuple[tuple[str, tuple[int, int], tuple[int, int]], ...] = (
    ("itm-001", (10, 70), (0, 60)),
    ("itm-002", (80, 110), (60, 90)),
    ("itm-003", (120, 150), (90, 120)),
)


def _placement_params(
    item_id: str, src: tuple[int, int], rec: tuple[int, int]
) -> dict[str, object]:
    return _place_clip_params(
        item_id=item_id, src_start=src[0], src_end=src[1], rec_start=rec[0], rec_end=rec[1]
    )


def test_idempotent_placements_share_one_bounded_track_scan() -> None:
    """An idempotent rerun of an already-built timeline must not walk the
    whole timeline per placement step: ONE bounded per-track scan is session
    evidence for every placement, each item's source span verified by the
    targeted per-item frame readback, zero appends."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS)))
    ]
    starts, ends = _frame_queues(_IDEMPOTENT_SPANS)
    transport._script[("timeline_item", "get_source_start_frame")] = starts
    transport._script[("timeline_item", "get_source_end_frame")] = ends
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]

    for item_id, src, rec in _IDEMPOTENT_SPANS:
        result = adapter("append_to_timeline", "place_clip", _placement_params(item_id, src, rec))
        expected = PlacementReadback(
            kind="placement",
            item_id=item_id,
            source_span=_source_span(src[0], src[1]),
            record_span=_record_span(rec[0], rec[1]),
        )
        assert verify_readback(expected, result).matched

    scan_calls = [c for c in transport.calls if c[1] == "get_items_in_track"]
    assert len(scan_calls) == 1
    assert _append_calls(transport) == []
    frame_calls = [c for c in transport.calls if c[0] == "timeline_item"]
    assert len(frame_calls) == 2 * len(_IDEMPOTENT_SPANS)


def test_placement_reconciliation_never_issues_the_whole_timeline_scan() -> None:
    """Regression (six-hour finishing run): the whole-timeline
    source_range_report — 294 occurrences on the representative timeline,
    which stopped completing even at a 900 s deadline — must never be
    issued by placement reconciliation, on any path (presence check,
    append verify, idempotent rerun)."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]

    adapter("append_to_timeline", "place_clip", _placement_params(*_IDEMPOTENT_SPANS[0]))
    adapter("append_to_timeline", "place_clip", _placement_params(*_IDEMPOTENT_SPANS[0]))

    raw_actions = {(tool, action) for tool, action, _ in transport.calls}
    assert ("timeline", "source_range_report") not in raw_actions
    assert len(_append_calls(transport)) == 1


def test_missing_placement_appends_once_and_refreshes_the_snapshot() -> None:
    """A genuinely missing placement still appends and re-verifies with a
    FRESH post-mutation bounded scan; that verified scan then serves later
    placements — a rerun of the same item costs no scan and no append."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    first = _IDEMPOTENT_SPANS[0]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]

    first_result = cast(
        "dict[str, object]",
        adapter("append_to_timeline", "place_clip", _placement_params(*first)),
    )
    assert first_result["item_id"] == first[0]

    second_result = cast(
        "dict[str, object]",
        adapter("append_to_timeline", "place_clip", _placement_params(*first)),
    )
    assert second_result["item_id"] == first[0]

    scan_calls = [c for c in transport.calls if c[1] == "get_items_in_track"]
    assert len(scan_calls) == 2
    assert len(_append_calls(transport)) == 1


def test_session_mutation_invalidates_the_placement_snapshot() -> None:
    """Any other content mutation (voice isolation here) drops the cached
    per-track scan — the next placement re-scans the track rather than
    trusting a snapshot that predates the mutation."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport._script[("timeline", "set_voice_isolation_state")] = [dict(PROBE_VOICE_SET)]
    # Preflight get is non-desired so the set fires and mutates the timeline;
    # the post-set get shows the applied state.
    transport._script[("timeline", "get_voice_isolation_state")] = [
        {"success": True, "isEnabled": False, "amount": 0},
        dict(PROBE_VOICE_GET),
    ]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    transport.calls.clear()

    first = _IDEMPOTENT_SPANS[0]
    adapter("append_to_timeline", "place_clip", _placement_params(*first))
    adapter("set_voice_isolation_state", "apply_voice_isolation", _voice_params())
    adapter("append_to_timeline", "place_clip", _placement_params(*first))

    scan_calls = [c for c in transport.calls if c[1] == "get_items_in_track"]
    assert len(scan_calls) == 2


def test_overlay_over_base_places_track_two_at_the_same_record_span() -> None:
    """The measured v44-real-01 blocker: an external transparent overlay must
    stack OVER a same-span base clip on video track 2. The base's verified
    placement on track 1 must never satisfy the overlay's presence check
    (dedupe is scoped to track type + track index), and an idempotent rerun
    of the overlay places nothing twice."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    span = _IDEMPOTENT_SPANS[0]
    # scan order: V1 pre (empty), V1 post (base row), V2 pre (empty), V2 post
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
        dict(_track_listing([])),
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1]))),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [
        _frame(10),
        _frame(10),
        _frame(10),
    ]
    transport._script[("timeline_item", "get_source_end_frame")] = [
        _frame(70),
        _frame(70),
        _frame(70),
    ]
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]

    adapter("append_to_timeline", "place_clip", _placement_params(*span))
    overlay_request = _place_overlay_params(
        item_id="ovl-001",
        src_start=span[1][0],
        src_end=span[1][1],
        rec_start=span[2][0],
        rec_end=span[2][1],
        track_index=2,
    )
    result = cast(
        "dict[str, object]",
        adapter("append_to_timeline", "place_overlay", overlay_request),
    )
    rerun = cast(
        "dict[str, object]",
        adapter("append_to_timeline", "place_overlay", overlay_request),
    )

    assert len(_append_calls(transport)) == 2
    overlay_append = _get_clip_infos(_append_calls(transport)[1])[0]
    assert overlay_append["track_index"] == 2
    assert result["track_index"] == 2
    assert rerun["track_index"] == 2
    scans = [c for c in transport.calls if c[1] == "get_items_in_track"]
    assert [c[2]["track_index"] for c in scans] == [1, 1, 2, 2]
    expected = PlacementReadback(
        kind="placement",
        item_id="ovl-001",
        source_span=_source_span(span[1][0], span[1][1]),
        record_span=_record_span(span[2][0], span[2][1]),
        track_index=2,
    )
    assert verify_readback(expected, result).matched


def test_legacy_overlay_request_defaults_to_track_one() -> None:
    """Stored plans predate track_index: a place_overlay without it parses,
    places on track 1, and its default-1 readback still verifies."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    span = _IDEMPOTENT_SPANS[0]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS[:1])))
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(span[1][0])]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(span[1][1])]

    result = cast(
        "dict[str, object]",
        adapter(
            "append_to_timeline",
            "place_overlay",
            _place_overlay_params(
                item_id="ovl-001",
                src_start=span[1][0],
                src_end=span[1][1],
                rec_start=span[2][0],
                rec_end=span[2][1],
            ),
        ),
    )

    assert _append_calls(transport) == []
    assert result["track_index"] == 1
    scans = [c for c in transport.calls if c[1] == "get_items_in_track"]
    assert all(c[2]["track_index"] == 1 for c in scans)
    expected = PlacementReadback(
        kind="placement",
        item_id="ovl-001",
        source_span=_source_span(span[1][0], span[1][1]),
        record_span=_record_span(span[2][0], span[2][1]),
    )
    assert verify_readback(expected, result).matched


# ---- Task 5 repair 9: the bounded placement readback carries its own
# measured per-track deadline (whole-timeline walks no longer complete at
# ANY deadline on the representative timeline; per-track scans measured
# 35.1 s / 97 items and 67.8 s / 100 items).


def test_placement_readback_carries_the_measured_deadline_and_bounded_params() -> None:
    """The per-track scan and both targeted source-frame readbacks must
    pass the named measured deadline; every other vendor call keeps the
    transport default (timeout None)."""
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    transport.timeouts.clear()
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for(_IDEMPOTENT_SPANS)))
    ]
    starts, ends = _frame_queues(_IDEMPOTENT_SPANS)
    transport._script[("timeline_item", "get_source_start_frame")] = starts
    transport._script[("timeline_item", "get_source_end_frame")] = ends

    adapter("append_to_timeline", "place_clip", _placement_params(*_IDEMPOTENT_SPANS[0]))

    bounded = {"get_items_in_track", "get_source_start_frame", "get_source_end_frame"}
    scan_rows = [
        (call, timeout)
        for call, timeout in zip(transport.calls, transport.timeouts, strict=True)
        if call[1] in bounded
    ]
    assert len(scan_rows) == 3
    scan_call, scan_timeout = scan_rows[0]
    assert scan_call[2] == {"track_type": "video", "track_index": 1}
    assert scan_timeout == PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS
    frame_addr = {"track_type": "video", "track_index": 1, "item_index": 0}
    assert scan_rows[1][0][2] == frame_addr
    assert scan_rows[2][0][2] == frame_addr
    assert {timeout for _call, timeout in scan_rows} == {PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS}
    other_timeouts = [
        timeout
        for call, timeout in zip(transport.calls, transport.timeouts, strict=True)
        if call[1] not in bounded
    ]
    assert set(other_timeouts) <= {None}


# ---------------------------------------------------------------- resume


def test_prepare_loads_existing_project_and_timeline_without_creating() -> None:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_OK)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    result = adapter("prepare_project", "prepare_project", _prepare_params())

    assert transport.calls[0] == ("project_manager", "load", {"name": TIMELINE_NAME})
    actions = {(tool, action) for tool, action, _ in transport.calls}
    assert ("project_manager", "create") not in actions
    assert ("project_settings", "set_setting") not in actions
    assert ("media_pool", "create_timeline") not in actions
    assert ("timeline", "set_current") in actions
    expected = ProjectReadback(kind="project", project_name=TIMELINE_NAME, timeline_frame_rate="30")
    assert verify_readback(expected, result).matched
    assert isinstance(result, dict)
    assert result["start_frame"] == TIMELINE_START


def test_prepare_resume_creates_timeline_when_project_exists_without_one() -> None:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_OK)],
            ("timeline", "set_current"): [
                dict(PROBE_SET_CURRENT_MISSING),
                dict(PROBE_SET_CURRENT),
            ],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    adapter("prepare_project", "prepare_project", _prepare_params())

    actions = [(tool, action) for tool, action, _ in transport.calls]
    assert actions == [
        ("project_manager", "load"),
        ("timeline", "set_current"),
        ("media_pool", "create_timeline"),
        ("timeline", "set_current"),
        ("timeline", "get_current"),
    ]


def test_prepare_load_missing_project_falls_back_to_create_sequence() -> None:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    adapter("prepare_project", "prepare_project", _prepare_params())

    actions = [(tool, action) for tool, action, _ in transport.calls]
    assert actions == [
        ("project_manager", "load"),
        ("project_manager", "create"),
        ("project_settings", "set_setting"),
        ("project_settings", "set_setting"),
        ("project_settings", "get_setting"),
        ("media_pool", "create_timeline"),
        ("timeline", "set_current"),
        ("timeline", "get_current"),
    ]


def test_prepare_refused_perf_cache_mode_is_a_loud_typed_failure() -> None:
    # Given: a fresh project whose perfRenderCacheMode write reads back
    #        anything other than "smart" (DESIGN §11: never silent)
    # When: preparing the project
    # Then: typed perf-cache-mode-refused — the create path never proceeds
    #       to timeline construction with caching silently unpinned
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [
                {"settings": "none", "success": True}
            ],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("prepare_project", "prepare_project", _prepare_params())
    assert excinfo.value.code == "perf-cache-mode-refused"
    # And: no timeline was created after the refused pin
    assert ("media_pool", "create_timeline") not in {
        (tool, action) for tool, action, _ in transport.calls
    }


def test_prepare_load_malformed_response_stays_loud() -> None:
    transport = ScriptedTransport({("project_manager", "load"): [{"success": "yes"}]})
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(ValidationError):
        adapter("prepare_project", "prepare_project", _prepare_params())


# ---------------------------------------------------------------- import


def test_import_maps_to_safe_import_media_with_probe_shape() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    result = adapter("safe_import_media", "import_media", _import_params("src-001"))
    assert transport.calls[0][0] == "media_pool"
    assert transport.calls[0][1] == "safe_import_media"
    paths = _get_paths(transport.calls[0])
    assert "/media/src-001.mp4" in paths
    expected = ImportReadback(kind="import", source_id="src-001")
    assert verify_readback(expected, result).matched


# ---------------------------------------------------------------- placements


def test_place_audio_uses_media_type_two_and_verifies_via_bounded_track_readback() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [
        {
            "imported": 1,
            "clips": [{"name": "src-002.mp4", "id": "clip-002", "file_path": "/media/src-002.mp4"}],
            "success": True,
        }
    ]
    adapter("safe_import_media", "import_media", _import_params("src-002"))
    transport.calls.clear()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("aud-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(0)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(60)]
    adapter("append_to_timeline", "place_audio", _place_audio_params())
    clip_infos = _get_clip_infos(_append_calls(transport)[0])
    assert clip_infos[0]["media_type"] == 2
    assert clip_infos[0]["track_index"] == 1
    audio_scans = [
        c for c in transport.calls if c[1] == "get_items_in_track" and c[2]["track_type"] == "audio"
    ]
    assert len(audio_scans) == 2


def test_place_audio_does_not_treat_matching_video_occurrence_as_existing_audio() -> None:
    """Track-type strictness is structural now: the presence scan is
    addressed BY track type, so a video item sharing the audio placement's
    exact record+source span is invisible to the audio reconciliation and
    the append still fires."""
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("aud-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]

    adapter(
        "append_to_timeline",
        "place_audio",
        _place_audio_params(source_id="src-001", src_start=10, src_end=70),
    )

    append_calls = _append_calls(transport)
    assert len(append_calls) == 1
    assert _get_clip_infos(append_calls[0])[0]["media_type"] == 2


def test_place_fails_when_bounded_readback_mismatch_never_fake_success() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([])),
    ]
    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _place_clip_params())


def _imported_clip_adapter() -> tuple[LiveMcpAdapter, ScriptedTransport]:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    return adapter, transport


def test_place_clip_skips_append_when_placement_exists_with_one_frame_source_variance() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)]))
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(11)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    result = adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert _append_calls(transport) == []
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-001",
        source_span=_source_span(10, 70),
        record_span=_record_span(0, 60),
    )
    assert verify_readback(expected, result).matched


def test_second_place_invocation_rescans_nothing_and_reverifies_source_only() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)]))
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(71)]
    adapter("append_to_timeline", "place_clip", _place_clip_params())
    transport.calls.clear()

    adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert _append_calls(transport) == []
    assert [c[1] for c in transport.calls] == [
        "get_source_start_frame",
        "get_source_end_frame",
    ]


def test_place_clip_probe_with_two_frame_source_difference_still_appends() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(12), _frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70), _frame(70)]
    adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert len(_append_calls(transport)) == 1


# ---- Task 8 repair: source-EOF-aware END reconciliation -------------------
# Measured Resolve 21.0.4.5 (2026-08-29; full runs r3/r4 + fresh-session
# targeted readbacks on v44-real-01): an item whose committed source span ends
# exactly at the imported media's frame EOF (ffprobe nb_frames=8467) reads its
# source END back two frames short ([8388,8465) vs committed [8388,8467)) on
# BOTH video and audio tracks while interior items stay exact; the same
# verification passed within ±1 on 2026-08-27 with no placement mutation.
# The two-frame END shortfall is reconciled ONLY when the committed end equals
# an independently established media EOF and only for exactly that drift.

_EOF_ITEM = ("itm-001", (8388, 8467), (0, 79))


def _eof_adapter(
    media_frame_counts: Mapping[str, int] | None,
) -> tuple[LiveMcpAdapter, ScriptedTransport]:
    adapter, transport = _make_prepared_adapter(media_frame_counts=media_frame_counts)
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    return adapter, transport


def _eof_scripts(transport: ScriptedTransport, *, end_frame: int) -> None:
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for((_EOF_ITEM,)))),
        dict(_track_listing(_rows_for((_EOF_ITEM,)))),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(8388)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(end_frame)]


def test_eof_source_end_two_frame_shortfall_reconciles_without_append() -> None:
    """The exact r3/r4 shape: committed [8388,8467), readback [8388,8465),
    trusted media EOF 8467 — present, so the false-negative must never reach
    append_to_timeline (both full runs died on the duplicate append)."""

    adapter, transport = _eof_adapter({"src-001": 8467})
    _eof_scripts(transport, end_frame=8465)

    result = adapter(
        "append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM)
    )

    assert _append_calls(transport) == []
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-001",
        source_span=_source_span(8388, 8467),
        record_span=_record_span(0, 79),
    )
    assert verify_readback(expected, result).matched


def test_eof_source_end_shortfall_refused_when_end_is_not_media_eof() -> None:
    adapter, transport = _eof_adapter({"src-001": 9000})
    _eof_scripts(transport, end_frame=8465)

    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM))

    assert len(_append_calls(transport)) == 1


def test_eof_source_end_shortfall_refused_when_eof_unknown() -> None:
    adapter, transport = _eof_adapter(None)
    _eof_scripts(transport, end_frame=8465)

    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM))

    assert len(_append_calls(transport)) == 1


def test_eof_source_end_shortfall_refused_beyond_two_frames() -> None:
    adapter, transport = _eof_adapter({"src-001": 8467})
    _eof_scripts(transport, end_frame=8464)

    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM))

    assert len(_append_calls(transport)) == 1


def test_eof_item_start_drift_beyond_one_frame_still_refused() -> None:
    adapter, transport = _eof_adapter({"src-001": 8467})
    _eof_scripts(transport, end_frame=8465)
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(8386)]

    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM))

    assert len(_append_calls(transport)) == 1


def test_eof_reconciliation_keeps_record_span_exact() -> None:
    adapter, transport = _eof_adapter({"src-001": 8467})
    _eof_scripts(transport, end_frame=8465)
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing(_rows_for((("itm-001", (8388, 8467), (1, 80)),)))),
        dict(_track_listing(_rows_for((("itm-001", (8388, 8467), (1, 80)),)))),
    ]

    with pytest.raises(LiveAdapterError):
        adapter("append_to_timeline", "place_clip", _placement_params(*_EOF_ITEM))

    assert len(_append_calls(transport)) == 1
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([_row("itm-001", TIMELINE_START + 1, TIMELINE_START + 61)])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert len(_append_calls(transport)) == 1


def test_place_clip_post_append_verification_accepts_one_frame_source_variance() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(9)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(71)]
    result = adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert len(_append_calls(transport)) == 1
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-001",
        source_span=_source_span(10, 70),
        record_span=_record_span(0, 60),
    )
    assert verify_readback(expected, result).matched


def test_verify_readback_set_transform_compares_committed_fields() -> None:
    expected = SetTransformReadback(
        kind="set_transform", track_index=1, item_index=0, rotation_angle=90.0
    )
    result = {
        "track_index": 1,
        "item_index": 0,
        "rotation_angle": 90.0,
        "applied": True,
    }
    verification = verify_readback(expected, result)
    assert verification.matched, verification.mismatches

    drifted = dict(result, rotation_angle=-90.0)
    rejected = verify_readback(expected, drifted)
    assert not rejected.matched
    assert any("rotation_angle" in mismatch for mismatch in rejected.mismatches)


def test_place_clip_post_append_verification_rejects_two_frame_source_difference() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(12)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert excinfo.value.code == "placement-readback-mismatch"


def test_place_clip_post_append_verification_rejects_record_difference() -> None:
    adapter, transport = _imported_clip_adapter()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("itm-001", TIMELINE_START + 1, TIMELINE_START + 61)])),
    ]
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("append_to_timeline", "place_clip", _place_clip_params())

    assert excinfo.value.code == "placement-readback-mismatch"


def test_place_with_av_link_id_succeeds_with_separate_dispatch() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [dict(PROBE_IMPORT)]
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    transport.calls.clear()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(10)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(70)]
    params = _place_clip_params(av_link_id="av3")
    result = adapter("append_to_timeline", "place_clip", params)
    append_calls = _append_calls(transport)
    assert len(append_calls) == 1
    clip_infos = _get_clip_infos(append_calls[0])
    assert clip_infos[0]["media_type"] == 1
    assert "av_link_id" not in clip_infos[0]
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-001",
        source_span=_source_span(10, 70),
        record_span=_record_span(0, 60),
    )
    assert verify_readback(expected, result).matched


def test_place_audio_with_av_link_id_succeeds() -> None:
    adapter, transport = _make_prepared_adapter()
    transport._script[("media_pool", "safe_import_media")] = [
        {
            "imported": 1,
            "clips": [{"name": "src-002.mp4", "id": "clip-002", "file_path": "/media/src-002.mp4"}],
            "success": True,
        }
    ]
    adapter("safe_import_media", "import_media", _import_params("src-002"))
    transport.calls.clear()
    transport._script[("media_pool", "append_to_timeline")] = [dict(PROBE_APPEND)]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(_track_listing([])),
        dict(_track_listing([_row("aud-001", TIMELINE_START, TIMELINE_START + 60)])),
    ]
    transport._script[("timeline_item", "get_source_start_frame")] = [_frame(0)]
    transport._script[("timeline_item", "get_source_end_frame")] = [_frame(60)]
    params = _place_audio_params(av_link_id="av3")
    result = adapter("append_to_timeline", "place_audio", params)
    clip_infos = _get_clip_infos(_append_calls(transport)[0])
    assert clip_infos[0]["media_type"] == 2
    assert "av_link_id" not in clip_infos[0]
    assert isinstance(result, dict)
    assert result.get("item_id") == "aud-001"


# ---------------------------------------------------------------- voice isolation


def test_voice_isolation_maps_to_set_then_get() -> None:
    adapter, transport = _make_prepared_adapter()
    # Preflight get is non-desired so the set fires; post-set get then succeeds.
    transport._script[("timeline", "get_voice_isolation_state")] = [
        {"success": True, "isEnabled": False, "amount": 0},
        dict(PROBE_VOICE_GET),
    ]
    transport._script[("timeline", "set_voice_isolation_state")] = [dict(PROBE_VOICE_SET)]
    result = adapter("set_voice_isolation_state", "apply_voice_isolation", _voice_params())
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert transport.calls[1] == (
        "timeline",
        "set_voice_isolation_state",
        {"state": {"isEnabled": True, "amount": 60}, "track_index": 1},
    )
    assert transport.calls[2][0] == "timeline"
    assert transport.calls[2][1] == "get_voice_isolation_state"
    expected = AudioStateReadback(
        kind="audio_state", item_ref="timeline", state_property="voice_isolation"
    )
    assert verify_readback(expected, result).matched


def test_voice_isolation_timeout_after_success_reconciles_and_counts_once() -> None:
    """Timeout on set_voice_isolation_state after the mutation succeeded
    must be reconciled via an independent get_voice_isolation_state readback;
    the mutation must not be sent twice."""

    from services.mcp_client.errors import McpTimeoutError  # noqa: PLC0415

    class CountingTimeoutTransport:
        def __init__(self) -> None:
            self.set_calls = 0
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.get_calls = 0

        def __call__(
            self,
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            self.calls.append((tool_name, action, dict(normalized_params)))
            if (tool_name, action) == ("timeline", "set_voice_isolation_state"):
                self.set_calls += 1
                raise McpTimeoutError("timeline", 5.0)
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                self.get_calls += 1
                # Preflight get (first) not desired → set; reconciliation (second) desired
                if self.get_calls == 1:
                    return {"success": True, "isEnabled": False, "amount": 0}
                return {"success": True, "isEnabled": True, "amount": 60}
            raise AssertionError(f"unexpected call {(tool_name, action)}")

    transport = CountingTimeoutTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    ctx.timeline_start = 0
    ctx.current_timeline_name = "ep-test-timeline"
    ctx.current_timeline_id = "id-1"
    # Directly invoke handler to isolate timeout-reconciliation behavior
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        set_voice_isolation_state,
    )

    result = set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert transport.set_calls == 1, "mutation must be sent exactly once"
    assert ctx.timeline_mutated is True
    expected = AudioStateReadback(
        kind="audio_state", item_ref="timeline", state_property="voice_isolation"
    )
    assert verify_readback(expected, result).matched


def test_voice_isolation_timeout_with_mismatch_preserves_failure() -> None:
    """Timeout plus a mismatched readback must not be fabricated as success."""

    from services.mcp_client.errors import McpTimeoutError  # noqa: PLC0415

    class MismatchTransport:
        def __init__(self) -> None:
            self.get_calls = 0

        def __call__(
            self,
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            if (tool_name, action) == ("timeline", "set_voice_isolation_state"):
                raise McpTimeoutError("timeline", 5.0)
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                self.get_calls += 1
                if self.get_calls == 1:
                    return {"success": True, "isEnabled": False, "amount": 0}
                return {"success": True, "isEnabled": False, "amount": 0}
            raise AssertionError(f"unexpected call {(tool_name, action)}")

    transport = MismatchTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    ctx.timeline_start = 0
    ctx.current_timeline_name = "ep-test-timeline"
    ctx.current_timeline_id = "id-1"
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        set_voice_isolation_state,
    )

    with pytest.raises(LiveAdapterError) as exc:
        set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert "voice-readback-mismatch" in str(exc.value) or "mismatch" in str(exc.value).lower()
    assert ctx.timeline_mutated is False


def test_voice_isolation_forwards_operation_timeout_on_set_and_get() -> None:
    """Both set and get must use the operation-specific 120 s deadline."""

    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        VOICE_ISOLATION_TIMEOUT_SECONDS,
    )

    adapter, transport = _make_prepared_adapter()
    transport._script[("timeline", "get_voice_isolation_state")] = [
        {"success": True, "isEnabled": False, "amount": 0},
        dict(PROBE_VOICE_GET),
    ]
    transport._script[("timeline", "set_voice_isolation_state")] = [dict(PROBE_VOICE_SET)]
    transport.calls.clear()
    transport.timeouts.clear()
    adapter("set_voice_isolation_state", "apply_voice_isolation", _voice_params())
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert transport.calls[1][1] == "set_voice_isolation_state"
    assert transport.calls[2][1] == "get_voice_isolation_state"
    assert transport.timeouts[0] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert transport.timeouts[1] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert transport.timeouts[2] == VOICE_ISOLATION_TIMEOUT_SECONDS


def test_voice_isolation_preflight_reuses_existing_desired_state() -> None:
    """Preflight get already at enabled/60 → return with no set, not mutated."""

    class PreflightDesiredTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.timeouts: list[float | None] = []

        def __call__(
            self,
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            self.calls.append((tool_name, action, dict(normalized_params)))
            self.timeouts.append(timeout_seconds)
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                return {"success": True, "isEnabled": True, "amount": 60}
            raise AssertionError(
                f"unexpected call {(tool_name, action)} - set should not be called"
            )

    transport = PreflightDesiredTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    ctx.timeline_start = 0
    ctx.current_timeline_name = "ep-test-timeline"
    ctx.current_timeline_id = "id-1"
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        VOICE_ISOLATION_TIMEOUT_SECONDS,
        set_voice_isolation_state,
    )

    result = set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert transport.timeouts[0] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert result["is_enabled"] is True
    assert result["amount"] == 60
    assert ctx.timeline_mutated is False


def test_voice_isolation_timeout_reconciliation_forwards_timeout_on_get() -> None:
    """After a set timeout, the reconciliation get must also use the 120 s deadline."""

    from services.mcp_client.errors import McpTimeoutError  # noqa: PLC0415
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        VOICE_ISOLATION_TIMEOUT_SECONDS,
    )

    class TimeoutCountingTransport:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.get_calls = 0

        def __call__(
            self,
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            self.calls.append((tool_name, action, dict(normalized_params)))
            self.timeouts.append(timeout_seconds)
            if (tool_name, action) == ("timeline", "set_voice_isolation_state"):
                raise McpTimeoutError("timeline", 5.0)
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                self.get_calls += 1
                # Preflight get (first) is NOT desired so the set happens;
                # the reconciliation get (second) shows the applied state.
                if self.get_calls == 1:
                    return {"success": True, "isEnabled": False, "amount": 0}
                return {"success": True, "isEnabled": True, "amount": 60}
            raise AssertionError(f"unexpected call {(tool_name, action)}")

    transport = TimeoutCountingTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    ctx.timeline_start = 0
    ctx.current_timeline_name = "ep-test-timeline"
    ctx.current_timeline_id = "id-1"
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        set_voice_isolation_state,
    )

    result = set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert transport.calls[1][1] == "set_voice_isolation_state"
    assert transport.calls[2][1] == "get_voice_isolation_state"
    assert transport.timeouts[0] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert transport.timeouts[1] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert transport.timeouts[2] == VOICE_ISOLATION_TIMEOUT_SECONDS
    assert result["is_enabled"] is True
    assert ctx.timeline_mutated is True


def test_voice_isolation_error_preflight_fails_closed_without_set() -> None:
    """An error preflight readback is a typed failure — never a reason to mutate."""

    class ErrorPreflightTransport:
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
            self.calls.append((tool_name, action, dict(normalized_params)))
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                return {"success": False, "status": "internal-error"}
            raise AssertionError(f"unexpected call {(tool_name, action)} — set must not fire")

    transport = ErrorPreflightTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        set_voice_isolation_state,
    )

    with pytest.raises(LiveAdapterError) as exc:
        set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert exc.value.code == "get-voice-failed"
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert ctx.timeline_mutated is False


def test_voice_isolation_malformed_preflight_fails_closed_without_set() -> None:
    """A successful-but-incomplete get (missing state fields) fails typed, never mutates."""

    class IncompletePreflightTransport:
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
            self.calls.append((tool_name, action, dict(normalized_params)))
            if (tool_name, action) == ("timeline", "get_voice_isolation_state"):
                # Validates against the typed readback (optional fields) but
                # carries no is_enabled/amount state at all.
                return {"success": True}
            raise AssertionError(f"unexpected call {(tool_name, action)} — set must not fire")

    transport = IncompletePreflightTransport()
    from services.mcp_execution.live_handlers.common import LiveSessionContext  # noqa: PLC0415

    ctx = LiveSessionContext.build(transport, {}, audio_measure=None)
    from services.mcp_execution.live_handlers.audio import (  # noqa: PLC0415
        set_voice_isolation_state,
    )

    with pytest.raises(LiveAdapterError) as exc:
        set_voice_isolation_state(ctx, "apply_voice_isolation", _voice_params())
    assert exc.value.code == "voice-readback-incomplete"
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == "get_voice_isolation_state"
    assert ctx.timeline_mutated is False


# ---------------------------------------------------------------- unsupported surfaces


@pytest.mark.parametrize(
    ("tool_surface", "action", "params"),
    [
        (
            "direct_script_adapter",
            "manual_required",
            {"action": "manual_required", "effect_kind": "manual_required", "note": "manual"},
        ),
        (
            "insert_fusion_title",
            "place_title",
            {
                "action": "place_title",
                "item_id": "t-1",
                "record_span": {"start_frame": 0, "end_frame": 10},
                "track_role": "primary",
            },
        ),
        (
            "external_asset_builder",
            "apply_kit_recipe",
            {
                "action": "apply_kit_recipe",
                "intent_id": "k-1",
                "kind": "k",
                "recipe_id": "r",
                "resolved_params": {},
                "rationale": "r",
                "target_span": {"start_frame": 0, "end_frame": 1},
            },
        ),
        (
            "manual_operator",
            "manual_required",
            {"action": "manual_required", "effect_kind": "manual_required", "note": "m"},
        ),
    ],
)
def test_unsupported_surfaces_raise_typed_before_dispatch(
    tool_surface: str, action: str, params: dict[str, object]
) -> None:
    """The surfaces still outside SUPPORTED_SURFACES (Task 5 moved the
    audio preset/level and loudness-QC surfaces live; Task 6 the DRX
    surface) refuse typed before any raw call."""
    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(LiveAdapterUnsupportedError):
        adapter(tool_surface, action, params)
    assert transport.calls == []


def test_no_raw_call_uses_logical_name_across_happy_flow() -> None:
    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
            ("media_pool", "safe_import_media"): [dict(PROBE_IMPORT)],
            ("media_pool", "append_to_timeline"): [dict(PROBE_APPEND)],
            ("timeline", "get_items_in_track"): [
                dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)]))
            ],
            ("timeline_item", "get_source_start_frame"): [_frame(10)],
            ("timeline_item", "get_source_end_frame"): [_frame(70)],
            ("timeline", "set_voice_isolation_state"): [dict(PROBE_VOICE_SET)],
            ("timeline", "get_voice_isolation_state"): [dict(PROBE_VOICE_GET)],
        }
    )
    adapter = LiveMcpAdapter(
        transport, media_paths={"src-001": "/media/src-001.mp4", "src-002": "/media/src-002.mp4"}
    )
    adapter("prepare_project", "prepare_project", _prepare_params())
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    adapter("append_to_timeline", "place_clip", _place_clip_params())
    adapter("set_voice_isolation_state", "apply_voice_isolation", _voice_params())
    assert all(c[0] != "prepare_project" for c in transport.calls)
    assert all(c[0] in ScriptedTransport.REAL_TOOLS for c in transport.calls)


def test_exhaustive_tool_surface_refusal_documented() -> None:
    all_surfaces: set[str] = set(get_args(ToolSurface))
    supported: set[str] = set(SUPPORTED_SURFACES)
    unsupported = all_surfaces - supported
    assert len(unsupported) >= 7
    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    color_params: dict[str, object] = {"action": "apply_color", "section": "s", "target_note": "n"}
    for surface in unsupported:
        with pytest.raises(LiveAdapterUnsupportedError):
            adapter(surface, "apply_color", color_params)


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 1 — measured regression baseline + red proofs.
# The tests above freeze the shipped behavior; the tests below record the
# measured v44-real-01 truth (4/16 surfaces live, 5 finishing failures) and
# pin the Phase-2 target contracts as failing-first proofs.
# ---------------------------------------------------------------------------

#: Derived (never hand-edited) from ToolSurface - SUPPORTED_SURFACES; the
#: measured baseline this freezes is 4 live of 16, i.e. 12 typed refusals.
KNOWN_NOT_LIVE_SURFACES: frozenset[str] = frozenset(get_args(ToolSurface)) - frozenset(
    SUPPORTED_SURFACES
)


class MeasuredFailedStep(NamedTuple):
    """One failed step of the measured v44-real-01 finishing run.

    Derived from the private ``mcp-run-report.json`` (202 steps: 197
    completed / 5 failed, every failure ``LiveAdapterUnsupportedError``).
    Each row's ``tool_surface`` is corroborated by that report's call ledger
    (``subtitle_generation_probe`` 3, ``direct_script_adapter`` 2,
    ``render_boundary_report`` 3, ``safe_apply_drx`` 3 — all error status).
    No private paths or media values are carried here.
    """

    step_id: str
    tool_surface: str
    action: str
    rung: str
    failure_code: str


MEASURED_FAILED_STEPS: tuple[MeasuredFailedStep, ...] = (
    MeasuredFailedStep(
        step_id="stp-subtitle-plan",
        tool_surface="subtitle_generation_probe",
        action="apply_subtitles",
        rung="mcp_verified_workflow",
        failure_code="executor-error",
    ),
    MeasuredFailedStep(
        step_id="stp-audio-dialogue_cleanup",
        tool_surface="direct_script_adapter",
        action="apply_audio_stage",
        rung="direct_scripting_gap_adapter",
        failure_code="executor-error",
    ),
    MeasuredFailedStep(
        step_id="stp-audio-dialogue_level_normalization",
        tool_surface="direct_script_adapter",
        action="apply_audio_stage",
        rung="direct_scripting_gap_adapter",
        failure_code="executor-error",
    ),
    MeasuredFailedStep(
        step_id="stp-audio-loudness_peak_qc",
        tool_surface="render_boundary_report",
        action="apply_audio_stage",
        rung="mcp_verified_workflow",
        failure_code="executor-error",
    ),
    MeasuredFailedStep(
        step_id="stp-color-technical_correction-exposure",
        tool_surface="safe_apply_drx",
        action="apply_color",
        rung="mcp_verified_workflow",
        failure_code="executor-error",
    ),
)

#: Channel audio-policy targets for the measured audio stages (ranges from
#: the product's own finishing policy, not run-specific measurements).
_STAGE_TARGETS: dict[str, tuple[str, float, float, str]] = {
    "dialogue_cleanup": ("noise_reduction", 3.0, 12.0, "db"),
    "dialogue_level_normalization": ("dialogue_loudness", -18.0, -16.0, "lufs"),
    "loudness_peak_qc": ("integrated_loudness", -17.0, -13.0, "lufs"),
}

#: The committed cues a native subtitle mutation must reproduce exactly
#: (text + record frames; mirrors the finishing-fixture plan cues).
NATIVE_COMMITTED_CUES: tuple[dict[str, object], ...] = (
    {
        "cue_id": "cue-s1",
        "text": "今日はDaVinci Resolveの使い方を紹介します",
        "start_frame": 15,
        "end_frame": 45,
    },
    {
        "cue_id": "cue-s2",
        "text": "再生と編集の違いに注意してください",
        "start_frame": 65,
        "end_frame": 86,
    },
)

CUE1_TEXT = str(NATIVE_COMMITTED_CUES[0]["text"])
CUE2_TEXT = str(NATIVE_COMMITTED_CUES[1]["text"])
CARD1_NAME = f"{TIMELINE_NAME}-cue-cue-s1"
CARD2_NAME = f"{TIMELINE_NAME}-cue-cue-s2"

PROBE_SUB_TRACK_COUNT_1 = {"count": 1, "success": True}
PROBE_SUB_ADD_TRACK = {"success": True}
PROBE_SUB_ITEMS_EMPTY = {"items": []}
PROBE_SUB_ITEM_1 = {
    "name": CARD1_NAME,
    "id": "ti-sub-c1",
    "start": TIMELINE_START + 15,
    "end": TIMELINE_START + 45,
    "duration": 30,
}
PROBE_SUB_ITEM_2 = {
    "name": CARD2_NAME,
    "id": "ti-sub-c2",
    "start": TIMELINE_START + 65,
    "end": TIMELINE_START + 86,
    "duration": 21,
}
PROBE_SUB_ITEMS_ONE = {"items": [dict(PROBE_SUB_ITEM_1)]}
PROBE_SUB_ITEMS_BOTH = {"items": [dict(PROBE_SUB_ITEM_1), dict(PROBE_SUB_ITEM_2)]}
PROBE_SUB_CREATE_CARD1 = {
    "name": CARD1_NAME,
    "id": "tl-card-1",
    "created_new": True,
    "success": True,
}
PROBE_SUB_CREATE_CARD2 = {
    "name": CARD2_NAME,
    "id": "tl-card-2",
    "created_new": True,
    "success": True,
}
PROBE_SUB_INSERT_TITLE = {"success": True}
PROBE_SUB_SET_INPUTS_1 = {
    "success": True,
    "tool_name": "Template",
    "results": {
        "StyledText": {"success": True, "value": CUE1_TEXT},
        "Font": {"success": True, "value": "Hiragino Sans W5"},
        "Size": {"success": True, "value": 0.055},
        "Center": {"success": True, "value": {"1": 0.5, "2": 0.14}},
        "Enabled3": {"success": True, "value": 1},
        "Red3": {"success": True, "value": 0.0},
        "Green3": {"success": True, "value": 0.0},
        "Blue3": {"success": True, "value": 0.0},
        "Alpha3": {"success": True, "value": 0.8},
        "Softness3": {"success": True, "value": 0.005},
        "Offset3": {"success": True, "value": {"1": 0.025, "2": -0.04, "3": 0.0}},
    },
}
PROBE_SUB_SET_INPUTS_2 = {
    "success": True,
    "tool_name": "Template",
    "results": {
        "StyledText": {"success": True, "value": CUE2_TEXT},
        "Font": {"success": True, "value": "Hiragino Sans W5"},
        "Size": {"success": True, "value": 0.055},
        "Center": {"success": True, "value": {"1": 0.5, "2": 0.14}},
        "Enabled3": {"success": True, "value": 1},
        "Red3": {"success": True, "value": 0.0},
        "Green3": {"success": True, "value": 0.0},
        "Blue3": {"success": True, "value": 0.0},
        "Alpha3": {"success": True, "value": 0.8},
        "Softness3": {"success": True, "value": 0.005},
        "Offset3": {"success": True, "value": {"1": 0.025, "2": -0.04, "3": 0.0}},
    },
}
PROBE_SUB_TEXT_1 = {
    "tool_name": "Template",
    "input_name": "StyledText",
    "text": CUE1_TEXT,
}
PROBE_SUB_TEXT_2 = {
    "tool_name": "Template",
    "input_name": "StyledText",
    "text": CUE2_TEXT,
}
PROBE_SUB_MPI_1 = {"name": CARD1_NAME, "id": "mpi-card-1"}
PROBE_SUB_MPI_2 = {"name": CARD2_NAME, "id": "mpi-card-2"}
PROBE_SUB_FONT_GET = {"value": "Hiragino Sans W5"}
PROBE_SUB_SIZE_GET = {"value": 0.055}
PROBE_SUB_SHADOW_GET = {"value": 1}
#: Live-observed Center readback shape (Resolve 21.0.4.5): points carry a
#: third axis; the binding's domain is the bound (x, y) axes only.
PROBE_SUB_CENTER_GET = {"value": {"1": 0.5, "2": 0.14, "3": 0.0}}
PROBE_SUB_FONT_GET_DRIFTED = {"value": "Open Sans"}
PROBE_SUB_SIZE_GET_DRIFTED = {"value": 0.09}
PROBE_SUB_CENTER_GET_DRIFTED = {"value": {"1": 0.5, "2": 0.86, "3": 0.0}}
PROBE_SUB_CENTER_GET_MISSING_AXIS = {"value": {"2": 0.14, "3": 0.0}}


def _subtitle_params() -> dict[str, object]:
    return {
        "action": "apply_subtitles",
        "selected_path": "native_text_plus",
        "cues": [
            {
                "cue_id": str(cue["cue_id"]),
                "text": str(cue["text"]),
                "record_span": {
                    "start_frame": cue["start_frame"],
                    "end_frame": cue["end_frame"],
                },
            }
            for cue in NATIVE_COMMITTED_CUES
        ],
        "style_profile_id": "subtitle-style-default",
    }


def _script_subtitle_creation(
    transport: ScriptedTransport,
    *,
    track_count: int = 1,
    scans: list[dict[str, Any]] | None = None,
) -> None:
    """Script the full two-cue native creation flow on a prepared adapter."""
    if scans is None:
        # Repair-6 order: ONE all-cues snapshot (empty), then each created
        # cue's independent post-append verification scan.
        scans = [
            dict(PROBE_SUB_ITEMS_EMPTY),
            dict(PROBE_SUB_ITEMS_ONE),
            dict(PROBE_SUB_ITEMS_BOTH),
        ]
    transport._script[("timeline", "get_track_count")] = [{"count": track_count, "success": True}]
    transport._script[("timeline", "add_track")] = [dict(PROBE_SUB_ADD_TRACK)]
    transport._script[("timeline", "get_items_in_track")] = [dict(s) for s in scans]
    transport._script[("media_pool", "create_timeline")] = [
        dict(PROBE_SUB_CREATE_CARD1),
        dict(PROBE_SUB_CREATE_CARD2),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 8
    transport._script[("timeline", "insert_fusion_title")] = [dict(PROBE_SUB_INSERT_TITLE)] * 2
    transport._script[("fusion_comp", "safe_set_inputs")] = [
        dict(PROBE_SUB_SET_INPUTS_1),
        dict(PROBE_SUB_SET_INPUTS_2),
    ]
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
        dict(PROBE_SUB_TEXT_2),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
    ]
    transport._script[("timeline", "get_media_pool_item")] = [
        dict(PROBE_SUB_MPI_1),
        dict(PROBE_SUB_MPI_2),
    ]
    transport._script[("media_pool", "append_to_timeline")] = [
        {
            "count": 1,
            "items": [{"name": CARD1_NAME, "timeline_item_id": "ti-sub-c1"}],
            "success": True,
            "verification_status": "readback_verified",
        },
        {
            "count": 1,
            "items": [{"name": CARD2_NAME, "timeline_item_id": "ti-sub-c2"}],
            "success": True,
            "verification_status": "readback_verified",
        },
    ]


def _subtitle_calls(
    transport: ScriptedTransport, action: str
) -> list[tuple[str, str, dict[str, object]]]:
    return [call for call in transport.calls if call[1] == action]


def _audio_stage_params(stage: str) -> dict[str, object]:
    metric, minimum, maximum, unit = _STAGE_TARGETS[stage]
    return {
        "action": "apply_audio_stage",
        "stage": stage,
        "goal": f"{stage} for the dialogue chain",
        "metric": metric,
        "minimum": minimum,
        "maximum": maximum,
        "unit": unit,
        "preset_ref": _PRESET_STAGES.get(stage),
    }


def _color_params() -> dict[str, object]:
    return {
        "action": "apply_color",
        "section": "technical_correction.exposure",
        "target_note": "exposure evidence fixture",
        "look_ref": None,
        "drx_ref": "drx-technical-normalize-v1",
        "targets": [{"item_id": "itm-color-1", "record_span": {"start_frame": 0, "end_frame": 60}}],
    }


def _color_params_unwired() -> dict[str, object]:
    """The pre-Task-6 shape: no DRX binding ref, no explicit targets."""

    return {
        "action": "apply_color",
        "section": "grade",
        "target_note": "look",
        "look_ref": None,
    }


def _measured_step_params(row: MeasuredFailedStep) -> dict[str, object]:
    if row.action == "apply_audio_stage":
        return _audio_stage_params(row.step_id.removeprefix("stp-audio-"))
    return _color_params()


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 5 — Resolve-native dialogue processing + measured QC.
# Real measured media (pinned ffmpeg) behind a scripted render transport: the
# handler's measurements are genuinely computed from files, only the vendor
# render/apply/listing responses are scripted.
# ---------------------------------------------------------------------------

PROBE_PRESETS_LISTED = {"presets": ["dialogue-chain", "music-bed"], "success": True}
PROBE_PRESETS_EMPTY = {"presets": {}, "success": True}
PROBE_PRESET_APPLY = {"success": True, "preset_name": "dialogue-chain"}
PROBE_PRESET_APPLY_FALSE = {"success": False}
PROBE_BOUNDARY = {"success": True, "capabilities": {"formats": 8}}
PROBE_PREPARE_JOB = {"success": True, "job_id": "job-t5-1"}
PROBE_RENDER_START = {"success": True}
PROBE_JOB_DONE = {"CompletionPercentage": 100.0, "IsRenderingInProgress": False, "success": True}
PROBE_RENDER_DELETE = {"success": True}

#: Calibrated against the pinned ffmpeg (measured): 0.28 → ≈-14.8 LUFS
#: (inside loudness QC's [-17,-13]), 0.21 → ≈-17.3 (inside normalization's
#: [-18,-16]), 0.9 → ≈-4.6 (outside), dip 0.10 vs 0.05 → ≈6.05 dB floor
#: delta (inside cleanup's [3,12]).
_T5_AUDIO_EXPRS = {
    "loud_ok": "sin(2*PI*440*t)*0.28",
    "normalized": "sin(2*PI*440*t)*0.21",
    "hot": "sin(2*PI*440*t)*0.9",
    "clean_before": "sin(2*PI*440*t)*0.25*max(sin(2*PI*0.4*t)\\,0.10)",
    "clean_after": "sin(2*PI*440*t)*0.25*max(sin(2*PI*0.4*t)\\,0.05)",
    "gain_after": "sin(2*PI*440*t)*0.5*max(sin(2*PI*0.4*t)\\,0.10)",
}


def _t5_media_file(tools, out_dir: Path, name: str, audio_expr: str) -> Path:
    out = out_dir / name
    subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30:d=6",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs={audio_expr}:s=48000:d=6",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "h264_videotoolbox",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            str(out),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return out


@pytest.fixture(scope="module")
def t5_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tools = load_qc_tools()
    root = tmp_path_factory.mktemp("t5-audio-media")
    return {
        key: _t5_media_file(tools, root, f"{key}.mp4", expr)
        for key, expr in _T5_AUDIO_EXPRS.items()
    }


class RenderingTransport(ScriptedTransport):
    """ScriptedTransport plus the one thing scripts cannot fake: the render
    output file. On ``render.start`` it materializes the next real measured
    media file at the prepared job's target path (fresh mtime), exactly
    where a live Resolve would write it."""

    def __init__(
        self,
        script: Mapping[tuple[str, str], list[dict[str, Any]]],
        render_dir: Path,
        outputs: list[Path],
    ) -> None:
        super().__init__(script)
        self._render_dir = render_dir
        self._outputs = list(outputs)
        self._prepared_name = ""
        self.rendered: list[Path] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        if (tool_name, action) == ("render", "prepare_render_job"):
            self._prepared_name = str(normalized_params.get("custom_name", ""))
        result = super().__call__(
            tool_name, action, normalized_params, timeout_seconds=timeout_seconds
        )
        if (tool_name, action) == ("render", "start"):
            source = self._outputs[min(len(self.rendered), len(self._outputs) - 1)]
            target = self._render_dir / f"{self._prepared_name}.mp4"
            self._render_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.rendered.append(target)
        return result


def _measured_adapter(transport: ScriptedTransport, render_dir: Path) -> LiveMcpAdapter:
    return LiveMcpAdapter(
        transport,
        media_paths={},
        audio_measure=default_audio_measurement(),
        render_dir=str(render_dir),
    )


def test_measured_baseline_eight_surfaces_live_and_eight_refused_before_dispatch() -> None:
    """Given the 18-surface product vocabulary, the live adapter dispatches
    exactly 11 surfaces to the pinned MCP (subtitle wired by Task 4; the
    audio preset/level and loudness-QC surfaces by Task 5; the DRX color
    surface by Task 6; the native render lifecycle by Task 7; the
    orientation transform by the Gate V44-2 fix; the telop nested-card
    surface by telop-nested WBS-2) and refuses the other 7
    typed, before any raw transport call."""

    all_surfaces: set[str] = set(get_args(ToolSurface))
    assert len(all_surfaces) == 18
    refused = set(KNOWN_NOT_LIVE_SURFACES)
    assert len(refused) == 7

    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
            ("media_pool", "safe_import_media"): [dict(PROBE_IMPORT)],
            ("media_pool", "append_to_timeline"): [dict(PROBE_APPEND)],
            ("timeline", "get_items_in_track"): [
                dict(_track_listing([])),
                dict(_track_listing([_row("itm-001", TIMELINE_START, TIMELINE_START + 60)])),
            ],
            ("timeline_item", "get_source_start_frame"): [_frame(10)],
            ("timeline_item", "get_source_end_frame"): [_frame(70)],
            ("timeline", "set_voice_isolation_state"): [dict(PROBE_VOICE_SET)],
            # Preflight get non-desired so the set surface stays exercised;
            # the post-set get shows the applied state.
            ("timeline", "get_voice_isolation_state"): [
                {"success": True, "isEnabled": False, "amount": 0},
                dict(PROBE_VOICE_GET),
            ],
        }
    )
    adapter = LiveMcpAdapter(
        transport, media_paths={"src-001": "/media/src-001.mp4", "src-002": "/media/src-002.mp4"}
    )
    adapter("prepare_project", "prepare_project", _prepare_params())
    adapter("safe_import_media", "import_media", _import_params("src-001"))
    adapter("append_to_timeline", "place_clip", _place_clip_params())
    adapter("set_voice_isolation_state", "apply_voice_isolation", _voice_params())

    raw_actions = {(tool, action) for tool, action, _ in transport.calls}
    assert ("project_manager", "load") in raw_actions
    assert ("media_pool", "safe_import_media") in raw_actions
    assert ("media_pool", "append_to_timeline") in raw_actions
    assert ("timeline", "set_voice_isolation_state") in raw_actions

    transport.calls.clear()
    color_params = _color_params()
    for surface in refused:
        with pytest.raises(LiveAdapterUnsupportedError):
            adapter(surface, "apply_color", color_params)
    assert transport.calls == []


def test_measured_failed_finishing_steps_except_wired_domains_refuse_typed() -> None:
    """Of the five measured v44-real-01 finishing failures, the two that
    Phase 2 has not rewired (dialogue cleanup + level normalization, which
    the measured run executed on the non-MCP executor surface) are exactly
    typed adapter refusals. Subtitle dispatches since Task 4; the
    loudness-QC surface since Task 5; the DRX exposure surface since
    Task 6."""

    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    still_refused = [
        row
        for row in MEASURED_FAILED_STEPS
        if row.tool_surface
        not in ("subtitle_generation_probe", "render_boundary_report", "safe_apply_drx")
    ]
    assert len(still_refused) == 2
    for row in still_refused:
        with pytest.raises(LiveAdapterUnsupportedError):
            adapter(row.tool_surface, row.action, _measured_step_params(row))
    assert transport.calls == []


def _require_native_call(call: Callable[[], object], behavior: str) -> object:
    """Failing-first harness: a typed surface refusal becomes a named red
    failure (``behavior`` unsupported), never a silent skip."""
    try:
        return call()
    except LiveAdapterUnsupportedError as error:
        pytest.fail(
            f"{behavior}: product surface refused before dispatch ({error.code}: {error.detail})"
        )


# The three tests below are RED until the remaining Phase-2 finishing tasks
# land: audio preset/level + loudness (Task 5), DRX (Task 6). The subtitle
# red turned green in Task 4 (native nested-timeline construction).


def test_exact_subtitle_mutation_creates_committed_cues_natively() -> None:
    """GREEN (Task 4): the subtitle surface mutates DaVinci natively through
    the existing pinned MCP (nested-timeline Fusion Titles) and returns a
    per-cue readback with exact text and record frames."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)

    result = _require_native_call(
        lambda: adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params()),
        "exact subtitle mutation of committed cues",
    )

    cues = result.get("cues") if isinstance(result, dict) else None
    if not isinstance(cues, list):
        pytest.fail("native subtitle mutation returned no per-cue readback")
    assert len(cues) == len(NATIVE_COMMITTED_CUES)
    for committed, actual in zip(NATIVE_COMMITTED_CUES, cues, strict=True):
        entry = cast("dict[str, object]", actual)
        assert entry.get("cue_id") == committed["cue_id"]
        assert entry.get("text") == committed["text"]
        span = cast("dict[str, object]", entry.get("record_span"))
        assert span.get("start_frame") == committed["start_frame"]
        assert span.get("end_frame") == committed["end_frame"]

    # The mutation rides existing pinned MCP operations only: every raw call
    # names a vendor tool, and the overlay append carries the exact absolute
    # record span (end frame exclusive, overlay track 2).
    raw_tools = {call[0] for call in transport.calls}
    assert raw_tools <= ScriptedTransport.REAL_TOOLS
    appends = _subtitle_calls(transport, "append_to_timeline")
    assert len(appends) == 2
    first_clip = _get_clip_infos(appends[0])[0]
    assert first_clip["record_frame"] == TIMELINE_START + 15
    assert first_clip["end_frame"] == 30
    assert first_clip["track_index"] == 2
    second_clip = _get_clip_infos(appends[1])[0]
    assert second_clip["record_frame"] == TIMELINE_START + 65
    assert second_clip["end_frame"] == 21
    assert second_clip["track_index"] == 2
    # Exact text reached the Text+ Template tool of each card timeline, bound
    # to the profile's Japanese-capable font (tofu-proof presentation).
    styled = [call for call in transport.calls if call[1] == "safe_set_inputs"]
    assert len(styled) == 2
    styled_inputs = [cast("dict[str, object]", call[2]["inputs"]) for call in styled]
    assert styled_inputs[0]["StyledText"] == CUE1_TEXT
    assert styled_inputs[1]["StyledText"] == CUE2_TEXT
    assert all(call[2]["tool_name"] == "Template" for call in styled)
    assert all(inputs["Font"] == "Hiragino Sans W5" for inputs in styled_inputs)
    # D shadow inputs ride every cue write (element-3 wire set); the
    # offset is the POINT input (live-verified: OffsetX/Y3 are phantoms)
    assert all(inputs["Enabled3"] == 1 for inputs in styled_inputs)
    assert all(inputs["Softness3"] == 0.005 for inputs in styled_inputs)
    assert all(inputs["Offset3"] == [0.025, -0.04] for inputs in styled_inputs)
    # The style binding is observable from independent readback, not just the
    # request: each cue row carries the read-back font, size, and center.
    rows = cast("list[dict[str, object]]", cues)
    style_rows = [cast("dict[str, object]", row.get("style")) for row in rows]
    assert all(row.get("font") == "Hiragino Sans W5" for row in style_rows)
    assert all(row.get("size") == 0.055 for row in style_rows)
    assert all(row.get("center") == [0.5, 0.14] for row in style_rows)
    get_inputs = [call for call in transport.calls if call[1] == "get_input"]
    # per card readback: Font, Size, Center, Enabled3 (the D shadow axis)
    assert [call[2]["input_name"] for call in get_inputs] == [
        "Font",
        "Size",
        "Center",
        "Enabled3",
    ] * 4


def test_subtitle_rerun_detects_identical_cues_and_creates_no_duplicates() -> None:
    """A second run finds the identical cues by exact record span, verifies
    their card-timeline text, and performs zero creation mutations."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    transport.calls.clear()
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(PROBE_SUB_ITEMS_BOTH),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 4
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
        dict(PROBE_SUB_TEXT_2),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
    ]

    result = adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    mutating_actions = {
        ("media_pool", "create_timeline"),
        ("media_pool", "append_to_timeline"),
        ("timeline", "insert_fusion_title"),
        ("fusion_comp", "safe_set_inputs"),
        ("timeline", "add_track"),
    }
    assert mutating_actions.isdisjoint({(c[0], c[1]) for c in transport.calls})
    cues = cast("dict[str, object]", result).get("cues")
    assert isinstance(cues, list)
    assert len(cues) == len(NATIVE_COMMITTED_CUES)
    texts = _subtitle_text_rows(cast("list[dict[str, object]]", cues))
    assert texts == [CUE1_TEXT, CUE2_TEXT]
    style_rows = [
        cast("dict[str, object]", row.get("style")) for row in cast("list[dict[str, object]]", cues)
    ]
    assert all(row.get("font") == "Hiragino Sans W5" for row in style_rows)
    assert all(row.get("size") == 0.055 for row in style_rows)
    assert all(row.get("center") == [0.5, 0.14] for row in style_rows)
    rerun_get_inputs = [call for call in transport.calls if call[1] == "get_input"]
    assert [call[2]["input_name"] for call in rerun_get_inputs] == [
        "Font",
        "Size",
        "Center",
        "Enabled3",
    ] * 2


def test_all_existing_subtitle_rerun_scans_the_overlay_track_once() -> None:
    """Live-measured blocker (repair 6): the overlay-track scan takes
    67.787s for 100 items, and the old per-cue loop re-scanned per cue on
    the 30s default — an all-existing rerun must perform exactly ONE scan
    on the named measured deadline, with every non-scan/non-switch vendor
    call keeping the transport default, and zero creation mutations."""
    adapter, transport = _make_prepared_adapter()
    _rerun_all_existing_script(transport, switches=5)
    transport.calls.clear()
    transport.timeouts.clear()

    result = adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    timed = list(zip(transport.calls, transport.timeouts, strict=True))
    scan_rows = [row for row in timed if row[0][1] == "get_items_in_track"]
    assert len(scan_rows) == 1
    assert scan_rows[0][0][2] == {"track_type": "video", "track_index": 2}
    assert scan_rows[0][1] == SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS
    unrelated_timeouts = [t for c, t in timed if c[1] not in {"get_items_in_track", "set_current"}]
    assert set(unrelated_timeouts) <= {None}
    assert _subtitle_calls(transport, "create_timeline") == []
    assert _subtitle_calls(transport, "append_to_timeline") == []
    assert _subtitle_calls(transport, "insert_fusion_title") == []
    cues = cast("dict[str, object]", result).get("cues")
    assert isinstance(cues, list)
    assert len(cues) == len(NATIVE_COMMITTED_CUES)


# ---- Task 5 repair 7: card switches grow to 65.898s (30s default killed
# the rerun around cue ~50), and a failed attempt can leave a cue card
# CURRENT — retries must restore the episode timeline before inspecting.


def _rerun_all_existing_script(transport: ScriptedTransport, *, switches: int) -> None:
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [dict(PROBE_SUB_ITEMS_BOTH)]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * switches
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
        dict(PROBE_SUB_TEXT_2),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
    ]


def test_subtitle_attempt_restores_the_main_timeline_before_track_inspection() -> None:
    """Live evidence: a timed-out attempt left a cue-card timeline CURRENT
    (track 2 read 0 items on it) — every subtitle attempt must FIRST
    re-select the prepared main timeline through the typed seam, before any
    track-count or overlay-scan call trusts session state."""
    adapter, transport = _make_prepared_adapter()
    _rerun_all_existing_script(transport, switches=1)

    transport.calls.clear()
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    timeline_ops = [c for c in transport.calls if c[0] == "timeline"]
    assert timeline_ops[0] == ("timeline", "set_current", {"name": TIMELINE_NAME})
    assert timeline_ops[1][1] == "get_track_count"


def test_subtitle_timeline_switches_carry_the_measured_card_deadline() -> None:
    """Card switches measured 17.110s→65.898s growing with cue position —
    every subtitle set_current (restore, card-in, main-out) must carry the
    named card-switch deadline; the overlay scan keeps its own scan
    deadline; every other vendor call keeps the transport default."""
    adapter, transport = _make_prepared_adapter()
    _rerun_all_existing_script(transport, switches=5)
    transport.calls.clear()
    transport.timeouts.clear()

    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    timed = list(zip(transport.calls, transport.timeouts, strict=True))
    switch_timeouts = [t for c, t in timed if c[1] == "set_current"]
    assert switch_timeouts
    assert set(switch_timeouts) == {SUBTITLE_CARD_SWITCH_TIMEOUT_SECONDS}
    scan_timeouts = [t for c, t in timed if c[1] == "get_items_in_track"]
    assert scan_timeouts == [SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS]
    other_timeouts = [t for c, t in timed if c[1] not in {"set_current", "get_items_in_track"}]
    assert set(other_timeouts) <= {None}


def _subtitle_text_rows(cues: list[dict[str, object]]) -> list[str]:
    return [str(cue.get("text")) for cue in cues]


def test_subtitle_rerun_with_drifted_text_is_a_typed_failure() -> None:
    """An existing cue whose card-timeline text disagrees with the committed
    text is a typed readback failure, never a silent overwrite or success."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    transport.calls.clear()
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(PROBE_SUB_ITEMS_BOTH),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 2
    transport._script[("fusion_comp", "get_text_plus")] = [
        {"tool_name": "Template", "input_name": "StyledText", "text": "drifted text"},
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-text-mismatch"


def test_subtitle_style_readback_mismatch_is_a_typed_failure() -> None:
    """When the independent Font readback disagrees with the profile's bound
    font (e.g. a silent input lie or external drift), creation refuses typed
    instead of reporting a cue whose glyphs would render as tofu."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET_DRIFTED),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SIZE_GET),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"
    # Nothing was placed: the refusal happened before the overlay append.
    assert _subtitle_calls(transport, "append_to_timeline") == []


def test_subtitle_size_drift_during_creation_is_a_typed_failure() -> None:
    """A Size readback that disagrees with the bound profile size refuses
    typed before the card is placed: numeric drift rescales every glyph and
    must never pass silently."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET_DRIFTED),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SIZE_GET),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"
    assert _subtitle_calls(transport, "append_to_timeline") == []


def test_subtitle_center_drift_during_creation_is_a_typed_failure() -> None:
    """A Center point readback that disagrees with the bound position (here
    the y flipped toward the top of frame) refuses typed before placement."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET_DRIFTED),
        dict(PROBE_SUB_SIZE_GET),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"
    assert _subtitle_calls(transport, "append_to_timeline") == []


def test_subtitle_style_readback_drift_on_rerun_is_a_typed_failure() -> None:
    """A rerun over a cue whose card font drifted away from the bound profile
    font fails typed rather than blessing the drifted presentation."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    transport.calls.clear()
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(PROBE_SUB_ITEMS_BOTH),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 2
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET_DRIFTED),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"


def test_subtitle_size_drift_on_rerun_is_a_typed_failure() -> None:
    """A rerun over a cue whose card Size drifted away from the bound profile
    size fails typed: the rerun must read and compare the size, not return a
    null placeholder."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    transport.calls.clear()
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(PROBE_SUB_ITEMS_BOTH),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 2
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET_DRIFTED),
        dict(PROBE_SUB_CENTER_GET),
        dict(PROBE_SUB_SHADOW_GET),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"


def test_subtitle_center_drift_on_rerun_is_a_typed_failure() -> None:
    """A rerun over a cue whose card Center drifted away from the bound
    position fails typed; the dict-shaped point readback is normalized
    before comparison, never shape-compared."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())

    transport.calls.clear()
    transport._script[("timeline", "get_track_count")] = [{"count": 2, "success": True}]
    transport._script[("timeline", "get_items_in_track")] = [
        dict(PROBE_SUB_ITEMS_BOTH),
    ]
    transport._script[("timeline", "set_current")] = [dict(PROBE_SET_CURRENT)] * 2
    transport._script[("fusion_comp", "get_text_plus")] = [
        dict(PROBE_SUB_TEXT_1),
    ]
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET_DRIFTED),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"


def test_subtitle_center_readback_missing_bound_axis_is_a_typed_failure() -> None:
    """A Center readback lacking a bound axis (x missing here) cannot be
    compared and is drift by definition, not a pass."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)
    transport._script[("fusion_comp", "get_input")] = [
        dict(PROBE_SUB_FONT_GET),
        dict(PROBE_SUB_SIZE_GET),
        dict(PROBE_SUB_CENTER_GET_MISSING_AXIS),
        dict(PROBE_SUB_SIZE_GET),
    ]

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", _subtitle_params())
    assert excinfo.value.code == "subtitle-style-mismatch"
    assert _subtitle_calls(transport, "append_to_timeline") == []


def test_subtitle_malformed_params_and_unknown_style_refuse_typed() -> None:
    """Empty cue text, an empty record span, and an unmapped style profile
    fail typed before any raw transport call."""
    adapter, transport = _make_prepared_adapter()
    _script_subtitle_creation(transport)

    bad_text = _subtitle_params()
    cast("list[dict[str, object]]", bad_text["cues"])[0]["text"] = ""
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", bad_text)
    assert excinfo.value.code == "params-invalid"

    bad_span = _subtitle_params()
    cast("list[dict[str, object]]", bad_span["cues"])[0]["record_span"] = {
        "start_frame": 45,
        "end_frame": 45,
    }
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", bad_span)
    assert excinfo.value.code == "params-invalid"

    bad_style = _subtitle_params()
    bad_style["style_profile_id"] = "prof-unknown"
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter("subtitle_generation_probe", "apply_subtitles", bad_style)
    assert excinfo.value.code == "style-profile-unsupported"
    assert transport.calls == []


def test_future_audio_preset_and_level_mutation_applies_named_fairlight_preset(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """GREEN (Task 5): the dialogue cleanup stage applies a named Resolve
    Fairlight preset through the pinned MCP and reads back applied state
    plus a MEASURED stage value (before/after render noise-floor delta)
    inside the stage's target range."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("resolve_control", "get_fairlight_presets"): [dict(PROBE_PRESETS_LISTED)],
            ("project_settings", "apply_fairlight_preset"): [dict(PROBE_PRESET_APPLY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)] * 2,
            ("render", "start"): [dict(PROBE_RENDER_START)] * 2,
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)] * 2,
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["clean_before"], t5_media["clean_after"]],
    )
    adapter = _measured_adapter(transport, render_dir)
    params = _audio_stage_params("dialogue_cleanup")
    metric, minimum, maximum, _unit = _STAGE_TARGETS["dialogue_cleanup"]

    result = _require_native_call(
        lambda: adapter("safe_set_audio_properties", "apply_audio_stage", params),
        "audio preset/level mutation via a named Fairlight preset",
    )

    data = cast("dict[str, object]", result)
    preset = data.get("preset")
    assert isinstance(preset, dict), "preset identity readback is required"
    preset_name = preset.get("name")
    assert isinstance(preset_name, str)
    assert preset_name, "preset identity must carry a name"
    assert preset.get("applied") is True
    assert preset.get("kit_recipe_id") == "audio/dialogue-chain"
    measured = data.get("measured")
    assert isinstance(measured, dict), "measured stage value is required"
    value = measured.get(metric)
    assert isinstance(value, float)
    assert minimum <= value <= maximum
    # The value is the media-measured floor delta, never a plan echo: it
    # matches the two retained measurement rows to the milli-decibel.
    before = cast("dict[str, object]", measured.get("before"))
    after = cast("dict[str, object]", measured.get("after"))
    assert before.get("media_sha256") != after.get("media_sha256")

    def _gap_db(row: dict[str, object]) -> float:
        return cast("float", row["integrated_loudness_lufs"]) - cast("int", row["floor_mb"]) / 1000

    gap_improvement = _gap_db(after) - _gap_db(before)
    assert value == pytest.approx(gap_improvement)
    assert measured.get("measured_from") == "resolve_render"
    apply_calls = [c for c in transport.calls if c[1] == "apply_fairlight_preset"]
    assert len(apply_calls) == 1
    assert apply_calls[0][2] == {"preset_name": "dialogue-chain"}


# ---- Task 5 repair 3: cleanup measures loudness-RELATIVE floor improvement
# (live evidence: the tuned preset raised loudness -26.1 → -20.1 (+6.0 dB)
# and the floor +6.198 dB; the old absolute floor delta mislabeled that
# pure gain as -6.198 dB "noise reduction").


def _measured_row(loudness: float, floor_db: float) -> MeasuredAudio:
    return MeasuredAudio(
        integrated_loudness_lufs=loudness,
        true_peak_dbtp=-1.5,
        channels=2,
        peak_mb=-3000,
        floor_mb=int(floor_db * 1000),
        max_silence_ms=0,
        media_name="row.mp4",
        media_sha256="a" * 64,
    )


def _cleanup_stage_params() -> AudioStageParams:
    return AudioStageParams(
        action="apply_audio_stage",
        stage="dialogue_cleanup",
        goal="fixture",
        metric="noise_reduction",
        minimum=3.0,
        maximum=12.0,
        unit="db",
        preset_ref="fairlight-dialogue-chain-v1",
    )


def test_cleanup_value_reports_zero_for_a_pure_gain_preset() -> None:
    """+6 dB loudness together with +6 dB floor is pure gain, not noise
    reduction: the measured cleanup value must be 0 dB (the old absolute
    floor delta reported -6)."""
    before = _measured_row(-26.1, -56.0)
    after = _measured_row(-20.1, -50.0)
    value = audio._stage_value(_cleanup_stage_params(), before, after)
    assert value == pytest.approx(0.0, abs=1e-9)


def test_cleanup_value_reports_relative_floor_improvement() -> None:
    """A preset that lifts dialogue +6 dB while pushing the floor DOWN 2 dB
    improves the dialogue-to-floor gap by 8 dB — inside [3,12] — even
    though the old absolute floor delta would report only +2."""
    before = _measured_row(-26.1, -56.0)
    after = _measured_row(-20.1, -58.0)
    value = audio._stage_value(_cleanup_stage_params(), before, after)
    assert value == pytest.approx(8.0, abs=1e-9)
    _metric, minimum, maximum, _unit = _STAGE_TARGETS["dialogue_cleanup"]
    assert minimum <= value <= maximum


def test_pure_gain_media_pair_measures_zero_cleanup(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """End-to-end media regression (live-defect repro): a preset whose only
    effect is gain — the clean_before signal doubled, measured +6.0 dB
    loudness AND +6.0 dB floor — must measure ≈0 dB cleanup (the old
    absolute floor delta measured -6.0 on this exact pair). The widened
    [-1,1] gate makes the pass/fail itself discriminate the formula."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("resolve_control", "get_fairlight_presets"): [dict(PROBE_PRESETS_LISTED)],
            ("project_settings", "apply_fairlight_preset"): [dict(PROBE_PRESET_APPLY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)] * 2,
            ("render", "start"): [dict(PROBE_RENDER_START)] * 2,
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)] * 2,
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["clean_before"], t5_media["gain_after"]],
    )
    adapter = _measured_adapter(transport, render_dir)
    params = dict(_audio_stage_params("dialogue_cleanup"), minimum=-1.0, maximum=1.0)

    result = cast(
        "dict[str, object]",
        adapter("safe_set_audio_properties", "apply_audio_stage", params),
    )

    value = cast("float", result["value"])
    assert value == pytest.approx(0.0, abs=0.5)
    assert result.get("metric") == "noise_reduction"


def test_dialogue_level_normalization_measures_after_render_loudness(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """The normalization stage's measured value is the after-render's
    integrated loudness — inside the dialogue-loudness target range."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("resolve_control", "get_fairlight_presets"): [dict(PROBE_PRESETS_LISTED)],
            ("project_settings", "apply_fairlight_preset"): [dict(PROBE_PRESET_APPLY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)] * 2,
            ("render", "start"): [dict(PROBE_RENDER_START)] * 2,
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)] * 2,
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["normalized"], t5_media["normalized"]],
    )
    adapter = _measured_adapter(transport, render_dir)
    _metric, minimum, maximum, _unit = _STAGE_TARGETS["dialogue_level_normalization"]

    result = adapter(
        "safe_set_audio_properties",
        "apply_audio_stage",
        _audio_stage_params("dialogue_level_normalization"),
    )

    data = cast("dict[str, object]", result)
    after = cast("dict[str, object]", cast("dict[str, object]", data.get("measured")).get("after"))
    after_loudness = cast("float", after["integrated_loudness_lufs"])
    assert data.get("value") == pytest.approx(after_loudness)
    assert minimum <= cast("float", data["value"]) <= maximum


def test_future_loudness_measurement_comes_from_the_native_render(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """GREEN (Task 5): the loudness/peak QC stage returns values measured
    from Resolve-rendered media, never the plan's own minimum."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["loud_ok"]],
    )
    adapter = _measured_adapter(transport, render_dir)
    params = _audio_stage_params("loudness_peak_qc")
    _metric, minimum, maximum, _unit = _STAGE_TARGETS["loudness_peak_qc"]

    result = _require_native_call(
        lambda: adapter("render_boundary_report", "apply_audio_stage", params),
        "loudness measurement of the native render",
    )

    data = cast("dict[str, object]", result)
    measurements = data.get("measurements")
    assert isinstance(measurements, dict), "rendered-media measurement readback is required"
    assert measurements.get("measured_from") == "resolve_render"
    loudness = measurements.get("integrated_loudness_lufs")
    assert isinstance(loudness, float)
    assert minimum <= loudness <= maximum
    peak = measurements.get("true_peak_dbtp")
    assert isinstance(peak, float)
    assert measurements.get("channels") == 2
    assert str(measurements.get("media_name")).startswith("audio-loudness_peak_qc-")
    assert len(str(measurements.get("media_sha256"))) == 64
    # The boundary report was reached but never used as the measurement.
    boundary_calls = [c for c in transport.calls if c[1] == "export_render_boundary_report"]
    assert len(boundary_calls) == 1
    # Live evidence (rerun3): parameterless request = unbounded matrix probe
    # that never answers (>170s); the exact bounded control is required.
    assert boundary_calls[0][2] == {"include_matrix": False}
    # Completed-job cleanup: the finished loudness job must leave the queue
    # (measured on v44-real-01: one queued job accumulated per attempt).
    delete_calls = [c for c in transport.calls if c[1] == "delete_job"]
    assert delete_calls, "the completed loudness job must be deleted, not accumulated"
    assert delete_calls[0][2] == {"job_id": "job-t5-1"}


def test_render_lifecycle_calls_carry_the_named_operation_timeout(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """Every render lifecycle vendor call in the audio render seam carries
    the measured 120s operation deadline (rerun5: a get_job_status response
    exceeded the 10s transport default mid-render while the job progressed
    healthily — the default starves real renders, not the loop deadline)."""
    from services.mcp_execution.live_handlers.render_poll import (  # noqa: PLC0415
        RENDER_OPERATION_TIMEOUT_SECONDS,
    )

    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["loud_ok"]],
    )
    adapter = _measured_adapter(transport, render_dir)

    adapter("render_boundary_report", "apply_audio_stage", _audio_stage_params("loudness_peak_qc"))

    lifecycle_actions = {
        "prepare_render_job",
        "start",
        "get_job_status",
        "is_rendering",
        "list_jobs",
        "stop",
        "delete_job",
    }
    render_rows = [
        (call, timeout)
        for call, timeout in zip(transport.calls, transport.timeouts, strict=True)
        if call[0] == "render" and call[1] in lifecycle_actions
    ]
    assert render_rows, "the loudness stage must exercise render lifecycle calls"
    assert {timeout for _call, timeout in render_rows} == {RENDER_OPERATION_TIMEOUT_SECONDS}


def test_poll_transport_timeout_stops_and_deletes_the_exact_job(
    tmp_path: Path,
) -> None:
    """A McpTimeoutError on a poll/witness call after the exact job started
    must run the existing exact-job stop+delete cleanup and then fail typed
    retryable — rerun5 measured the raw timeout escaping and orphaning the
    healthy render, which then starved every retry attempt."""
    from services.mcp_client.errors import McpTimeoutError  # noqa: PLC0415
    from services.mcp_execution.live_handlers.render_poll import (  # noqa: PLC0415
        RENDER_OPERATION_TIMEOUT_SECONDS,
    )

    class PollTimeoutTransport(ScriptedTransport):
        def __init__(self) -> None:
            super().__init__(
                {
                    ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
                    ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
                    ("render", "start"): [dict(PROBE_RENDER_START)],
                    ("render", "stop"): [dict(PROBE_RENDER_START)],
                    ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
                }
            )
            self.timed_out = False

        def __call__(
            self,
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            if (tool_name, action) == ("render", "get_job_status") and not self.timed_out:
                self.timed_out = True
                self.calls.append((tool_name, action, dict(normalized_params)))
                self.timeouts.append(timeout_seconds)
                raise McpTimeoutError("render", RENDER_OPERATION_TIMEOUT_SECONDS)
            return super().__call__(
                tool_name, action, normalized_params, timeout_seconds=timeout_seconds
            )

    transport = PollTimeoutTransport()
    adapter = _measured_adapter(transport, tmp_path)

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter(
            "render_boundary_report", "apply_audio_stage", _audio_stage_params("loudness_peak_qc")
        )

    assert excinfo.value.code == "render-poll-timeout"
    assert excinfo.value.retryable is True
    assert "job stopped" in excinfo.value.detail
    assert "job deleted" in excinfo.value.detail
    actions = [c[1] for c in transport.calls]
    assert actions.index("stop") > actions.index("get_job_status")
    assert transport.calls[-1] == ("render", "delete_job", {"job_id": "job-t5-1"})


def test_audio_stage_missing_preset_is_a_permanent_typed_refusal(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """A dialogue-chain stage whose bound preset name is absent from the
    live listing refuses permanently — no apply, no render, no success."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {("resolve_control", "get_fairlight_presets"): [dict(PROBE_PRESETS_EMPTY)]},
        render_dir,
        [t5_media["loud_ok"]],
    )
    adapter = _measured_adapter(transport, render_dir)

    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter(
            "safe_set_audio_properties",
            "apply_audio_stage",
            _audio_stage_params("dialogue_cleanup"),
        )
    assert excinfo.value.code == "preset-missing"
    assert [c[1] for c in transport.calls] == ["get_fairlight_presets"]


def test_audio_stage_apply_false_is_a_typed_failure(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """A preset apply answering success=false (the measured missing-name
    response shape) blocks the stage after the before-render, before any
    after-render or value claim."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("resolve_control", "get_fairlight_presets"): [dict(PROBE_PRESETS_LISTED)],
            ("project_settings", "apply_fairlight_preset"): [dict(PROBE_PRESET_APPLY_FALSE)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["clean_before"]],
    )
    adapter = _measured_adapter(transport, render_dir)

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter(
            "safe_set_audio_properties",
            "apply_audio_stage",
            _audio_stage_params("dialogue_cleanup"),
        )
    assert excinfo.value.code == "apply-fairlight-preset-failed"
    assert len(transport.rendered) == 1


def test_audio_stage_without_measurement_port_refuses_before_transport(
    tmp_path: Path,
) -> None:
    """With no rendered-media measurement port wired, both audio stage
    surfaces refuse typed with zero raw transport calls."""
    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={}, render_dir=str(tmp_path))
    for surface in ("safe_set_audio_properties", "render_boundary_report"):
        with pytest.raises(LiveAdapterError) as excinfo:
            adapter(surface, "apply_audio_stage", _audio_stage_params("dialogue_cleanup"))
        assert excinfo.value.code == "measurement-unavailable"
    assert transport.calls == []


def test_audio_stage_out_of_range_measurement_blocks_completion(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """A render measured outside the plan range blocks the step — the
    measured value is reported in the refusal, never trimmed to pass."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["hot"]],
    )
    adapter = _measured_adapter(transport, render_dir)

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter(
            "render_boundary_report", "apply_audio_stage", _audio_stage_params("loudness_peak_qc")
        )
    assert excinfo.value.code == "audio-stage-out-of-range"
    assert "-4" in excinfo.value.detail


def test_audio_stage_unknown_preset_ref_is_a_typed_refusal(tmp_path: Path) -> None:
    """A stage carrying a preset_ref with no dialogue-chain binding
    refuses typed before any transport call."""
    transport = ScriptedTransport({})
    adapter = _measured_adapter(transport, tmp_path)
    params = _audio_stage_params("dialogue_cleanup")
    params["preset_ref"] = "fairlight-unknown-v9"

    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter("safe_set_audio_properties", "apply_audio_stage", params)
    assert excinfo.value.code == "preset-binding-unknown"
    assert transport.calls == []


def test_audio_plan_steps_route_dialogue_chain_through_the_preset_surface() -> None:
    """Given the real audio finishing ladder, the dialogue cleanup and
    normalization stages compile onto the granular Fairlight preset surface
    with their kit preset_ref; the loudness QC stage keeps the accepted
    delivery-QC surface; unmapped stages keep their executor fallback."""
    plan = build_audio_plan(
        AudioFactsV1(
            episode_id="ep-t5",
            dialogue_clean=False,
            has_bgm=True,
            has_ambience=True,
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=(AudioOpRequestV1(op="voice_isolation"),),
    )
    caps = CapabilityView(
        {
            "voice-isolation": "accepted",
            "advanced-delivery-qc": "accepted",
            "audio-property-operation": "failed",
            "bgm-track-ducking": "failed",
        },
        {},
    )
    steps = audio_plan_steps(plan, caps, 90)
    stages = {
        cast("AudioStageParams", step.normalized_params).stage: step
        for step in steps
        if step.normalized_params.action == "apply_audio_stage"
    }
    for stage in ("dialogue_cleanup", "dialogue_level_normalization"):
        step = stages[stage]
        assert step.tool_surface == "safe_set_audio_properties"
        assert step.rung == "mcp_granular_tool"
        assert step.fallback_record is None
        assert cast("AudioStageParams", step.normalized_params).preset_ref == _PRESET_STAGES[stage]
        assert step.retry_class == "transient"
    loudness = stages["loudness_peak_qc"]
    assert loudness.tool_surface == "render_boundary_report"
    assert loudness.rung == "mcp_verified_workflow"
    assert cast("AudioStageParams", loudness.normalized_params).preset_ref is None
    assert stages["bgm_placement"].tool_surface == "direct_script_adapter"


def test_dialogue_only_audio_plan_compiles_satisfiable_lufs_gates() -> None:
    """A dialogue-only plan must reach execution with the normalization and
    delivery gates over the SAME full-render integrated loudness carrying
    intersecting ranges — disjoint ranges there would be an impossible
    contract (both handlers measure one quantity)."""
    plan = build_audio_plan(
        AudioFactsV1(
            episode_id="ep-t5-do",
            dialogue_clean=False,
            has_bgm=False,
            has_ambience=False,
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
    )
    caps = CapabilityView(
        {
            "voice-isolation": "accepted",
            "advanced-delivery-qc": "accepted",
            "audio-property-operation": "failed",
            "bgm-track-ducking": "failed",
        },
        {},
    )
    steps = audio_plan_steps(plan, caps, 90)
    stage_params = {
        cast("AudioStageParams", step.normalized_params).stage: cast(
            "AudioStageParams", step.normalized_params
        )
        for step in steps
        if step.normalized_params.action == "apply_audio_stage"
    }
    normalization = stage_params["dialogue_level_normalization"]
    qc = stage_params["loudness_peak_qc"]
    assert normalization.metric == "dialogue_loudness"
    assert qc.metric == "integrated_loudness"
    assert (normalization.minimum, normalization.maximum) == (
        qc.minimum,
        qc.maximum,
    )


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 6 — explicit-target DRX exposure correction.
# Real measured media (pinned ffmpeg) behind a scripted render transport:
# the handler's frame evidence is genuinely computed from rendered files;
# only the vendor structure/version/graph/apply responses are scripted.
# ---------------------------------------------------------------------------

PROBE_STRUCT_ITEM = {
    "name": "src-001.mp4",
    "track_type": "video",
    "media_pool_item_name": "src-001.mp4",
}


def _structure_item(item_index: int, start: int, end: int, item_id: str) -> dict[str, object]:
    return {
        **PROBE_STRUCT_ITEM,
        "id": item_id,
        "timeline_item_id": item_id,
        "track_index": 1,
        "item_index": item_index,
        "start": start,
        "end": end,
        "duration": end - start,
    }


def _structure_response(*items: dict[str, object]) -> dict[str, object]:
    return {
        "name": TIMELINE_NAME,
        "id": "tl-struct-1",
        "start_frame": TIMELINE_START,
        "end_frame": TIMELINE_START + 120,
        "start_timecode": "01:00:00:00",
        "item_count": len(items),
        "tracks": {
            "video": {
                "track_count": 1,
                "tracks": [{"track_index": 1, "item_count": len(items), "items": list(items)}],
            }
        },
    }


DRX_STRUCT_TWO_ITEMS = _structure_response(
    _structure_item(0, TIMELINE_START, TIMELINE_START + 60, "ti-color-1"),
    _structure_item(1, TIMELINE_START + 60, TIMELINE_START + 120, "ti-color-2"),
)
PROBE_VERSIONS_FRESH = {"current": "Version 1", "local": [], "remote": [], "errors": []}
PROBE_VERSIONS_APPLIED = {
    "current": "Version 1",
    "local": ["pre-drx-509e70f9", "drx-drx-technical-normalize-v1-509e70f9"],
    "remote": [],
    "errors": [],
}
PROBE_GRAPH_EMPTY = {"available": True, "num_nodes": 0, "nodes": [], "errors": [], "source": "item"}
PROBE_GRAPH_GRADED = {
    "available": True,
    "num_nodes": 1,
    "nodes": [{"node_index": 1, "label": "01", "lut": None, "tools": []}],
    "errors": [],
    "source": "item",
}
PROBE_ADD_VERSION = {"success": True}
DRX_DRY_OK = {"success": True, "path": "staged.drx", "source": "item", "would_apply": True}
_FIXTURE_CONFIRM_TOKEN = "tok-t6-1"  # noqa: S105 - deterministic test fixture token, not a secret
DRX_CONFIRM_REQUIRED = {
    "error": {
        "message": "This action is destructive. Re-call with confirm_token to proceed.",
        "code": "CONFIRMATION_REQUIRED",
        "category": "pending_user_decision",
        "retryable": False,
    },
    "status": "confirmation_required",
    "confirm_token": _FIXTURE_CONFIRM_TOKEN,
    "ttl_seconds": 300,
}
DRX_APPLIED_OK = {"success": True, "path": "staged.drx", "source": "item"}
TRANSFORM_READBACK_UPRIGHT_ZERO = {"success": True, "RotationAngle": 0.0}
TRANSFORM_READBACK_COMPENSATED = {"success": True, "RotationAngle": 90.0}
TRANSFORM_SET_OK = {"success": True}
DRX_TOKEN_INVALID = {
    "error": {
        "message": "confirm_token is invalid, expired, or was issued by a different server.",
        "code": "CONFIRM_TOKEN_INVALID",
        "category": "destructive_blocked",
        "retryable": False,
    }
}


def _t6_media_file(tools, out_dir: Path, name: str, vf: str) -> Path:
    out = out_dir / name
    subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30:d=4",
            "-vf",
            vf,
            "-c:v",
            "h264_videotoolbox",
            "-g",
            "30",
            str(out),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return out


@pytest.fixture(scope="module")
def t6_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Calibrated media pair: ``after`` is graded on frames 0-59 only (the
    target item's span); frames 60-119 (the untargeted item) are input-
    identical with a keyframe at 60 so the change cannot propagate."""

    tools = load_qc_tools()
    root = tmp_path_factory.mktemp("t6-color-media")
    return {
        "before": _t6_media_file(tools, root, "before.mp4", "null"),
        "after": _t6_media_file(tools, root, "after.mp4", "hue=b=0.3:enable='between(n,0,59)'"),
        "changed": _t6_media_file(tools, root, "changed.mp4", "hue=b=0.3"),
    }


def _drx_script(
    *,
    versions: dict[str, object] | None = None,
    apply_queue: list[dict[str, Any]] | None = None,
    graph_queue: list[dict[str, Any]] | None = None,
    structure: dict[str, object] | None = None,
    transform_angles: list[float] | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    angles = (
        [dict(TRANSFORM_READBACK_UPRIGHT_ZERO, RotationAngle=a) for a in transform_angles]
        if transform_angles is not None
        else [dict(TRANSFORM_READBACK_UPRIGHT_ZERO), dict(TRANSFORM_READBACK_COMPENSATED)]
    )
    return {
        ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
        ("project_manager", "create"): [dict(PROBE_CREATE_PROJECT)],
        ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
        ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
        ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
        ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
        ("timeline", "get_current"): [dict(PROBE_GET_CURRENT)],
        ("timeline", "probe_timeline_structure"): [dict(structure or DRX_STRUCT_TWO_ITEMS)],
        ("timeline_item_color", "grade_version_snapshot"): [dict(versions or PROBE_VERSIONS_FRESH)],
        ("timeline_item_color", "probe_node_graph"): list(
            graph_queue or [dict(PROBE_GRAPH_EMPTY), dict(PROBE_GRAPH_GRADED)]
        ),
        ("timeline_item_color", "add_version"): [dict(PROBE_ADD_VERSION)],
        ("timeline_item_color", "safe_apply_drx"): list(
            apply_queue
            if apply_queue is not None
            else [dict(DRX_DRY_OK), dict(DRX_CONFIRM_REQUIRED), dict(DRX_APPLIED_OK)]
        ),
        ("timeline_item", "get_transform"): angles,
        ("timeline_item", "set_transform"): [dict(TRANSFORM_SET_OK)],
        ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
        ("render", "start"): [dict(PROBE_RENDER_START)],
        ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
        ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
    }


def _drx_adapter(transport: ScriptedTransport, render_dir: Path) -> LiveMcpAdapter:
    adapter = LiveMcpAdapter(
        transport,
        media_paths={"src-001": "/media/src-001.mp4"},
        frame_diff=default_frame_comparison(),
        render_dir=str(render_dir),
    )
    adapter("prepare_project", "prepare_project", _prepare_params())
    transport.calls.clear()
    return adapter


def _drx_transport(
    script: dict[tuple[str, str], list[dict[str, Any]]],
    render_dir: Path,
    outputs: list[Path],
) -> RenderingTransport:
    return RenderingTransport(script, render_dir, outputs)


def test_future_drx_apply_targets_explicit_items_with_grade_readback(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """GREEN (Task 6): exposure correction applies the hash-pinned DRX to
    explicit target items through the vendor dry-run → confirmation token
    → apply flow and reads back per-target grade + frame evidence."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(_drx_script(), render_dir, [t6_media["before"], t6_media["after"]])
    adapter = _drx_adapter(transport, render_dir)

    result = _require_native_call(
        lambda: adapter("safe_apply_drx", "apply_color", _color_params()),
        "explicit-target DRX apply with grade readback",
    )

    data = cast("dict[str, object]", result)
    targets = data.get("applied_targets")
    assert isinstance(targets, list), "explicit target readback is required"
    assert targets, "at least one explicit target is required"
    for target in targets:
        entry = cast("dict[str, object]", target)
        item_id = entry.get("item_id")
        assert isinstance(item_id, str), "targets must be explicit item ids"
        assert item_id, "target item ids must be non-empty"
        assert entry.get("grade_version"), "each target must read back a grade version"
    drx_sha = data.get("drx_sha256")
    assert isinstance(drx_sha, str)
    assert len(drx_sha) == 64
    assert all(char in "0123456789abcdef" for char in drx_sha)

    # Hash pinning is real: the readback hash is the committed asset's own.
    from services.foundation_io import sha256_file  # noqa: PLC0415 (single-use proof import)

    kit_drx = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "production-kit"
        / "drx"
        / "technical-normalize-v1.drx"
    )
    assert drx_sha == sha256_file(kit_drx)

    # Vendor flow discipline: dry-run, then unconfirmed call issues the
    # token, then the confirmed apply — every call explicitly addressed.
    drx_calls = [
        call
        for call in transport.calls
        if (call[0], call[1]) == ("timeline_item_color", "safe_apply_drx")
    ]
    assert len(drx_calls) == 3
    dry_params = drx_calls[0][2]
    assert dry_params["dry_run"] is True
    assert "confirm_token" not in dry_params
    assert dry_params["track_type"] == "video"
    assert dry_params["track_index"] == 1
    assert dry_params["item_index"] == 0
    assert str(dry_params["path"]).endswith("technical-normalize-v1.drx")
    confirm_params = drx_calls[1][2]
    assert confirm_params["dry_run"] is False
    assert "confirm_token" not in confirm_params
    applied_params = drx_calls[2][2]
    assert applied_params["confirm_token"] == _FIXTURE_CONFIRM_TOKEN
    # A recoverable pre-grade version and the DRX-identity version exist.
    add_version_names = [
        call[2]["name"]
        for call in transport.calls
        if call[0:2] == ("timeline_item_color", "add_version")
    ]
    assert add_version_names == ["pre-drx-509e70f9", "drx-drx-technical-normalize-v1-509e70f9"]
    entry = cast("dict[str, object]", targets[0])
    assert entry["already_applied"] is False
    assert entry["confirmation"] == {"required": True, "token_issued": True}
    graph = cast("dict[str, object]", entry["graph"])
    assert graph["before_num_nodes"] == 0
    assert graph["after_num_nodes"] == 1
    frame = cast("dict[str, object]", entry["frame"])
    assert cast("float", frame["mean_abs_diff"]) >= 0.5
    assert frame["a_sha256"] != frame["b_sha256"]
    untargeted = cast("list[dict[str, object]]", data["untargeted_frames"])
    assert len(untargeted) == 1
    assert cast("float", untargeted[0]["mean_abs_diff"]) < 0.5


def test_drx_apply_compensates_the_measured_layer_rotation(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """MEASURED 2026-08-29 (T9, pinned Resolve 21.0.4.5 build): the vendor
    safe_apply_drx rotates the target's video layer 90 deg clockwise in the
    composite while Inspector RotationAngle reads 0.0. The adapter must
    restore upright INSIDE the same step through timeline_item.set_transform
    (Inspector +90 in this build), so the production render needs no
    out-of-chain correction; the fresh-render orientation QC fail-closes if
    a build behaves differently."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(_drx_script(), render_dir, [t6_media["before"], t6_media["after"]])
    adapter = _drx_adapter(transport, render_dir)

    result = _require_native_call(
        lambda: adapter("safe_apply_drx", "apply_color", _color_params()),
        "orientation-compensated DRX apply",
    )

    data = cast("dict[str, object]", result)
    entry = cast("dict[str, object]", cast("list[object]", data["applied_targets"])[0])
    compensation = cast("dict[str, object]", entry["orientation_compensation"])
    assert compensation == {"rotation_angle": 90.0, "applied": True}
    sets = [c for c in transport.calls if c[0:2] == ("timeline_item", "set_transform")]
    assert len(sets) == 1, "exactly one compensated vendor write per target"
    assert sets[0][2]["RotationAngle"] == 90.0
    assert sets[0][2]["track_type"] == "video"
    assert sets[0][2]["track_index"] == 1
    assert sets[0][2]["item_index"] == 0
    gets = [c for c in transport.calls if c[0:2] == ("timeline_item", "get_transform")]
    assert len(gets) == 2, "preflight read + post-write readback"


def test_drx_rerun_compensation_is_idempotent(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A rerun over an already-compensated project reads RotationAngle 90
    back and issues ZERO vendor set_transform writes."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(versions=PROBE_VERSIONS_APPLIED, transform_angles=[90.0]),
        render_dir,
        [t6_media["before"], t6_media["before"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    result = adapter("safe_apply_drx", "apply_color", _color_params())

    sets = [c for c in transport.calls if c[0:2] == ("timeline_item", "set_transform")]
    assert sets == [], "compensation must be idempotent on rerun"
    data = cast("dict[str, object]", result)
    entry = cast("dict[str, object]", cast("list[object]", data["applied_targets"])[0])
    compensation = cast("dict[str, object]", entry["orientation_compensation"])
    assert compensation == {"rotation_angle": 90.0, "applied": False}


def test_drx_rerun_detects_identity_version_and_never_reapplies(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A rerun reads back the DRX-identity grade version per target and
    issues ZERO vendor apply calls; the frame stays stable."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(versions=PROBE_VERSIONS_APPLIED),
        render_dir,
        [t6_media["before"], t6_media["before"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    result = adapter("safe_apply_drx", "apply_color", _color_params())

    drx_calls = [
        call
        for call in transport.calls
        if (call[0], call[1]) == ("timeline_item_color", "safe_apply_drx")
    ]
    assert drx_calls == [], "rerun must not re-apply the same DRX identity"
    add_versions = [
        c for c in transport.calls if (c[0], c[1]) == ("timeline_item_color", "add_version")
    ]
    assert add_versions == []
    result_map = cast("dict[str, object]", result)
    applied_targets = cast("list[object]", result_map["applied_targets"])
    entry = cast("dict[str, object]", applied_targets[0])
    assert entry.get("already_applied") is True
    assert entry.get("grade_version") == "drx-drx-technical-normalize-v1-509e70f9"
    frame = cast("dict[str, object]", entry.get("frame"))
    assert cast("float", frame["mean_abs_diff"]) < 0.5
    assert frame["a_sha256"] == frame["b_sha256"]


def test_drx_missing_binding_file_refuses_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = ScriptedTransport(_drx_script())
    adapter = _drx_adapter(transport, tmp_path)
    broken = color.DrxBinding(
        drx_ref="drx-technical-normalize-v1",
        kit_recipe_id="color/technical-normalize",
        drx_path=tmp_path / "nonexistent.drx",
        sha256="0" * 64,
    )
    monkeypatch.setitem(color._DRX_BINDINGS, "drx-technical-normalize-v1", broken)
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "drx-missing"
    assert transport.calls == []


def test_drx_hash_mismatch_refuses_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScriptedTransport(_drx_script())
    adapter = _drx_adapter(transport, tmp_path)
    real = color._DRX_BINDINGS["drx-technical-normalize-v1"]
    tampered = color.DrxBinding(
        drx_ref=real.drx_ref,
        kit_recipe_id=real.kit_recipe_id,
        drx_path=real.drx_path,
        sha256="f" * 64,
    )
    monkeypatch.setitem(color._DRX_BINDINGS, real.drx_ref, tampered)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "drx-hash-mismatch"
    assert transport.calls == []


def test_drx_unknown_ref_and_missing_targets_refuse_before_transport(tmp_path: Path) -> None:
    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    unknown = _color_params()
    unknown["drx_ref"] = "drx-unknown-v9"
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter("safe_apply_drx", "apply_color", unknown)
    assert excinfo.value.code == "drx-binding-unknown"
    unwired = _color_params_unwired()
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo2:
        adapter("safe_apply_drx", "apply_color", unwired)
    assert excinfo2.value.code == "drx-ref-missing"
    transport2 = ScriptedTransport(_drx_script())
    adapter2 = _drx_adapter(transport2, tmp_path)
    empty_targets = _color_params()
    empty_targets["targets"] = []
    with pytest.raises(LiveAdapterError) as excinfo3:
        adapter2("safe_apply_drx", "apply_color", empty_targets)
    assert excinfo3.value.code == "color-targets-missing"
    assert transport.calls == []


def test_drx_wrong_target_span_refuses_typed(tmp_path: Path) -> None:
    """A target whose committed record span matches no placed video item
    (wrong target) is a typed refusal — the vendor is never called."""
    transport = ScriptedTransport(
        _drx_script(
            structure=_structure_response(
                _structure_item(0, TIMELINE_START + 600, TIMELINE_START + 660, "ti-elsewhere")
            )
        )
    )
    adapter = _drx_adapter(transport, tmp_path)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "target-not-placed"
    color_calls = [c for c in transport.calls if c[0] == "timeline_item_color"]
    assert color_calls == []


def test_drx_ambiguous_target_span_refuses_typed(tmp_path: Path) -> None:
    duplicate = _structure_response(
        _structure_item(0, TIMELINE_START, TIMELINE_START + 60, "ti-a"),
        _structure_item(1, TIMELINE_START, TIMELINE_START + 60, "ti-b"),
    )
    transport = ScriptedTransport(_drx_script(structure=duplicate))
    adapter = _drx_adapter(transport, tmp_path)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "target-ambiguous"
    color_calls = [c for c in transport.calls if c[0] == "timeline_item_color"]
    assert color_calls == []


def _card_structure_response(
    *,
    card_source: tuple[int, int] | None = (0, 60),
    primary_source: tuple[int, int] | None = (213, 309),
    overlay: dict[str, object] | None = None,
    overlay_span: tuple[int, int] = (TIMELINE_START, TIMELINE_START + 60),
) -> dict[str, object]:
    """The measured v44-real-01 collision shape: the primary edit-source
    clip on video track 1 AND a native subtitle cue card on track 2 share
    the exact absolute record span (cards sit on top of graded footage).
    The card carries the measured identity fields (media-pool name
    ``{timeline}-cue-{cue_id}``, no source file, overlay track 2);
    ``overlay`` overrides those identity fields for the non-card cases."""

    primary = {
        **PROBE_STRUCT_ITEM,
        "id": "ti-color-1",
        "timeline_item_id": "ti-color-1",
        "track_index": 1,
        "item_index": 0,
        "start": TIMELINE_START,
        "end": TIMELINE_START + 60,
        "duration": 60,
        "source_start": primary_source[0] if primary_source else None,
        "source_end": primary_source[1] if primary_source else None,
        "media_pool_item_name": "edit-source.mov",
    }
    card_name = f"{TIMELINE_NAME}-cue-cue-asr-st3"
    card = {
        **PROBE_STRUCT_ITEM,
        "id": "ti-card-st3",
        "timeline_item_id": "ti-card-st3",
        "track_index": 2,
        "item_index": 0,
        "start": overlay_span[0],
        "end": overlay_span[1],
        "duration": overlay_span[1] - overlay_span[0],
        "source_start": card_source[0] if card_source else None,
        "source_end": card_source[1] if card_source else None,
        "name": card_name,
        "media_pool_item_name": card_name,
        "file_path": None,
        **(overlay or {}),
    }
    return {
        "name": TIMELINE_NAME,
        "id": "tl-struct-1",
        "start_frame": TIMELINE_START,
        "end_frame": TIMELINE_START + 120,
        "start_timecode": "01:00:00:00",
        "item_count": 2,
        "tracks": {
            "video": {
                "track_count": 2,
                "tracks": [
                    {"track_index": 1, "item_count": 1, "items": [primary]},
                    {"track_index": 2, "item_count": 1, "items": [card]},
                ],
            }
        },
    }


def _color_params_with_source_span(start: int = 213, end: int = 309) -> dict[str, object]:
    return {
        **_color_params(),
        "targets": [
            {
                "item_id": "itm-color-1",
                "record_span": {"start_frame": 0, "end_frame": 60},
                "source_span": {
                    "start_frame": start,
                    "end_frame": end,
                    "rate": {"num": 30, "den": 1},
                },
            }
        ],
    }


def test_drx_duplicate_record_span_resolves_by_committed_source_identity(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """Measured on v44-real-01: every color target's record span matches 2
    video items — the placed clip AND the subtitle cue card composited over
    it. The committed source span (the item's cut of the edit mezzanine) is
    the deterministic independent identity: only the placed clip carries it,
    and the vendor apply is addressed to exactly that item — never the first
    duplicate by position."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(structure=_card_structure_response()),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    result = _require_native_call(
        lambda: adapter("safe_apply_drx", "apply_color", _color_params_with_source_span()),
        "duplicate-span resolution by committed source identity",
    )

    data = cast("dict[str, object]", result)
    applied = cast("list[dict[str, object]]", data["applied_targets"])
    assert applied
    assert applied[0]["timeline_item_id"] == "ti-color-1"
    drx_calls = [
        call
        for call in transport.calls
        if (call[0], call[1]) == ("timeline_item_color", "safe_apply_drx")
    ]
    assert len(drx_calls) == 3
    for _tool, _action, params in drx_calls:
        assert params["track_index"] == 1
        assert params["item_index"] == 0


def test_drx_source_identity_tolerates_measured_one_frame_readback_drift(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """Measured on v44-real-01 rerun: target s5's placed clip read back
    source frames [395,464) against the committed [396,465) — the same
    vendor GetSourceStartFrame ±1 drift the placement readback already
    tolerates (SOURCE_READBACK_TOLERANCE_FRAMES). The identity join accepts
    exactly that tolerance; the cue card ([0,69)) stays excluded."""

    render_dir = tmp_path / "render-color"
    structure = _card_structure_response(primary_source=(395, 464), card_source=(0, 69))
    transport = _drx_transport(
        _drx_script(structure=structure),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    result = _require_native_call(
        lambda: adapter("safe_apply_drx", "apply_color", _color_params_with_source_span(396, 465)),
        "one-frame source readback drift tolerance",
    )

    data = cast("dict[str, object]", result)
    applied = cast("list[dict[str, object]]", data["applied_targets"])
    assert applied
    assert applied[0]["timeline_item_id"] == "ti-color-1"
    drx_calls = [
        call
        for call in transport.calls
        if (call[0], call[1]) == ("timeline_item_color", "safe_apply_drx")
    ]
    for _tool, _action, params in drx_calls:
        assert params["track_index"] == 1
        assert params["item_index"] == 0


def test_drx_identity_unmatched_names_the_missing_source_identity(
    tmp_path: Path,
) -> None:
    """When no span hit carries the committed source frames, resolution is
    a typed refusal naming the exact identity join that failed — never a
    positional pick, never a guessed success."""
    structure = _card_structure_response(card_source=(7, 67), primary_source=(7, 67))
    transport = ScriptedTransport(_drx_script(structure=structure))
    adapter = _drx_adapter(transport, tmp_path)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert excinfo.value.code == "target-identity-unmatched"
    assert "source frames [213, 309)" in excinfo.value.detail
    color_calls = [c for c in transport.calls if c[0] == "timeline_item_color"]
    assert color_calls == []


def test_drx_identity_absent_source_frames_refuse_naming_missing_fields(
    tmp_path: Path,
) -> None:
    """A structure readback whose items carry NO source frame identity
    cannot disambiguate duplicate spans; the refusal names the missing
    readback fields (the precise unresolvable-blocker contract)."""
    structure = _card_structure_response(card_source=None, primary_source=None)
    transport = ScriptedTransport(_drx_script(structure=structure))
    adapter = _drx_adapter(transport, tmp_path)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert excinfo.value.code == "target-identity-unmatched"
    assert "source_start" in excinfo.value.detail
    assert "source_end" in excinfo.value.detail
    color_calls = [c for c in transport.calls if c[0] == "timeline_item_color"]
    assert color_calls == []


def test_drx_untargeted_composited_cue_card_is_skipped_with_card_identity(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """Only an independently identified NATIVE SUBTITLE CUE CARD whose
    comparison frame lies inside a targeted span may skip pixel-stability
    comparison: the card composites the graded footage beneath it, so its
    rendered pixels change because the target changed. The skip row names
    the card evidence; the diff gate itself is unchanged."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(structure=_card_structure_response()),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    result = _require_native_call(
        lambda: adapter("safe_apply_drx", "apply_color", _color_params_with_source_span()),
        "composite-aware untargeted verification",
    )

    data = cast("dict[str, object]", result)
    untargeted = cast("list[dict[str, object]]", data["untargeted_frames"])
    assert len(untargeted) == 1
    assert untargeted[0]["timeline_item_id"] == "ti-card-st3"
    assert "skipped" in untargeted[0]
    assert "mean_abs_diff" not in untargeted[0]


def test_drx_untargeted_ordinary_overlay_inside_target_span_still_gated(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """Regression (blanket-span-skip defect): a file-backed overlay clip
    (B-roll/secondary footage) sharing the targeted record span is NOT a
    subtitle cue card — its rendered frame must stay under the codec-noise
    gate, and a change raises the typed failure."""
    render_dir = tmp_path / "render-color"
    structure = _card_structure_response(
        card_source=(500, 560),
        overlay={
            "id": "ti-broll-1",
            "timeline_item_id": "ti-broll-1",
            "name": "broll.mov",
            "media_pool_item_name": "broll.mov",
            "file_path": "/media/broll.mov",
        },
    )
    transport = _drx_transport(
        _drx_script(structure=structure),
        render_dir,
        [t6_media["before"], t6_media["changed"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert excinfo.value.code == "untargeted-frame-changed"
    assert "ti-broll-1" in excinfo.value.detail


@pytest.mark.parametrize(
    "overlay_fields",
    [
        {"media_pool_item_name": None},
        {"media_pool_item_name": "mystery-overlay"},
        {"media_pool_item_name": f"{TIMELINE_NAME}-cue-"},
        {"media_pool_item_name": "edit-source.mov", "file_path": "/media/edit-source.mov"},
        {"track_index": 3},
    ],
    ids=["no-name", "foreign-name", "empty-cue-id", "card-name-with-file", "wrong-track"],
)
def test_drx_untargeted_unknown_overlay_identity_fails_closed(
    tmp_path: Path, t6_media: dict[str, Path], overlay_fields: dict[str, object]
) -> None:
    """An overlay that fails ANY conjunct of the cue-card identity (track,
    no source file, exact card media-pool name with non-empty cue id) is
    never skipped — insufficient identity measures the frame, and a change
    is the typed failure (fail-closed, no blanket skip)."""
    render_dir = tmp_path / "render-color"
    structure = _card_structure_response(
        card_source=(500, 560),
        overlay={"id": "ti-unknown-1", "timeline_item_id": "ti-unknown-1", **overlay_fields},
    )
    transport = _drx_transport(
        _drx_script(structure=structure),
        render_dir,
        [t6_media["before"], t6_media["changed"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert excinfo.value.code == "untargeted-frame-changed"
    assert "ti-unknown-1" in excinfo.value.detail


def test_drx_cue_card_outside_target_spans_is_still_measured(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """The covering-span condition is required even for a true cue card: a
    card composited over UNGRADED footage (outside every targeted span)
    must stay under the codec-noise gate like any other untargeted item."""
    render_dir = tmp_path / "render-color"
    structure = _card_structure_response(
        overlay_span=(TIMELINE_START + 60, TIMELINE_START + 120),
    )
    transport = _drx_transport(
        _drx_script(structure=structure),
        render_dir,
        [t6_media["before"], t6_media["changed"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert excinfo.value.code == "untargeted-frame-changed"
    assert "ti-card-st3" in excinfo.value.detail


def test_drx_apply_marks_the_session_timeline_mutated(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A successful DRX application sets the session mutation flag so the
    native render step cannot silently reuse a render that predates the
    grade (render currency, Task 8)."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(structure=_card_structure_response()),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    assert adapter.timeline_mutated is False
    adapter("safe_apply_drx", "apply_color", _color_params_with_source_span())
    assert adapter.timeline_mutated is True


def test_drx_confirmation_failure_never_becomes_success(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A vendor-rejected confirmation token (invalid/expired) is a typed
    failure; the refused apply is never counted as success."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(
            apply_queue=[dict(DRX_DRY_OK), dict(DRX_CONFIRM_REQUIRED), dict(DRX_TOKEN_INVALID)]
        ),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "drx-apply-failed"
    drx_calls = [
        call
        for call in transport.calls
        if (call[0], call[1]) == ("timeline_item_color", "safe_apply_drx")
    ]
    assert len(drx_calls) == 3
    assert drx_calls[2][2]["confirm_token"] == _FIXTURE_CONFIRM_TOKEN


def test_drx_unchanged_after_frame_never_becomes_success(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """An apply whose after-render is pixel-identical at the target frame
    (a grade with no effect) is a typed failure, not a success."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(),
        render_dir,
        [t6_media["before"], t6_media["before"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "frame-unchanged"


# ---- Task 5 repair 8: probe media is rebuildable — every measurement/
# comparison render is deleted after its evidence is captured (user
# incident: 50 leaked probe videos / 22,421,568,327 bytes in render-audio).


def test_audio_probe_media_is_self_cleaning_after_measurement(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """The QC stage's fresh render must be unlinked once the measurement
    (hash-bound, media-named) is captured — the report keeps the evidence
    computed BEFORE deletion."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["loud_ok"]],
    )
    adapter = _measured_adapter(transport, render_dir)

    result = cast(
        "dict[str, object]",
        adapter(
            "render_boundary_report", "apply_audio_stage", _audio_stage_params("loudness_peak_qc")
        ),
    )

    assert len(transport.rendered) == 1
    assert not transport.rendered[0].exists()
    measurements = cast("dict[str, object]", result["measurements"])
    sha = str(measurements["media_sha256"])
    assert len(sha) == 64
    assert str(measurements["media_name"]).endswith(".mp4")


def test_audio_probe_media_is_self_cleaning_on_measurement_error(
    t5_media: dict[str, Path], tmp_path: Path
) -> None:
    """A malformed measurement port payload is a typed failure AND the
    probe render is still deleted — typed failures must not leak media."""
    render_dir = tmp_path / "t5-render"
    transport = RenderingTransport(
        {
            ("render", "export_render_boundary_report"): [dict(PROBE_BOUNDARY)],
            ("render", "prepare_render_job"): [dict(PROBE_PREPARE_JOB)],
            ("render", "start"): [dict(PROBE_RENDER_START)],
            ("render", "get_job_status"): [dict(PROBE_JOB_DONE)],
            ("render", "delete_job"): [dict(PROBE_RENDER_DELETE)],
        },
        render_dir,
        [t5_media["loud_ok"]],
    )
    adapter = LiveMcpAdapter(
        transport,
        media_paths={},
        audio_measure=lambda _path: {"integrated_loudness_lufs": "not-a-float"},
        render_dir=str(render_dir),
    )

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter(
            "render_boundary_report",
            "apply_audio_stage",
            _audio_stage_params("loudness_peak_qc"),
        )
    assert excinfo.value.code == "measurement-invalid"
    assert len(transport.rendered) == 1
    assert not transport.rendered[0].exists()


def test_color_probe_media_is_self_cleaning_after_evidence(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """After every frame comparison passes, both DRX probe renders are
    deleted while the report keeps the measured frame evidence."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(_drx_script(), render_dir, [t6_media["before"], t6_media["after"]])
    adapter = _drx_adapter(transport, render_dir)

    result = cast(
        "dict[str, object]",
        _require_native_call(
            lambda: adapter("safe_apply_drx", "apply_color", _color_params()),
            "DRX apply with self-cleaning probe media",
        ),
    )

    assert len(transport.rendered) == 2
    assert all(not path.exists() for path in transport.rendered)
    targets = cast("list[dict[str, object]]", result["applied_targets"])
    frame = cast("dict[str, object]", targets[0]["frame"])
    assert "mean_abs_diff" in frame


def test_color_probe_media_is_self_cleaning_on_frame_evidence_failure(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A frame-evidence typed failure must not leak the before/after probe
    renders either."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(),
        render_dir,
        [t6_media["before"], t6_media["before"]],
    )
    adapter = _drx_adapter(transport, render_dir)

    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "frame-unchanged"
    assert len(transport.rendered) == 2
    assert all(not path.exists() for path in transport.rendered)


def test_drx_untargeted_frame_change_never_becomes_success(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A grade that leaks outside the explicit targets (every frame
    changed) is a typed failure."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(),
        render_dir,
        [t6_media["before"], t6_media["changed"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "untargeted-frame-changed"


def test_drx_graph_unchanged_after_apply_never_becomes_success(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """A vendor-successful apply whose node graph did not change is a
    typed failure (probe reachability is not mutation success)."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(graph_queue=[dict(PROBE_GRAPH_EMPTY), dict(PROBE_GRAPH_EMPTY)]),
        render_dir,
        [t6_media["before"], t6_media["after"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "graph-unchanged"


def test_drx_without_frame_port_refuses_before_any_mutation(tmp_path: Path) -> None:
    transport = ScriptedTransport(_drx_script())
    adapter = LiveMcpAdapter(transport, media_paths={"src-001": "/media/src-001.mp4"})
    adapter("prepare_project", "prepare_project", _prepare_params())
    transport.calls.clear()
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "frame-evidence-unavailable"
    assert transport.calls == []


def test_failed_drx_evidence_never_leaves_identity_marker(
    tmp_path: Path, t6_media: dict[str, Path]
) -> None:
    """Regression: a failed graph/frame/untargeted gate must not leave the
    DRX identity version behind, otherwise the next rerun is misread as
    already_applied success."""
    render_dir = tmp_path / "render-color"
    transport = _drx_transport(
        _drx_script(),
        render_dir,
        [t6_media["before"], t6_media["before"]],
    )
    adapter = _drx_adapter(transport, render_dir)
    with pytest.raises(LiveAdapterError) as excinfo:
        adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo.value.code == "frame-unchanged"
    add_names = [
        call[2]["name"]
        for call in transport.calls
        if call[0:2] == ("timeline_item_color", "add_version")
    ]
    assert add_names == ["pre-drx-509e70f9"], "identity must not be recorded before evidence passes"
    assert "drx-drx-technical-normalize-v1-509e70f9" not in add_names
    rerun_transport = _drx_transport(
        _drx_script(),
        tmp_path / "render-color-2",
        [t6_media["before"], t6_media["before"]],
    )
    rerun_adapter = _drx_adapter(rerun_transport, tmp_path / "render-color-2")
    with pytest.raises(LiveAdapterError) as excinfo2:
        rerun_adapter("safe_apply_drx", "apply_color", _color_params())
    assert excinfo2.value.code == "frame-unchanged"
    rerun_adds = [
        call[2]["name"]
        for call in rerun_transport.calls
        if call[0:2] == ("timeline_item_color", "add_version")
    ]
    assert rerun_adds == ["pre-drx-509e70f9"]


def test_color_steps_resolve_exposure_to_pinned_drx_with_explicit_targets() -> None:
    """The compile side resolves the measured exposure section onto the
    guarded DRX surface with the kit binding ref and the explicit target
    item ids derived from the exposure evidence sources; the readback
    preset_ref is the real binding, never the channel-default placeholder."""
    plan = build_color_plan(
        ColorFactsV1(
            episode_id="ep-t6",
            exposure_issues=(ColorIssueV1(source_id="src-001", detail="underexposed"),),
            look_configured=False,
        ),
        policy=ColorPlanPolicy(color_grade_status="accepted", advanced_qc_status="accepted"),
    )
    ir = TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-t6",
        rate=RationalFrameRate(num=30, den=1),
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="trk-1",
                items=(
                    PlacedClipV2(
                        item_id="itm-001",
                        source=SourceRef(
                            source_id="src-001",
                            span=SourceFrameSpan(
                                start_frame=0, end_frame=60, rate=RationalFrameRate(num=30, den=1)
                            ),
                        ),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
                        candidate_ref="cand-1",
                    ),
                ),
            ),
        ),
    )
    caps = CapabilityView({"color-grade-preset-drx": "accepted"}, {})
    steps = color_steps(plan, caps, 60, ir)
    exposure = [
        s
        for s in steps
        if cast("ColorParams", s.normalized_params).section == "technical_correction.exposure"
    ]
    assert len(exposure) == 1
    step = exposure[0]
    assert step.tool_surface == "safe_apply_drx"
    assert step.rung == "mcp_verified_workflow"
    params = cast("ColorParams", step.normalized_params)
    assert params.drx_ref == "drx-technical-normalize-v1"
    assert [t.item_id for t in params.targets] == ["itm-001"]
    assert params.targets[0].record_span.start_frame == 0
    assert params.targets[0].record_span.end_frame == 60
    committed_source = params.targets[0].source_span
    assert committed_source is not None, "targets must carry the committed source span identity"
    assert (committed_source.start_frame, committed_source.end_frame) == (0, 60)
    grade_readback = cast("GradeReadback", step.expected_readback)
    assert grade_readback.preset_ref == "drx-technical-normalize-v1"
    assert step.preconditions.items_placed == ("itm-001",)
    # Without the IR the section compiles but the live handler refuses
    # typed (no explicit targets) — never a guessed success.
    no_ir = color_steps(plan, caps, 60, None)
    assert cast("ColorParams", no_ir[0].normalized_params).targets == ()


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 2 — one source of truth for adapter support.
# The adapter's exported SUPPORTED_SURFACES is the only supported-set
# authority; these tests derive everything else from it and the current
# ToolSurface vocabulary, so any classification gap fails loudly here.
# ---------------------------------------------------------------------------

#: Product surfaces served by non-MCP routes of the retreat ladder
#: (executor scripting, external assets, manual operator work). They are
#: product vocabulary but never pinned-MCP surfaces.
NON_MCP_FALLBACK_SURFACES: frozenset[str] = frozenset(
    {"direct_script_adapter", "external_asset_builder", "manual_operator"}
)


def test_every_tool_surface_is_classified_exactly_once() -> None:
    """Given the current ToolSurface vocabulary and the adapter's exported
    support authority, each surface falls into exactly one of: live
    supported, explicit non-MCP fallback, or known typed unsupported."""

    vocabulary: set[str] = set(get_args(ToolSurface))
    supported: set[str] = set(SUPPORTED_SURFACES)
    fallback: set[str] = set(NON_MCP_FALLBACK_SURFACES)
    known_unsupported = vocabulary - supported - fallback

    assert supported <= vocabulary, "support authority must not outlive the vocabulary"
    assert fallback <= vocabulary, "fallback set must not outlive the vocabulary"
    assert not supported & fallback, "a non-MCP fallback surface cannot also be live"
    assert known_unsupported, "the MCP-unimplemented class must stay non-empty"
    assert not known_unsupported & (supported | fallback)


@pytest.mark.parametrize("surface", sorted(SUPPORTED_SURFACES))
def test_supported_surfaces_route_past_the_refusal_guard(surface: str) -> None:
    """When a surface in the adapter's support authority is called, it
    reaches a real handler (failing on params, state, or the unscripted
    transport) instead of the unsupported-surface refusal."""

    adapter = LiveMcpAdapter(ScriptedTransport({}), media_paths={})
    try:
        adapter(surface, "classify_probe", {})
    except LiveAdapterUnsupportedError as error:
        pytest.fail(f"{surface!r} is in SUPPORTED_SURFACES but was refused ({error.code})")
    except (LiveAdapterError, AssertionError):
        pass


@pytest.mark.parametrize("surface", sorted(KNOWN_NOT_LIVE_SURFACES))
def test_known_not_live_surfaces_refuse_with_dedicated_code_before_transport(
    surface: str,
) -> None:
    """When a known product surface without a live handler is called (even
    with future-shaped params), it fails closed with the dedicated
    known-not-live code and issues zero raw transport calls."""

    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter(surface, "apply_color", _color_params())
    assert excinfo.value.code == "surface-not-live"
    assert transport.calls == []


@pytest.mark.parametrize(
    "surface",
    ["", "PREPARE_PROJECT", "prepare_projectx", "safe_import_media ", "not_a_surface"],
)
def test_unknown_surface_names_refuse_with_distinct_code_before_transport(
    surface: str,
) -> None:
    """When a name outside the ToolSurface vocabulary is called, it fails
    closed with a code distinct from the known-not-live refusal and issues
    zero raw transport calls."""

    transport = ScriptedTransport({})
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        adapter(surface, "apply_color", _color_params())
    assert excinfo.value.code == "surface-unknown"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 3 — adapter refactor for measured handlers.
# LiveMcpAdapter stays the only public product mutation adapter; the domain
# handlers it dispatches to live in live_handlers/, share one typed session
# context, and only ever call the vendor transport with vendor tool names.
# These tests pin the architecture, not just the frozen behavior above.
# ---------------------------------------------------------------------------

_ADAPTER_CLASS_RE = re.compile(r"^\s*class\s+(\w*Adapter)\b", re.MULTILINE)
_TRANSPORT_TOOL_RE = re.compile(r"\b(?:transport|_raw)\(\s*[\"']([^\"']+)[\"']")

#: Vendor compound tools the pinned MCP actually exposes (probe-evidenced).
#: Handler/adapter source may only open a raw call with one of these names,
#: so a logical product surface can never reach the transport statically.
_VENDOR_TOOLS: frozenset[str] = frozenset(
    {
        "project_manager",
        "project_settings",
        "media_pool",
        "timeline",
        "timeline_item",
        "fusion_comp",
        "resolve_control",
        "render",
        "timeline_item_color",
    }
)

_MCP_EXECUTION_DIR = Path(common.__file__).resolve().parent.parent
_LIVE_HANDLERS_DIR = Path(common.__file__).resolve().parent


def test_live_handlers_split_domains_with_bounded_seams() -> None:
    """The live handlers package carries one module per finishing domain;
    native render is now wired (Task 7); no other empty seams remain."""

    assert callable(placement.prepare_project)
    assert callable(placement.safe_import_media)
    assert callable(placement.append_to_timeline)
    assert callable(audio.set_voice_isolation_state)
    assert callable(audio.apply_dialogue_preset)
    assert callable(audio.measure_audio_stage)
    assert callable(subtitle.apply_subtitles)
    assert callable(color.apply_drx_grade)
    assert callable(render.render_native)
    public_names = {name for name in vars(render) if not name.startswith("__")}
    assert "render_native" in public_names


def test_handler_registry_routes_exactly_the_supported_surfaces() -> None:
    """Every surface in the adapter's support authority has exactly one
    routed handler and the registry routes nothing beyond it."""

    assert frozenset(HANDLERS) == frozenset(SUPPORTED_SURFACES)


def test_handlers_share_the_session_context_signature() -> None:
    """Handlers are context-consuming functions (ctx first), not adapters
    that own their own transport or state."""

    for surface, handler in HANDLERS.items():
        parameters = list(__import__("inspect").signature(handler).parameters)
        assert parameters[0] == "ctx", surface


def test_live_mcp_adapter_is_the_only_adapter_class_in_mcp_execution() -> None:
    """No second product mutation adapter/dispatcher class exists anywhere
    in the mcp_execution production tree."""

    found: dict[str, str] = {}
    for path in sorted(_MCP_EXECUTION_DIR.rglob("*.py")):
        for name in _ADAPTER_CLASS_RE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, str(path.relative_to(_MCP_EXECUTION_DIR)))
    assert set(found) == {"LiveMcpAdapter"}
    assert found["LiveMcpAdapter"] == "live_adapter.py"


def test_live_handlers_are_consumed_only_by_the_public_adapter() -> None:
    """Production code outside the handler package reaches handlers only
    through LiveMcpAdapter; handlers are not a second public entry point."""

    services_root = _MCP_EXECUTION_DIR.parent
    offenders: list[str] = []
    for path in sorted(services_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "live_handlers" not in source:
            continue
        if path.parent == _LIVE_HANDLERS_DIR:
            continue  # the package wires its own modules
        if path == _MCP_EXECUTION_DIR / "live_adapter.py":
            continue  # the one public dispatcher
        offenders.append(str(path.relative_to(services_root)))
    assert offenders == []


def test_handler_and_adapter_transport_calls_use_vendor_tools_only() -> None:
    """Every raw transport call opened in handler/adapter source names a
    vendor tool, so logical product surface names never reach transport."""

    called: set[str] = set()
    scanned = [
        *sorted(_LIVE_HANDLERS_DIR.glob("*.py")),
        _MCP_EXECUTION_DIR / "live_adapter.py",
    ]
    for path in scanned:
        called |= set(_TRANSPORT_TOOL_RE.findall(path.read_text(encoding="utf-8")))
    assert called
    assert called <= _VENDOR_TOOLS


def test_adapter_state_is_one_typed_session_context() -> None:
    """All mutable adapter session state lives on the shared typed context;
    the adapter itself holds exactly that one context."""

    adapter = LiveMcpAdapter(ScriptedTransport({}), media_paths={})
    attributes = set(vars(adapter))
    assert attributes == {"_ctx"}
    assert isinstance(vars(adapter)["_ctx"], LiveSessionContext)


def test_session_context_normalizes_path_keyed_media_mappings() -> None:
    """The shared context accepts id→path or path→id media mappings and
    always stores id→path (the pre-refactor adapter behavior)."""

    by_id = LiveSessionContext.build(ScriptedTransport({}), {"src-001": "/media/a.mp4"})
    assert by_id.media_paths == {"src-001": "/media/a.mp4"}
    assert by_id.source_paths == {"src-001": "/media/a.mp4"}
    by_path = LiveSessionContext.build(ScriptedTransport({}), {"/media/a.mp4": "src-001"})
    assert by_path.media_paths == {"src-001": "/media/a.mp4"}
    assert by_path.source_paths == {"src-001": "/media/a.mp4"}


#: get_current readback values that cannot appear in the request params, so
#: any assertion against them proves readback sourcing, not echo.
_READBACK_TIMELINE = {
    "name": "tl-readback-name",
    "id": "tl-readback-id",
    "start_frame": TIMELINE_START,
    "end_frame": TIMELINE_START,
    "start_timecode": "01:00:00:00",
    "success": True,
}


def _session_context(adapter: LiveMcpAdapter) -> LiveSessionContext:
    ctx = vars(adapter)["_ctx"]
    assert isinstance(ctx, LiveSessionContext)
    return ctx


def test_prepare_records_project_and_timeline_identity_from_readback_on_create() -> None:
    """Given the create path (project absent), the shared context records
    project identity from the create readback and timeline identity/name
    from the independent get_current readback; the caller-visible result
    keeps its original shape (requested project name)."""

    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_MISSING)],
            ("project_manager", "create"): [{"name": "ep-readback-project", "success": True}],
            ("project_settings", "set_setting"): [dict(PROBE_SET_FPS)],
            ("project_settings", "get_setting"): [dict(PROBE_GET_PERF_CACHE)],
            ("media_pool", "create_timeline"): [dict(PROBE_CREATE_TIMELINE)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(_READBACK_TIMELINE)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    result = adapter("prepare_project", "prepare_project", _prepare_params())

    ctx = _session_context(adapter)
    assert ctx.current_project_name == "ep-readback-project"
    assert ctx.current_timeline_name == "tl-readback-name"
    assert ctx.current_timeline_id == "tl-readback-id"
    assert ctx.timeline_start == TIMELINE_START
    assert isinstance(result, dict)
    assert result["project_name"] == TIMELINE_NAME


def test_prepare_records_identity_on_resume_with_requested_project_name() -> None:
    """Given the resume path (load ok), the context's project identity is
    the validated requested name (the load response exposes none) while
    timeline identity/name still come from the get_current readback."""

    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_OK)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(_READBACK_TIMELINE)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    adapter("prepare_project", "prepare_project", _prepare_params())

    ctx = _session_context(adapter)
    assert ctx.current_project_name == TIMELINE_NAME
    assert ctx.current_timeline_name == "tl-readback-name"
    assert ctx.current_timeline_id == "tl-readback-id"


def test_prepare_failure_leaves_context_identity_unset() -> None:
    """When the flow fails at the final get-current readback, no identity
    reaches the shared context: only fully validated state is recorded."""

    transport = ScriptedTransport(
        {
            ("project_manager", "load"): [dict(PROBE_LOAD_OK)],
            ("timeline", "set_current"): [dict(PROBE_SET_CURRENT)],
            ("timeline", "get_current"): [dict(PROBE_SET_CURRENT_MISSING)],
        }
    )
    adapter = LiveMcpAdapter(transport, media_paths={})
    with pytest.raises(LiveAdapterError):
        adapter("prepare_project", "prepare_project", _prepare_params())

    ctx = _session_context(adapter)
    assert ctx.current_project_name is None
    assert ctx.current_timeline_name is None
    assert ctx.current_timeline_id is None
    assert ctx.timeline_start is None
