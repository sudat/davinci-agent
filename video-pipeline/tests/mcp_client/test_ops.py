"""McpOps typed-surface contract against the fake stdio MCP server.

Every ops method must round-trip its canned LIVE-shaped payload into the
frozen result model from ``services.mcp_client.ops_models`` — including the
error-envelope path (``ok`` false with the typed code) — proving the probe
surface parses real wire shapes instead of trusting console claims.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator, Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.mcp_client.client import McpClient
from services.mcp_client.ops import McpOps
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_server.py"


@pytest.fixture
def ops() -> Generator[McpOps, None, None]:
    config = StdioTransportConfig(command=(sys.executable, str(FAKE_SERVER_PATH)))
    client = McpClient(StdioJsonRpcTransport(config))
    with client:
        client.connect()
        yield McpOps(client)


def test_project_and_timeline_results_are_typed(ops: McpOps) -> None:
    project = ops.create_project("probe-unit")
    assert project.ok is True
    assert project.name == "Fake Project"
    clip_info = {
        "clip_id": "mpi-1",
        "start_frame": 0,
        "end_frame": 60,
        "record_frame": 0,
        "track_index": 1,
    }
    timeline = ops.create_timeline_from_clips("Fake Timeline", [clip_info])
    assert timeline.ok is True
    assert timeline.id == "tl-fake-1"
    assert timeline.created_new is True


def test_import_media_returns_typed_clip_summaries(ops: McpOps) -> None:
    imported = ops.safe_import_media(["media/a.mp4", "media/b.mp4"])
    assert imported.ok is True
    assert imported.imported == 2
    assert [clip.name for clip in imported.clips] == ["parity-src-001", "parity-src-002"]
    assert imported.clips[0].clip_id == "mpi-1"


def test_timeline_structure_snapshot_parses_tracks_and_items(ops: McpOps) -> None:
    snapshot = ops.timeline_structure()
    assert snapshot.ok is True
    assert snapshot.start_frame == 108000
    assert snapshot.end_frame == 108210
    video_group = snapshot.tracks["video"]
    assert video_group.track_count == 1
    item = video_group.tracks[0].items[0]
    assert item.source_start == 0
    assert item.source_end == 60
    assert item.source_fps == 30.0
    assert item.media_pool_item_name == "parity-src-001"
    audio_item = snapshot.tracks["audio"].tracks[0].items[0]
    assert audio_item.track_type == "audio"
    assert audio_item.start == 108000


def test_transform_readback_is_typed(ops: McpOps) -> None:
    transform = ops.get_transform()
    assert transform.ZoomX == 1.2
    assert transform.Pan == 0.0


def test_render_lifecycle_results_are_typed(ops: McpOps) -> None:
    added = ops.render_add_job()
    assert added.job_id == "b5075b6c-53e1-44f2-be02-0377fb6a8ae7"
    status = ops.render_job_status("b5075b6c-53e1-44f2-be02-0377fb6a8ae7")
    assert status.job_status == "Complete"
    assert status.completion_percentage == 100
    jobs = ops.render_list_jobs()
    assert len(jobs.jobs) == 1
    settings = ops.render_get_settings()
    assert settings.FormatWidth == 1920
    assert settings.AudioSampleRate == 48000
    format_codec = ops.render_get_format_and_codec()
    assert format_codec.format == "mp4"
    assert format_codec.codec == "h264"


def test_error_envelope_flows_into_typed_outcome(ops: McpOps) -> None:
    outcome = ops.safe_apply_drx("fixtures/does-not-matter.drx")
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code == "UNKNOWN_ACTION"


def test_resolve_version_uses_live_shape(ops: McpOps) -> None:
    report = ops.client.resolve_get_version()
    assert report.version_string == "21.0.4.5"
    assert report.mcp.update.update_mode == "never"


# ---------------------------------------------------------------------------
# Task 4: subtitle-construction ops (wire shapes recorded by the live probe
# under capabilities/v4.4/probes/task4-native-subtitle, Resolve 21.0.4.5).
# ---------------------------------------------------------------------------

#: Responses captured from the probe ledger for one cue's creation flow.
_TASK4_WIRE: dict[tuple[str, str], object] = {
    ("timeline", "get_track_count"): {"count": 1, "success": True},
    ("timeline", "add_track"): {"success": True},
    (
        "timeline",
        "get_items_in_track",
    ): {
        "items": [
            {
                "name": "probe-t4sub-tl-cue-c1",
                "id": "247eb5d3-4d15-47c9-90f9-229b78a13882",
                "start": 108015,
                "end": 108045,
                "duration": 30,
            }
        ]
    },
    ("timeline", "insert_fusion_title"): {"success": True},
    (
        "fusion_comp",
        "safe_set_inputs",
    ): {
        "success": True,
        "tool_name": "Template",
        "results": {
            "StyledText": {
                "success": True,
                "value": "PROBE-CUE-1 今日はDaVinci Resolveの使い方を紹介します",
            },
            "Size": {"success": True, "value": 0.08},
            "Center": {"success": True, "value": {"1": 0.5, "2": 0.9}},
        },
    },
    (
        "fusion_comp",
        "get_text_plus",
    ): {
        "tool_name": "Template",
        "input_name": "StyledText",
        "text": "PROBE-CUE-1 今日はDaVinci Resolveの使い方を紹介します",
    },
    ("fusion_comp", "get_input"): {"value": "Hiragino Sans W3"},
    ("timeline", "get_media_pool_item"): {
        "name": "probe-t4sub-tl-cue-c1",
        "id": "3d159a0f-55f5-4635-a7d8-facd7f5fba58",
    },
    (
        "timeline",
        "subtitle_generation_probe",
    ): {"would_generate": True, "settings": {"language": "auto"}, "success": True},
}


class _ProbeWireTransport(StdioJsonRpcTransport):
    """In-process transport answering from recorded probe wire shapes."""

    def __init__(self, wire: Mapping[tuple[str, str], object]) -> None:
        # Manual (non-pin) config: in-process wire transport, never spawned.
        super().__init__(StdioTransportConfig(command=("in-process-wire",)))
        self._wire = dict(wire)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send_notification(
        self, method: str, params: Mapping[str, object] | None = None
    ) -> None:
        pass

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if method != "tools/call" or params is None:
            return {}
        name = str(params.get("name"))
        arguments = params.get("arguments")
        action = str(arguments.get("action", "")) if isinstance(arguments, dict) else ""
        canned = self._wire.get((name, action))
        if canned is None:
            canned = {
                "error": {
                    "message": f"unknown action: {action}",
                    "code": "UNKNOWN_ACTION",
                }
            }
        text = json.dumps(canned)
        return {"result": {"content": [{"type": "text", "text": text}], "isError": False}}


@pytest.fixture
def task4_ops() -> Generator[McpOps, None, None]:
    client = McpClient(_ProbeWireTransport(_TASK4_WIRE))
    yield McpOps(client)
    client.close()


def test_subtitle_construction_ops_parse_probe_wire_shapes(task4_ops: McpOps) -> None:
    count = task4_ops.get_track_count("video")
    assert count.ok is True
    assert count.count == 1

    added = task4_ops.add_video_track()
    assert added.ok is True

    items = task4_ops.get_items_in_track("video", 2)
    assert items.ok is True
    assert len(items.items) == 1
    assert items.items[0].start == 108015
    assert items.items[0].end == 108045
    assert items.items[0].duration == 30

    title = task4_ops.insert_fusion_title("Text+")
    assert title.ok is True

    inputs = task4_ops.set_fusion_inputs(
        "Template",
        {"StyledText": "PROBE-CUE-1 今日はDaVinci Resolveの使い方を紹介します", "Size": 0.08},
    )
    assert inputs.ok is True
    assert inputs.tool_name == "Template"
    styled = inputs.results["StyledText"]
    assert styled.success is True
    assert styled.value == "PROBE-CUE-1 今日はDaVinci Resolveの使い方を紹介します"

    text = task4_ops.get_text_plus("Template")
    assert text.ok is True
    assert text.tool_name == "Template"
    assert text.text == "PROBE-CUE-1 今日はDaVinci Resolveの使い方を紹介します"

    font = task4_ops.get_fusion_input("Template", "Font")
    assert font.ok is True
    assert font.value == "Hiragino Sans W3"

    mpi = task4_ops.current_timeline_media_pool_item()
    assert mpi.ok is True
    assert mpi.id == "3d159a0f-55f5-4635-a7d8-facd7f5fba58"


def test_probe_generation_echo_parses_and_unknown_action_stays_typed(
    task4_ops: McpOps,
) -> None:
    probe = task4_ops.subtitle_generation_probe()
    assert probe.ok is True
    assert probe.would_generate is True
    assert probe.settings == {"language": "auto"}

    empty_wire = McpOps(McpClient(_ProbeWireTransport({})))
    unknown = empty_wire.set_fusion_inputs("Template", {"StyledText": "x"})
    assert unknown.ok is False
    assert unknown.error is not None
    assert unknown.error.code == "UNKNOWN_ACTION"


# mcp-complete-parity Task 5 — Fairlight preset + probe-render wire shapes
# (measured live on Resolve 21.0.4.5 / pinned 2.98.3: presets listing is a
# JSON array, the EMPTY MAPPING when the host saved none, or — after the
# operator saved presets — an INDEX-KEYED MAPPING {"0": "<name>", ...} in
# list order; a missing preset apply answers {"success": false} with no
# error envelope).

_TASK5_WIRE: Mapping[tuple[str, str], object] = {
    ("resolve_control", "get_fairlight_presets"): {
        "presets": ["dialogue-chain", "music-bed"],
        "success": True,
    },
    ("project_settings", "apply_fairlight_preset"): {
        "success": True,
        "preset_name": "dialogue-chain",
    },
    ("render", "prepare_render_job"): {"success": True, "job_id": "job-t5-9"},
    ("render", "get_job_status"): {
        "CompletionPercentage": 100.0,
        "IsRenderingInProgress": False,
        "JobStatus": "Complete",
        "success": True,
    },
}


@pytest.fixture
def task5_ops() -> Generator[McpOps, None, None]:
    client = McpClient(_ProbeWireTransport(_TASK5_WIRE))
    yield McpOps(client)
    client.close()


def _wire_ops(wire: Mapping[tuple[str, str], object]) -> McpOps:
    return McpOps(McpClient(_ProbeWireTransport(dict(wire))))


def test_task5_audio_ops_parse_probe_wire_shapes(task5_ops: McpOps) -> None:
    listed = task5_ops.fairlight_presets()
    assert listed.ok is True
    assert listed.presets == ("dialogue-chain", "music-bed")

    applied = task5_ops.apply_fairlight_preset("dialogue-chain")
    assert applied.ok is True
    assert applied.preset_name == "dialogue-chain"

    job = task5_ops.render_prepare_job(str(Path("/Users") / "t5-render"), "audio-probe")
    assert job.ok is True
    assert job.job_id == "job-t5-9"

    status = task5_ops.render_job_status("job-t5-9")
    assert status.completion_percentage == 100.0
    assert status.is_rendering_in_progress is False


def test_fairlight_listing_parses_the_measured_empty_mapping() -> None:
    empty = _wire_ops(
        {("resolve_control", "get_fairlight_presets"): {"presets": {}, "success": True}}
    )
    listed = empty.fairlight_presets()
    assert listed.ok is True
    assert listed.presets == ()


def test_fairlight_apply_false_stays_not_ok_without_error_envelope() -> None:
    refusing = _wire_ops(
        {("project_settings", "apply_fairlight_preset"): {"success": False}}
    )
    refused = refusing.apply_fairlight_preset("no-such-preset")
    assert refused.ok is False
    assert refused.error is None


def test_fairlight_unobserved_listing_shape_fails_loud() -> None:
    drifted = _wire_ops(
        {
            ("resolve_control", "get_fairlight_presets"): {
                "presets": {"dialogue": {"amount": 3}},
                "success": True,
            }
        }
    )
    with pytest.raises(ValidationError):
        drifted.fairlight_presets()


def test_fairlight_listing_parses_the_live_saved_preset_indexed_mapping() -> None:
    saved = _wire_ops(
        {
            ("resolve_control", "get_fairlight_presets"): {
                "presets": {"0": "dialogue-chain"},
                "success": True,
            }
        }
    )
    listed = saved.fairlight_presets()
    assert listed.ok is True
    assert listed.presets == ("dialogue-chain",)


def test_fairlight_indexed_mapping_keeps_list_position_order() -> None:
    saved = _wire_ops(
        {
            ("resolve_control", "get_fairlight_presets"): {
                "presets": {"0": "dialogue-chain", "1": "music-bed"},
                "success": True,
            }
        }
    )
    listed = saved.fairlight_presets()
    assert listed.ok is True
    assert listed.presets == ("dialogue-chain", "music-bed")


@pytest.mark.parametrize(
    "presets_wire",
    [
        pytest.param({"0": "a", "2": "b"}, id="index-gap"),
        pytest.param({"1": "a"}, id="not-zero-based"),
        pytest.param({"0": {"amount": 3}}, id="non-string-value"),
        pytest.param({"dialogue": "chain"}, id="non-index-key"),
    ],
)
def test_fairlight_ambiguous_indexed_mapping_shapes_fail_loud(
    presets_wire: dict[str, object],
) -> None:
    drifted = _wire_ops(
        {
            ("resolve_control", "get_fairlight_presets"): {
                "presets": presets_wire,
                "success": True,
            }
        }
    )
    with pytest.raises(ValidationError):
        drifted.fairlight_presets()


# ---------------------------------------------------------------------------
# Measured live 2026-09-05 (pin v2.207.0, sol-cu-integration evidence):
# get_items_in_track items now carry a ``kind`` field — "transition" observed
# on a Cross Dissolve readback; values beyond that unknown. TrackItemsResult
# accepts it (pin-following); other unknown fields stay extra=forbidden.
# ---------------------------------------------------------------------------

_KIND_WIRE: Mapping[tuple[str, str], object] = {
    ("timeline", "get_items_in_track"): {
        "items": [
            {
                "name": "クロスディゾルブ",
                "id": "item-trans-1",
                "start": 108135,
                "end": 108165,
                "duration": 30,
                "kind": "transition",
            },
            {
                "name": "edit-source.mov",
                "id": "item-clip-1",
                "start": 108000,
                "end": 108150,
                "duration": 150,
            },
        ]
    }
}


def test_items_in_track_parses_measured_kind_present_and_absent() -> None:
    items = _wire_ops(_KIND_WIRE).get_items_in_track("video", 1)
    assert items.ok is True
    assert len(items.items) == 2
    assert items.items[0].kind == "transition"  # measured live value
    assert items.items[1].kind is None  # items without kind keep parsing


def test_items_in_track_still_forbids_other_unknown_fields() -> None:
    drifted = _wire_ops(
        {
            ("timeline", "get_items_in_track"): {
                "items": [{"name": "x", "id": "y", "bogus": 1}]
            }
        }
    )
    with pytest.raises(ValidationError):
        drifted.get_items_in_track("video", 1)
