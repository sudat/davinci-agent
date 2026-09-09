"""Decision/plan agreement guard: version skew with identical content renders.

The r9b re-adoption advanced the review-decision chain to v3 while the
rendered plan stayed at v1 with byte-identical content; the preview guard
must accept that shape (sha identity) while still refusing a genuinely
stale plan (sha mismatch) or an unprovable one (missing sha).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.cli.project import plan_sha256
from services.compile.phase0c import build_ir
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)
from services.preview.binding import bindings_for_ir
from services.preview.models import (
    AppliedDecision,
    ItemBinding,
    MediaBinding,
    PreviewFile,
    PreviewLayoutError,
    PreviewMediaBindings,
    PreviewTraceManifest,
    RecordDecisionSpan,
    TimelineBinding,
    TraceDecision,
    TraceInput,
)
from services.preview.render import _check_decision_plan_agreement, render_preview
from tests.preview.conftest import p0c_plan_and_command

if TYPE_CHECKING:
    from collections.abc import Mapping

    from services.preview.tools import PinnedTools

RATE = RationalFrameRate(num=30, den=1)


def _previous_trace() -> PreviewTraceManifest:
    """Minimal valid trace to anchor an AppliedDecision (no media needed)."""
    record = RecordFrameSpan(start_frame=0, end_frame=30)
    return PreviewTraceManifest(
        schema_version="preview-trace-v1",
        preview=PreviewFile(
            path="/tmp/never-rendered.mp4",  # noqa: S108 (never opened; display string only)
            sha256="0" * 64,
            size=1,
            decoded_video_sha256="0" * 64,
        ),
        timeline_binding=TimelineBinding(
            plan_version="v1",
            ir_sha256="0" * 64,
            total_record_frames=30,
            timeline_rate=RATE,
        ),
        inputs=(
            TraceInput(
                item_id="cut-001",
                kind="video",
                media_path="/tmp/never-touched.mov",  # noqa: S108 (never opened; display string only)
                sha256="0" * 64,
                source_span=SourceFrameSpan(start_frame=0, end_frame=30, rate=RATE),
                record_span=record,
            ),
        ),
        decisions=(
            TraceDecision(
                decision_id="initial-plan-v1",
                case_id="initial-plan",
                classification="initial",
                applied=True,
                plan_version_after="v1",
            ),
        ),
        record_to_decision=(
            RecordDecisionSpan(span=record, decision_id="initial-plan-v1"),
        ),
        strategy_notes={
            "subtitle_rung": "omitted-no-subtitle-items",
            "overlay_strategy": "lavfi-color-corner-marker-overlay",
            "audio_strategy": "item-linked-concat-single-track-no-bgm-binding",
            "determinism_policy": "semantic-equivalence-h264-videotoolbox",
        },
        ffprobe_summary={
            "stream_count": 2,
            "video_codec": "h264",
            "width": 640,
            "height": 360,
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "nb_read_frames": 30,
            "video_duration_ms": 1000,
            "container_duration_ms": 1000,
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
            "subtitle_codec": None,
        },
    )


def _decision(version_after: str, sha: str | None) -> AppliedDecision:
    return AppliedDecision(
        decision_id="decision-r9b-readopt",
        case_id="r9b-case",
        classification="clear",
        plan_version_after=version_after,
        plan_sha256=sha,
        previous_trace=_previous_trace(),
    )


def _dummy_bindings() -> PreviewMediaBindings:
    """Bindings never touched when the guard refuses (refusal precedes media)."""
    return PreviewMediaBindings(
        items=(
            ItemBinding(
                item_id="cut-001",
                binding=MediaBinding(
                    media_path="/tmp/never-touched.mov", sha256="0" * 64  # noqa: S108 (guard fires pre-media)
                ),
            ),
        ),
    )


def test_skewed_versions_with_identical_sha_pass_the_guard() -> None:
    """(a) r9b exact shape: decision v3 + plan v1, same content sha → proceed."""
    plan, _command = p0c_plan_and_command()
    assert plan.plan.plan_version == "v1"
    _check_decision_plan_agreement(plan, _decision("v3", plan_sha256(plan)), "v1")


def test_skewed_versions_with_different_sha_are_refused() -> None:
    """(b) decision v3 + plan v1, different sha → the legacy typed refusal."""
    plan, _command = p0c_plan_and_command()
    with pytest.raises(
        PreviewLayoutError, match=r"decision bumps to v3 but the plan is v1$"
    ):
        _check_decision_plan_agreement(plan, _decision("v3", "f" * 64), "v1")


def test_equal_versions_need_no_sha() -> None:
    """(c) equal versions → unchanged: no sha comparison happens."""
    plan, _command = p0c_plan_and_command()
    _check_decision_plan_agreement(plan, _decision("v1", None), "v1")


def test_skewed_versions_with_missing_decision_sha_are_refused() -> None:
    """(d1) decision v3 + plan v1, decision sha absent → honest typed failure."""
    plan, _command = p0c_plan_and_command()
    with pytest.raises(PreviewLayoutError, match="unprovable"):
        _check_decision_plan_agreement(plan, _decision("v3", None), "v1")


def test_skewed_versions_with_missing_plan_are_refused() -> None:
    """(d2) decision v2 + no plan content to hash → honest typed failure."""
    with pytest.raises(PreviewLayoutError, match="unprovable"):
        _check_decision_plan_agreement(None, _decision("v2", "f" * 64), "v1")


def test_render_refuses_sha_mismatch_before_any_ffmpeg_run(
    tools: PinnedTools, tmp_path: Path
) -> None:
    """(b, wired) the mismatch refusal fires inside render_preview pre-media."""
    plan, _command = p0c_plan_and_command()
    ir = build_ir(plan, "timeline-ir-r9b-mismatch", ())
    with pytest.raises(
        PreviewLayoutError, match=r"decision bumps to v3 but the plan is v1$"
    ):
        render_preview(
            plan, ir, _dummy_bindings(), tmp_path, tools=tools,
            decision=_decision("v3", "f" * 64),
        )
    assert not (tmp_path / "preview.mp4").exists()


def test_render_refuses_missing_sha_before_any_ffmpeg_run(
    tools: PinnedTools, tmp_path: Path
) -> None:
    """(d1, wired) the missing-sha refusal fires inside render_preview pre-media."""
    plan, _command = p0c_plan_and_command()
    ir = build_ir(plan, "timeline-ir-r9b-unprovable", ())
    with pytest.raises(PreviewLayoutError, match="unprovable"):
        render_preview(
            plan, ir, _dummy_bindings(), tmp_path, tools=tools,
            decision=_decision("v3", None),
        )
    assert not (tmp_path / "preview.mp4").exists()


def test_render_accepts_r9b_shape_end_to_end(
    tools: PinnedTools,
    fixture_dir: Path,
    p0c_media: Mapping[str, Path],
    tmp_path: Path,
) -> None:
    """(a, wired) decision v3 + plan v1 with identical sha renders a preview."""
    plan, _command = p0c_plan_and_command()
    ir = build_ir(plan, "timeline-ir-r9b-accepted", ())
    bindings = bindings_for_ir(ir, p0c_media, fixture_dir / "pulse.wav")
    first = render_preview(
        plan, ir, bindings, tmp_path / "v1", tools=tools,
    )
    manifest = render_preview(
        plan, ir, bindings, tmp_path / "v3", tools=tools,
        decision=AppliedDecision(
            decision_id="decision-r9b-readopt",
            case_id="r9b-case",
            classification="clear",
            plan_version_after="v3",
            plan_sha256=plan_sha256(plan),
            previous_trace=first,
        ),
    )
    assert (tmp_path / "v3" / "preview.mp4").is_file()
    assert manifest.timeline_binding.plan_version == "v1"
