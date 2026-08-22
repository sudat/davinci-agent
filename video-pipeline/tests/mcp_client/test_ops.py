"""McpOps typed-surface contract against the fake stdio MCP server.

Every ops method must round-trip its canned LIVE-shaped payload into the
frozen result model from ``services.mcp_client.ops_models`` — including the
error-envelope path (``ok`` false with the typed code) — proving the probe
surface parses real wire shapes instead of trusting console claims.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest

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
