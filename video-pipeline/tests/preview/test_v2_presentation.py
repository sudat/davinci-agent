"""Presentation Preview v2 (task 60, W6 scope): stub-launch contract test.

The real low-cost presentation build path arrives with task 39's runner; this
module pins the LAUNCH surface: a stub McpExecutionPlan is shape-validated
against the Timeline IR v2 (episode + canonical ir sha) and produces a
placeholder trace without fabricating any media.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.ir_models_v2 import PlacedClipV2, TimelineIrV2, VideoTrackV2
from services.foundation_io import canonical_model_bytes
from services.preview.v2_presentation import (
    McpExecutionPlanStub,
    PresentationPreviewError,
    StubStepV2,
    render_presentation_preview,
)

RATE = RationalFrameRate(num=30, den=1)


def _minimal_ir() -> TimelineIrV2:
    return TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-v2-presentation",
        rate=RATE,
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="v-primary",
                items=(
                    PlacedClipV2(
                        item_id="itm-cand-a",
                        source=SourceRef(
                            source_id="cam-a",
                            span=SourceFrameSpan(start_frame=0, end_frame=60, rate=RATE),
                        ),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
                        candidate_ref="cand-a",
                    ),
                ),
            ),
        ),
    )


def _stub_for(ir: TimelineIrV2) -> McpExecutionPlanStub:
    return McpExecutionPlanStub(
        schema_version="mcp-execution-plan-stub-v1",
        episode_id=ir.episode_id,
        ir_sha256=hashlib.sha256(canonical_model_bytes(ir)).hexdigest(),
        steps=(
            StubStepV2(step_id="s-place", capability="exact-source-range-placement"),
            StubStepV2(step_id="s-subtitle", capability="subtitle-capability"),
        ),
    )


def test_presentation_stub_launch_writes_placeholder_trace(tmp_path: Path) -> None:
    """(g) A well-formed stub launches the presentation path: placeholder
    trace written atomically, round-trips, and no media is fabricated."""

    ir = _minimal_ir()
    out = tmp_path / "presentation.trace.json"
    trace = render_presentation_preview(ir, _stub_for(ir), output_path=out)
    assert trace.status == "placeholder-not-rendered"
    assert trace.episode_id == ir.episode_id
    assert out.is_file()
    reloaded = type(trace).model_validate_json(out.read_bytes())
    assert reloaded == trace
    assert list(tmp_path.iterdir()) == [out]


def test_presentation_stub_episode_mismatch_refused(tmp_path: Path) -> None:
    ir = _minimal_ir()
    stub = _stub_for(ir).model_copy(update={"episode_id": "ep-other"})
    with pytest.raises(PresentationPreviewError, match="episode"):
        render_presentation_preview(ir, stub, output_path=tmp_path / "t.json")


def test_presentation_stub_stale_ir_sha_refused(tmp_path: Path) -> None:
    ir = _minimal_ir()
    stale = _stub_for(ir).model_copy(update={"ir_sha256": "0" * 64})
    with pytest.raises(PresentationPreviewError, match="stale"):
        render_presentation_preview(ir, stale, output_path=tmp_path / "t.json")


def test_presentation_stub_requires_steps(tmp_path: Path) -> None:
    ir = _minimal_ir()
    with pytest.raises(ValidationError) as excinfo:
        McpExecutionPlanStub.model_validate({**_stub_for(ir).model_dump(mode="json"), "steps": []})
    assert any(error["loc"] == ("steps",) for error in excinfo.value.errors())
