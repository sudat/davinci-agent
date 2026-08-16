"""Shared preview fixtures: pinned tools, frozen 0A media, rendered session artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.compile.phase0c import build_ir, compile_plan
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
    ReviewCommand0C,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    SourceFrameSpan,
)
from services.fixtures.manifest import Phase0AFixtureManifest
from services.fixtures.manifest_phase0c import Phase0CFixtureManifest
from services.preview.binding import bindings_for_ir, initial_bindings, initial_timeline_ir
from services.preview.models import AppliedDecision, PreviewError, PreviewTraceManifest
from services.preview.render import render_preview
from services.preview.tools import PinnedTools, load_pinned_tools

if TYPE_CHECKING:
    from collections.abc import Mapping

PHASE_0C_LOCK = Path("config/toolchains/phase-0c-v1.json")
P0A_MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
P0C_REMOVE_CLEAR = Path("tests/fixtures/manifests/phase-0c/p0c-remove-clear.json")
RATE = RationalFrameRate(num=30, den=1)
TEST_PRODUCER = Producer(name="phase0c-preview-test", version="1")


def _fixture_dir() -> Path:
    override = os.environ.get("FVP_PREVIEW_FIXTURE_DIR")
    if override:
        return Path(override)
    tools = load_pinned_tools(PHASE_0C_LOCK)
    return tools.ffmpeg.parents[3] / "phase-0a" / "fixture"


@pytest.fixture(scope="session")
def tools() -> PinnedTools:
    try:
        return load_pinned_tools(PHASE_0C_LOCK)
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    path = _fixture_dir()
    if not (path / "source.mov").is_file():
        pytest.skip(f"phase 0A fixture media not materialized: {path}")
    return path


@pytest.fixture(scope="session")
def manifest() -> Phase0AFixtureManifest:
    return Phase0AFixtureManifest.model_validate_json(P0A_MANIFEST.read_bytes())


@pytest.fixture(scope="session")
def initial_render(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PreviewTraceManifest, Path]:
    ir = initial_timeline_ir(manifest)
    bindings = initial_bindings(fixture_dir)
    out_dir = tmp_path_factory.mktemp("preview-initial")
    trace = render_preview(None, ir, bindings, out_dir, tools=tools)
    return trace, out_dir / "preview.mp4"


def p0c_plan_and_command() -> tuple[EditPlan0C, ReviewCommand0C]:
    spec = Phase0CFixtureManifest.model_validate_json(P0C_REMOVE_CLEAR.read_bytes())
    items = tuple(
        EditPlanItem0C(
            item_id=item.item_id,
            kind=item.kind,
            source_id=item.source_id,
            span=SourceFrameSpan(
                start_frame=item.span.start_frame,
                end_frame=item.span.end_frame,
                rate=RATE,
            ),
            track_index=item.track_index,
            av_link_id=item.av_link_id,
            subtitle_text=item.subtitle_text,
            locked_fields=item.locked_fields,
        )
        for item in spec.edit_plan.items
    )
    plan = EditPlan0C(
        artifact_id=f"edit-plan-{spec.fixture_id}",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=TEST_PRODUCER,
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(
                source_id=spec.edit_plan.edit_source.source_id,
                total_frames=spec.edit_plan.edit_source.total_frames,
            ),
            items=items,
        ),
    )
    target = spec.command.target
    assert target.kind == "item_id"
    command = ReviewCommand0C(
        command_id=f"cmd-{spec.fixture_id}",
        language=spec.command.language,
        instruction=spec.command.instruction,
        operation=spec.command.operation,
        base_plan_version="v1",
        target=ItemIdSelector0C(kind="item_id", item_id=target.item_id),
        new_span=None,
        new_text=None,
    )
    return plan, command


@pytest.fixture(scope="session")
def p0c_media(fixture_dir: Path) -> Mapping[str, Path]:
    return {"p0b-cfr24-cfr30-mezzanine": fixture_dir / "source.mov"}


@pytest.fixture(scope="session")
def p0c_context(
    tools: PinnedTools,
    fixture_dir: Path,
    p0c_media: Mapping[str, Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PreviewTraceManifest, PreviewTraceManifest]:
    """(initial 0C v1 trace, post p0c-remove-clear v2 trace)."""

    plan, command = p0c_plan_and_command()
    ir_v1 = build_ir(plan, "timeline-ir-preview-p0c-v1", ())
    bindings_v1 = bindings_for_ir(ir_v1, p0c_media, fixture_dir / "pulse.wav")
    trace_v1 = render_preview(
        plan, ir_v1, bindings_v1, tmp_path_factory.mktemp("preview-p0c-v1"), tools=tools
    )
    result = compile_plan(plan, command, "timeline-ir-preview-p0c-v2")
    bindings_v2 = bindings_for_ir(result.ir, p0c_media, fixture_dir / "pulse.wav")
    decision = AppliedDecision(
        decision_id=command.command_id,
        case_id="p0c-remove-clear",
        classification="clear",
        plan_version_after=result.plan.plan.plan_version,
        previous_trace=trace_v1,
    )
    trace_v2 = render_preview(
        result.plan,
        result.ir,
        bindings_v2,
        tmp_path_factory.mktemp("preview-p0c-v2"),
        tools=tools,
        decision=decision,
    )
    return trace_v1, trace_v2


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
