"""Contract tests for MCP response normalization (task 9).

Covers the task-9 acceptance surface: every seed fixture normalizes into its
typed model and round-trips byte-stably, missing required keys raise
``NormalizationError`` naming the key, unknown keys are never silently
dropped, non-object payloads are refused, and the live recorder collects as
skipped with an explicit reason unless ``-m mcp_live`` selected it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from services.mcp_client.response_normalize import (
    NormalizationError,
    normalize_deep_shot_analysis,
    normalize_media_analysis_standard,
    normalize_resolve_version,
    normalize_server_info,
    normalize_tools_list,
)
from tests.mcp_client.record_live_fixtures import (
    PLANNED_FIXTURES,
    canonical_fixture_bytes,
)

FIXTURES_DIR: Final = Path(__file__).resolve().parent / "fixtures"
VIDEO_PIPELINE_ROOT: Final = Path(__file__).resolve().parents[2]
RECORDER_PATH: Final = Path(__file__).resolve().parent / "record_live_fixtures.py"

SERVER_INFO: Final = "server-info.json"
TOOLS_LIST: Final = "tools-list.json"
RESOLVE_VERSION: Final = "resolve-version.json"
MEDIA_ANALYSIS_STANDARD: Final = "media-analysis-standard.json"
DEEP_SHOT_ANALYSIS: Final = "deep-shot-analysis.json"
MEDIA_ANALYSIS_STANDARD_RECORDED: Final = "media-analysis-standard.recorded.json"
DEEP_SHOT_ANALYSIS_RECORDED: Final = "deep-shot-analysis.recorded.json"

ALL_FIXTURES: Final = (
    SERVER_INFO,
    TOOLS_LIST,
    RESOLVE_VERSION,
    MEDIA_ANALYSIS_STANDARD,
    DEEP_SHOT_ANALYSIS,
)

RECORDED_FIXTURES: Final = (
    MEDIA_ANALYSIS_STANDARD_RECORDED,
    DEEP_SHOT_ANALYSIS_RECORDED,
)

# The pinned compound server (davinci-resolve-mcp 2.207.0) tools/list roster,
# recorded live by the task-11 recorder.  A roster change means a server
# version change: re-record and update this tuple together with the pin.
PINNED_SERVER_TOOLS: Final = (
    "color_group",
    "dctl",
    "edit_engine",
    "folder",
    "fuse_plugin",
    "fusion_comp",
    "gallery",
    "gallery_stills",
    "graph",
    "layout_presets",
    "media_analysis",
    "media_pool",
    "media_pool_item",
    "media_pool_item_markers",
    "media_storage",
    "project_manager",
    "project_manager_cloud",
    "project_manager_database",
    "project_manager_folders",
    "project_settings",
    "render",
    "render_presets",
    "resolve_control",
    "script_plugin",
    "setup",
    "timeline",
    "timeline_ai",
    "timeline_frame",
    "timeline_item",
    "timeline_item_color",
    "timeline_item_fusion",
    "timeline_item_markers",
    "timeline_item_takes",
    "timeline_markers",
    "timeline_versioning",
)

ROGUE_KEY: Final = "__rogue_key__"


def _load_fixture(name: str) -> dict[str, object]:
    payload: object = json.loads((FIXTURES_DIR / name).read_bytes())
    assert isinstance(payload, dict)
    return payload


def _child(node: object, step: str | int) -> object:
    if isinstance(step, int):
        assert isinstance(node, list)
        return node[step]
    assert isinstance(node, dict)
    return node[step]


def _drop_key(payload: object, path: tuple[str | int, ...]) -> None:
    """Remove the key at *path* from a decoded fixture (in place)."""
    node: object = payload
    for step in path[:-1]:
        node = _child(node, step)
    last = path[-1]
    if isinstance(last, str) and isinstance(node, dict):
        del node[last]
        return
    if isinstance(last, int) and isinstance(node, list):
        del node[last]
        return
    raise AssertionError(f"cannot drop {path!r} from {type(node).__name__}")


def _add_key(payload: object, path: tuple[str | int, ...]) -> None:
    """Add a rogue key at *path*'s parent container (in place)."""
    node: object = payload
    for step in path:
        node = _child(node, step)
    assert isinstance(node, dict), f"expected dict at {path!r}"
    node[ROGUE_KEY] = 1


def _assert_round_trip(
    normalizer: Callable[[object], object],
    payload: dict[str, object],
    model: object,
) -> None:
    dumped: object = model.model_dump(mode="json", by_alias=True)  # type: ignore[attr-defined]
    assert dumped == payload
    assert normalizer(dumped) == model


# ---------------------------------------------------------------------------
# (a) seed fixture → typed model round-trip
# ---------------------------------------------------------------------------


def test_server_info_fixture_round_trips() -> None:
    payload = _load_fixture(SERVER_INFO)
    identity = normalize_server_info(payload)
    assert identity.name == "DaVinciResolveMCP"
    assert identity.version == "1.29.1"
    _assert_round_trip(normalize_server_info, payload, identity)


def test_tools_list_fixture_round_trips() -> None:
    payload = _load_fixture(TOOLS_LIST)
    listing = normalize_tools_list(payload)
    names = {tool.name for tool in listing.tools}
    assert names == set(PINNED_SERVER_TOOLS)
    media_analysis = next(tool for tool in listing.tools if tool.name == "media_analysis")
    assert media_analysis.input_schema["type"] == "object"
    resolve_control = next(tool for tool in listing.tools if tool.name == "resolve_control")
    assert resolve_control.description is not None
    _assert_round_trip(normalize_tools_list, payload, listing)


def test_resolve_version_fixture_matches_live_evidence_shape() -> None:
    payload = _load_fixture(RESOLVE_VERSION)
    report = normalize_resolve_version(payload)
    # Values mirror the raw live payload recorded in task-7 live-smoke.txt.
    assert report.product == "DaVinci Resolve Studio"
    assert report.version == (21, 0, 4, 5, "")
    assert report.version_string == "21.0.4.5"
    assert report.build.known_gates == 41
    assert report.build.unavailable_on_this_build == ()
    assert report.mcp.version == "2.207.0"
    assert report.mcp.update.update_mode == "never"
    assert report.mcp.update_decision.action == "none"
    _assert_round_trip(normalize_resolve_version, payload, report)


def test_dual_envelope_operation_key_is_stripped_at_the_boundary() -> None:
    """v2.207.0 `dual` mode adds `_operation` to every payload; strict models
    must not see it (measured live 2026-09-05: get_version carries it)."""
    payload = _load_fixture(RESOLVE_VERSION)
    enveloped = {**payload, "_operation": {
        "status": "success", "operation": "resolve_control.get_version",
        "execution_id": "exec_probe000000",
    }}
    report = normalize_resolve_version(enveloped)
    assert report.mcp.version == "2.207.0"
    assert normalize_resolve_version(payload) == report


def test_media_analysis_standard_fixture_round_trips() -> None:
    payload = _load_fixture(MEDIA_ANALYSIS_STANDARD)
    report = normalize_media_analysis_standard(payload)
    assert report.success is True
    assert report.provider == "host_chat_paths"
    assert report.schema_version == "2.0"
    assert report.editorial_classification.select_potential == "high"
    assert len(report.shot_descriptions) == 2
    first = report.shot_descriptions[0]
    assert first.shot_index == 1
    assert first.frame_indices_used == (1, 2, 3)
    assert first.visual.shot_size == "medium_close"
    assert first.visual.camera_motion == "locked"
    assert first.content.action == "Host introduces the episode."
    assert first.editorial.editorial_role == "coverage"
    assert first.editorial.best_moment is not None
    assert first.editorial.best_moment.time_seconds == 1.4
    assert first.cuttability.cut_in.quality == "clean"
    assert first.confidence.audio == "medium"
    second = report.shot_descriptions[1]
    assert second.editorial.best_moment_present is False
    assert second.editorial.best_moment is None
    _assert_round_trip(normalize_media_analysis_standard, payload, report)


def test_deep_shot_analysis_fixture_round_trips() -> None:
    payload = _load_fixture(DEEP_SHOT_ANALYSIS)
    report = normalize_deep_shot_analysis(payload)
    assert len(report.shots) == 2
    first = report.shots[0]
    assert first.shot_index == 1
    assert first.time_seconds_start == 0.0
    assert first.time_seconds_end == 4.2
    assert first.frame_indices == (0, 12, 24)
    assert first.visual.shot_size == "medium_close"
    assert first.visual.camera_motion == "handheld"
    assert first.content.primary_subject.type == "person"
    assert first.content.action == "Host reacts to the take."
    assert first.editorial.editorial_role == "reaction"
    assert first.editorial.select_potential == "high"
    assert first.editorial.pacing == "still"
    assert first.editorial.stillness_type == "held_tension"
    assert first.editorial.best_moment is not None
    assert first.cuttability.match_action_out is True
    assert first.confidence.audio == "low"
    second = report.shots[1]
    assert second.editorial.best_moment_present is False
    assert second.editorial.best_moment is None
    assert second.editorial.stillness_type is None
    _assert_round_trip(normalize_deep_shot_analysis, payload, report)


@pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
def test_seed_fixtures_are_canonical_and_byte_stable(fixture_name: str) -> None:
    raw = (FIXTURES_DIR / fixture_name).read_bytes()
    parsed: object = json.loads(raw)
    assert canonical_fixture_bytes(parsed) == raw


# ---------------------------------------------------------------------------
# (a2) recorded live analysis payloads — provenance + by-design divergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", RECORDED_FIXTURES)
def test_recorded_analysis_fixtures_are_canonical_with_provenance(
    fixture_name: str,
) -> None:
    raw = (FIXTURES_DIR / fixture_name).read_bytes()
    parsed: object = json.loads(raw)
    assert canonical_fixture_bytes(parsed) == raw
    meta: object = json.loads((FIXTURES_DIR / ".recording-meta.json").read_bytes())
    assert isinstance(meta, dict)
    entry = meta.get(fixture_name)
    assert isinstance(entry, dict)
    assert entry["server_identity"]["name"] == "DaVinciResolveMCP"


def test_recorded_stored_visual_is_rejected_by_strict_standard_model() -> None:
    payload = _load_fixture(MEDIA_ANALYSIS_STANDARD_RECORDED)
    with pytest.raises(NormalizationError) as excinfo:
        normalize_media_analysis_standard(payload)
    message = str(excinfo.value)
    assert "unknown key" in message
    assert "slate" in message or "clip_summary" in message


def test_recorded_deep_shots_are_rejected_by_strict_deep_model() -> None:
    payload = _load_fixture(DEEP_SHOT_ANALYSIS_RECORDED)
    with pytest.raises(NormalizationError) as excinfo:
        normalize_deep_shot_analysis(payload)
    message = str(excinfo.value)
    assert "shot_uuid" in message


# ---------------------------------------------------------------------------
# (b) missing required key → NormalizationError naming the key
# ---------------------------------------------------------------------------

MISSING_KEY_CASES: Final = (
    (SERVER_INFO, normalize_server_info, ("version",)),
    (TOOLS_LIST, normalize_tools_list, ("tools",)),
    (TOOLS_LIST, normalize_tools_list, ("tools", 0, "name")),
    (RESOLVE_VERSION, normalize_resolve_version, ("version_string",)),
    (RESOLVE_VERSION, normalize_resolve_version, ("build", "known_gates")),
    (
        MEDIA_ANALYSIS_STANDARD,
        normalize_media_analysis_standard,
        ("editorial_classification",),
    ),
    (
        MEDIA_ANALYSIS_STANDARD,
        normalize_media_analysis_standard,
        ("shot_descriptions", 0, "editorial"),
    ),
    (DEEP_SHOT_ANALYSIS, normalize_deep_shot_analysis, ("shots",)),
    (DEEP_SHOT_ANALYSIS, normalize_deep_shot_analysis, ("shots", 0, "cuttability")),
)


@pytest.mark.parametrize(("fixture_name", "normalizer", "key_path"), MISSING_KEY_CASES)
def test_missing_required_key_raises_error_naming_the_key(
    fixture_name: str,
    normalizer: Callable[[object], object],
    key_path: tuple[str | int, ...],
) -> None:
    payload = _load_fixture(fixture_name)
    _drop_key(payload, key_path)
    with pytest.raises(NormalizationError) as excinfo:
        normalizer(payload)
    message = str(excinfo.value)
    assert str(key_path[-1]) in message
    assert "missing" in message


# ---------------------------------------------------------------------------
# (c) unknown extra key → NormalizationError (no silent drop)
# ---------------------------------------------------------------------------

UNKNOWN_KEY_CASES: Final = (
    (SERVER_INFO, normalize_server_info, ()),
    (TOOLS_LIST, normalize_tools_list, ()),
    (TOOLS_LIST, normalize_tools_list, ("tools", 0)),
    (RESOLVE_VERSION, normalize_resolve_version, ()),
    (RESOLVE_VERSION, normalize_resolve_version, ("mcp", "update")),
    (MEDIA_ANALYSIS_STANDARD, normalize_media_analysis_standard, ()),
    (
        MEDIA_ANALYSIS_STANDARD,
        normalize_media_analysis_standard,
        ("shot_descriptions", 0, "visual"),
    ),
    (DEEP_SHOT_ANALYSIS, normalize_deep_shot_analysis, ()),
    (DEEP_SHOT_ANALYSIS, normalize_deep_shot_analysis, ("shots", 0, "editorial")),
)


@pytest.mark.parametrize(("fixture_name", "normalizer", "key_path"), UNKNOWN_KEY_CASES)
def test_unknown_extra_key_raises_error_naming_the_key(
    fixture_name: str,
    normalizer: Callable[[object], object],
    key_path: tuple[str | int, ...],
) -> None:
    payload = _load_fixture(fixture_name)
    _add_key(payload, key_path)
    with pytest.raises(NormalizationError) as excinfo:
        normalizer(payload)
    message = str(excinfo.value)
    assert ROGUE_KEY in message
    assert "unknown" in message


@pytest.mark.parametrize("payload", [[1, 2], "text", None, 3])
def test_non_object_payload_raises_normalization_error(payload: object) -> None:
    with pytest.raises(NormalizationError):
        normalize_server_info(payload)


# ---------------------------------------------------------------------------
# (d) the mcp_live recorder collects as skipped by default, reason explicit
# ---------------------------------------------------------------------------


def test_recorder_plan_matches_live_and_recorded_sets() -> None:
    live_replaced = {
        SERVER_INFO,
        TOOLS_LIST,
        RESOLVE_VERSION,
    }
    assert frozenset(RECORDED_FIXTURES) | live_replaced == PLANNED_FIXTURES


def test_recorder_skips_with_explicit_reason_outside_mcp_live_selection() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(RECORDER_PATH),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        cwd=str(VIDEO_PIPELINE_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "1 skipped" in combined
    assert "-m mcp_live" in combined
