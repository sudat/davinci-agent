"""Production IR compilation for the Phase-1 fixture chain (Todo 44).

The committed EditPlan compiles through the production compiler with the
manifest's DECLARED subtitle cue table and the frozen Japanese QC policy; the
compile is deterministic and fail-closed (an IR that violates QC never leaves
the compiler).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.compile.conform_inputs import EditSourceGeometry
from services.compile.production_compiler import CompileProductionResult, compile_production
from services.compile.subtitle_policy import (
    FrameCueSpan,
    SubtitleQcPolicy,
    TranscriptCueSegment,
    TranscriptCueSource,
)
from services.contracts.primitives import RationalFrameRate

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.plan.edit_plan_models import EditPlan


def cue_source(manifest: Phase1TechnicalFixtureManifest) -> TranscriptCueSource:
    return TranscriptCueSource(
        language="ja",
        segments=tuple(
            TranscriptCueSegment(
                segment_id=subtitle.segment_id,
                text=subtitle.text,
                span=FrameCueSpan(
                    start_frame=subtitle.span.start_frame,
                    end_frame=subtitle.span.end_frame,
                ),
            )
            for subtitle in manifest.subtitles
        ),
    )


def qc_policy() -> SubtitleQcPolicy:
    return SubtitleQcPolicy(
        policy_id="subtitle-qc-phase1-v1",
        min_duration_frames=15,
        max_lines=2,
        max_chars_per_line=20,
        declared_style_refs=("style-default-ja",),
        default_style_ref="style-default-ja",
    )


def compile_production_ir(
    manifest: Phase1TechnicalFixtureManifest, plan: EditPlan
) -> CompileProductionResult:
    geometry = EditSourceGeometry(
        source_id=manifest.edit_source.source_id,
        frame_rate=RationalFrameRate(
            num=manifest.edit_source.frame_rate_num, den=manifest.edit_source.frame_rate_den
        ),
        total_frames=manifest.edit_source.total_frames,
        audio_sample_rate=manifest.edit_source.audio_sample_rate,
    )
    return compile_production(
        plan,
        geometry,
        cue_source(manifest),
        qc_policy(),
        artifact_id=f"timeline-ir-{manifest.fixture_id}",
    )


__all__ = ["compile_production_ir", "cue_source", "qc_policy"]
