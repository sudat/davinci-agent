"""MCP parity backend — offline contract (task 11).

The live builder spawns the pinned server and runs only under ``mcp_live``
(see tests/mcp_client/test_live_probes.py); these tests pin the OFFLINE
contract: the backend is registered, and the pure readback→structure
derivation produces byte-equal canonical structures against the recorded
fake-server snapshot shapes and the committed expected extract.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from services.contracts.timeline_ir import TimelineIrProduction
from services.mcp_client.ops_models import StructureSnapshot
from services.qa.parity_harness import _BACKENDS
from services.qa.parity_mcp import (
    McpRenderReadback,
    mcp_structure_from_readback,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures/parity-basecut"
IR_PATH = FIXTURE_DIR / "ir.json"
EXPECTED_PATH = FIXTURE_DIR / "expected_extract.json"

FAKE_SNAPSHOT_PAYLOAD: dict[str, object] = {
    "name": "Fake Timeline",
    "id": "tl-fake-1",
    "start_frame": 108000,
    "end_frame": 108210,
    "start_timecode": "01:00:00:00",
    "item_count": 6,
    "tracks": {
        "video": {
            "track_count": 1,
            "tracks": [
                {
                    "track_index": 1,
                    "item_count": 3,
                    "items": [
                        {
                            "name": f"parity-src-00{index}.mp4",
                            "timeline_item_id": f"ti-v{index}",
                            "track_type": "video",
                            "track_index": 1,
                            "item_index": index - 1,
                            "start": start,
                            "end": end,
                            "duration": end - start,
                            "source_start": sstart,
                            "source_end": send,
                            "source_fps": 30.0,
                            "media_pool_item_id": f"mpi-{index}",
                            "media_pool_item_name": f"parity-src-00{index}",
                        }
                        for index, start, end, sstart, send in (
                            (1, 108000, 108060, 0, 60),
                            (2, 108060, 108150, 0, 90),
                            (3, 108150, 108210, 0, 60),
                        )
                    ],
                }
            ],
        },
        "audio": {
            "track_count": 1,
            "tracks": [
                {
                    "track_index": 1,
                    "item_count": 3,
                    "items": [
                        {
                            "name": f"parity-src-00{index}.mp4",
                            "timeline_item_id": f"ti-a{index}",
                            "track_type": "audio",
                            "track_index": 1,
                            "item_index": index - 1,
                            "start": start,
                            "end": end,
                            "duration": end - start,
                            "source_start": sstart,
                            "source_end": send,
                            "source_fps": 30.0,
                            "media_pool_item_id": f"mpi-{index}",
                            "media_pool_item_name": f"parity-src-00{index}",
                        }
                        for index, start, end, sstart, send in (
                            (1, 108000, 108060, 0, 60),
                            (2, 108060, 108150, 0, 90),
                            (3, 108150, 108210, 0, 60),
                        )
                    ],
                }
            ],
        },
        "subtitle": {"track_count": 0, "tracks": []},
    },
    "markers": {},
}

RENDER_READBACK = McpRenderReadback(
    video_format="mp4",
    video_codec="H.264",
    width=1920,
    height=1080,
    frame_rate=Fraction(30, 1),
    audio_codec="aac",
    audio_sample_rate=48000,
    audio_channels=2,
)


def _load_fixture_ir() -> TimelineIrProduction:
    payload: object = json.loads(IR_PATH.read_bytes())
    assert isinstance(payload, dict)
    return TimelineIrProduction.model_validate(payload, strict=False)


def test_mcp_backend_is_registered() -> None:
    assert {"legacy", "mcp"} <= set(_BACKENDS.keys())


def test_readback_derivation_matches_expected_extract() -> None:
    snapshot = StructureSnapshot.model_validate(FAKE_SNAPSHOT_PAYLOAD)
    structure = mcp_structure_from_readback(
        _load_fixture_ir(),
        snapshot,
        RENDER_READBACK,
        lock_video_format="MP4",
        lock_video_codec="H264",
    )
    expected: dict[str, object] = json.loads(EXPECTED_PATH.read_bytes())
    canonical = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    assert canonical == json.dumps(expected, sort_keys=True, separators=(",", ":"))


def test_codec_drift_still_diffs_not_normalized_away() -> None:
    snapshot = StructureSnapshot.model_validate(FAKE_SNAPSHOT_PAYLOAD)
    drifted = mcp_structure_from_readback(
        _load_fixture_ir(),
        snapshot,
        McpRenderReadback(
            video_format="mov",
            video_codec="prores",
            width=1920,
            height=1080,
            frame_rate=Fraction(30, 1),
            audio_codec="pcm",
            audio_sample_rate=48000,
            audio_channels=2,
        ),
        lock_video_format="MP4",
        lock_video_codec="H264",
    )
    render = drifted["render_properties"]
    assert isinstance(render, dict)
    assert render["video_format"] == "mov"
    assert render["video_codec"] == "prores"
    assert render["audio_codec"] == "pcm"
