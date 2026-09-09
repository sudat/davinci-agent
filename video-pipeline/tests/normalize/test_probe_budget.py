"""Media-size-scaled decode budgets for normalize probes (PRD §2.5 fix).

Pure unit tests over the scaling math and the metadata-hint parser — the
subprocess paths are already exercised end-to-end by the happy-path
normalize tests on small real media (floor budget, unchanged behavior).
The constants here encode the MEASURED blockers from
``.omo/evidence/v44-first-publish-delta/v44-0-runs/BLOCKED.md``: the 4K
HEVC input needs 333 s (8459 frames x 39.4 ms/frame measured), which the
old frozen 120 s budget refused.
"""

from __future__ import annotations

import json

from services.normalize.probe import (
    PROBE_TIMEOUT_CEILING_SECONDS,
    PROBE_TIMEOUT_FLOOR_SECONDS,
    _frame_hint,
    decode_probe_timeout_seconds,
)


def _hint(payload_json: str) -> int | None:
    return _frame_hint(json.loads(payload_json))


def test_budget_floor_keeps_small_media_fast() -> None:
    """Given: None/negative/tiny hints; Then: the 120 s floor applies —
    small/test media keep today's behavior and speed."""

    assert decode_probe_timeout_seconds(None) == PROBE_TIMEOUT_FLOOR_SECONDS == 120
    assert decode_probe_timeout_seconds(0) == 120
    assert decode_probe_timeout_seconds(-5) == 120
    assert decode_probe_timeout_seconds(1_700) == 120  # 1700 x 70 ms = 119 s < floor


def test_budget_scales_with_measured_per_frame_cost() -> None:
    """Given: the real episode's frame counts; Then: the budget covers the
    MEASURED decode range (333-437 s across two runs of the same 8459-frame
    HEVC input; 18.9 ms/frame mezzanine probe) with the 70 ms/frame budget."""

    # 8459-frame 4K HEVC input: measured 333 s and 437 s, budget 593 s
    assert decode_probe_timeout_seconds(8_459) == 593
    # 8468-frame mezzanine (same class): budget 593 s
    assert decode_probe_timeout_seconds(8_468) == 593


def test_budget_ceiling_preserves_the_hung_command_guard() -> None:
    """Given: an absurd frame count; Then: the budget hard-caps at 20 min —
    a hung ffprobe still dies typed, never unbounded."""

    assert decode_probe_timeout_seconds(600_000) == PROBE_TIMEOUT_CEILING_SECONDS == 1200


def test_metadata_hint_from_real_episode_payload_shape() -> None:
    """Given: the v44-real-01 metadata shape (CFR 30000/1001, 282.25 s);
    Then: the hint is rate x duration (8459 frames)."""

    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "video", "r_frame_rate": "30000/1001", "duration": "282.248611"},
            ],
            "format": {"duration": "282.248611"},
        }
    )
    # container duration truncates below the true 8459 (282.2543 s) — the
    # hint only sizes a budget (423 s either way), never correctness
    assert _hint(payload) == 8_458


def test_metadata_hint_survives_container_lies_and_garbage() -> None:
    """Given: payloads a hint cannot be derived from; Then: None (floor
    budget) — the real -count_frames probe stays the typed authority."""

    assert _frame_hint(None) is None
    assert _hint("[]") is None
    assert _hint(json.dumps({"streams": "nope"})) is None
    assert _hint(json.dumps({"streams": [{"codec_type": "video"}]})) is None
    no_rate = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "10"}})
    assert _hint(no_rate) is None
    bad_rate = json.dumps(
        {
            "streams": [{"codec_type": "video", "r_frame_rate": "garbage"}],
            "format": {"duration": "10"},
        }
    )
    assert _hint(bad_rate) is None
    zero_duration = json.dumps(
        {
            "streams": [{"codec_type": "video", "r_frame_rate": "30/1"}],
            "format": {"duration": "0"},
        }
    )
    assert _hint(zero_duration) is None
