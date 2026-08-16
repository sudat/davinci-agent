from __future__ import annotations

import pytest

from services.toolchain.smoke import ProbeAssertionError, ProbePayload, assert_probe_payload


def test_probe_asserts_stream_fps_frames_and_duration() -> None:
    payload = {
        "streams": (
            {
                "codec_type": "video",
                "avg_frame_rate": "30/1",
                "duration": "1.000000",
                "nb_frames": "30",
            },
        ),
        "format": {"duration": "1.000000"},
    }

    result = assert_probe_payload(ProbePayload.model_validate(payload))

    assert result.frame_rate == "30/1"
    assert result.frame_count == 30
    assert result.duration_milliseconds == 1000


def test_probe_rejects_misleading_success_payload() -> None:
    payload = {
        "streams": ({"codec_type": "video", "avg_frame_rate": "30/1"},),
        "format": {"duration": "0.500000"},
    }

    with pytest.raises(ProbeAssertionError, match=r"duration|frame"):
        assert_probe_payload(ProbePayload.model_validate(payload))
