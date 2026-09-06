"""readbacks: the per-operation table maps exactly the Opus rows; unknown refused.

Covers safeguard E (suda order 2026-09-06, Opus 2026-09-06T08:30:40Z):
speed → source extent (duration blind) + render cadence, volume → render
RMS, color_lift → render pixel diff, subtitle_text → get_transcript,
transform → get_transform. An operation with no row raises
ReadbackUndefinedError instead of guessing. Pure data contract — no
Resolve contact.
"""

from __future__ import annotations

import pytest

from services.cu_client.errors import ReadbackUndefinedError
from services.cu_client.readbacks import READBACKS, readback_for


def test_table_covers_exactly_the_five_opus_operations() -> None:
    assert sorted(spec.operation for spec in READBACKS) == [
        "color_lift",
        "speed",
        "subtitle_text",
        "transform",
        "volume",
    ]


def test_speed_row_names_extent_and_marks_duration_blind() -> None:
    spec = readback_for("speed")
    assert "source extent" in spec.primary
    assert "render cadence" in (spec.secondary or "")
    assert spec.blind is not None
    assert "duration" in spec.blind


def test_volume_color_subtitle_transform_rows() -> None:
    assert "RMS" in readback_for("volume").primary
    assert "pixel diff" in readback_for("color_lift").primary
    assert "get_transcript" in readback_for("subtitle_text").primary
    assert "get_transform" in readback_for("transform").primary


def test_unknown_operation_is_refused_not_guessed() -> None:
    with pytest.raises(ReadbackUndefinedError, match="no readback row"):
        readback_for("fairlight_pan")
