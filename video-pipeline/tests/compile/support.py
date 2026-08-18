"""Shared Todo-44 rig: production compile inputs from frozen Phase-1 fixtures.

Every compile input here is derived through the Todo-42→43 chain
(``tests.plan.support`` planner inputs + Todo 43 ``generate``) over the frozen
Phase-1 fixture manifests and the frozen golden tables. Subtitle cue sources
come from the manifests' declared transcript/subtitle tables only — no
invented cues, no clocks, no randomness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.compile.conform_inputs import EditSourceGeometry
from services.compile.production_compiler import compile_production
from services.compile.subtitle_policy import (
    FrameCueSpan,
    SubtitleQcPolicy,
    TranscriptCueSegment,
    TranscriptCueSource,
)
from services.contracts.primitives import RationalFrameRate
from services.editorial.candidate_models import CandidateSpan, ProposalProducer
from services.editorial.proposal_builder import build_selection_proposal
from services.plan.constraint_planner import solve
from services.plan.edit_plan_generate import generate
from services.plan.edit_plan_models import AudioSpanBinding, EditPlan, SelectionPlanRef
from services.plan.planner_models import PlannerSolution
from tests.editorial.support import GOLDEN_EXPECTED_PATH, load_manifest
from tests.editorial.test_selection_proposal import index_for, pool_for
from tests.plan.support import planner_input_for, pre_budget_document, segment_map

if TYPE_CHECKING:
    from services.compile.production_compiler import CompileProductionResult
    from services.editorial.candidate_models import SelectionPlanProposal

EDIT_BASE = "edit-base-v0"
RATE = RationalFrameRate(num=30, den=1)
PRODUCER = ProposalProducer(model_role_id="constraint-planner", contract_version="todo44-v1")


def _selection_for(fixture_id: str) -> SelectionPlanProposal:
    manifest = load_manifest(fixture_id)
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool_for(manifest),
        evidence_index=index_for(manifest),
        director_document=pre_budget_document(fixture_id),
        plan_base_version="plan-base-v0",
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def selection_ref_for(fixture_id: str, selection: SelectionPlanProposal) -> SelectionPlanRef:
    return SelectionPlanRef(
        episode_id=fixture_id,
        plan_version="v1",
        plan_sha256=hashlib.sha256(
            json.dumps(
                selection.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
        plan_artifact_id=f"selection-plan.{fixture_id}.v1",
    )


def _binding_span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=30,
        rate_den=1,
        start_ms=start_frame * 1000 // 30,
        end_ms=end_frame * 1000 // 30,
    )


def _audio_bindings(fixture_id: str) -> tuple[AudioSpanBinding, ...]:
    manifest = load_manifest(fixture_id)
    mapping = segment_map(fixture_id)
    link_by_segment = {link.segment_id: link for link in manifest.av_links}
    return tuple(
        AudioSpanBinding(
            candidate_id=candidate.candidate_id,
            span=_binding_span(
                link_by_segment[mapping[candidate.candidate_id]].audio_span.start_frame,
                link_by_segment[mapping[candidate.candidate_id]].audio_span.end_frame,
            ),
        )
        for candidate in _selection_for(fixture_id).candidates
        if candidate.intent == "keep"
        and mapping[candidate.candidate_id] in link_by_segment
    )


def generated_for(fixture_id: str) -> EditPlan:
    selection = _selection_for(fixture_id)
    outcome = solve(planner_input_for(fixture_id))
    assert isinstance(outcome, PlannerSolution)
    return generate(
        selection,
        outcome,
        plan_ref=selection_ref_for(fixture_id, selection),
        plan_base_version=EDIT_BASE,
        producer=PRODUCER,
        fixture_only=True,
        audio_bindings=_audio_bindings(fixture_id),
    )


def geometry_for(fixture_id: str) -> EditSourceGeometry:
    manifest = load_manifest(fixture_id)
    return EditSourceGeometry(
        source_id=manifest.edit_source.source_id,
        frame_rate=RationalFrameRate(
            num=manifest.edit_source.frame_rate_num,
            den=manifest.edit_source.frame_rate_den,
        ),
        total_frames=manifest.edit_source.total_frames,
        audio_sample_rate=manifest.edit_source.audio_sample_rate,
    )


def manifest_cue_source(fixture_id: str) -> TranscriptCueSource:
    """The manifest's DECLARED subtitle table (empty for fixtures 01-04)."""

    manifest = load_manifest(fixture_id)
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


def golden_transcript_cue_source(
    fixture_id: str, segment_ids: tuple[str, ...]
) -> TranscriptCueSource:
    """Cue source built from the FROZEN golden transcript table (speech only)."""

    document: object = json.loads(Path(GOLDEN_EXPECTED_PATH).read_bytes())
    assert isinstance(document, dict)
    spans = document["transcript_spans"][fixture_id]
    assert isinstance(spans, list)
    segments = []
    for row in spans:
        assert isinstance(row, dict)
        if row["segment_id"] not in segment_ids or not str(row["text"]):
            continue
        segments.append(
            TranscriptCueSegment(
                segment_id=str(row["segment_id"]),
                text=str(row["text"]),
                span=FrameCueSpan(
                    start_frame=int(row["start_frame"]),
                    end_frame=int(row["end_frame"]),
                ),
            )
        )
    return TranscriptCueSource(language="ja", segments=tuple(segments))


def default_policy() -> SubtitleQcPolicy:
    return SubtitleQcPolicy(
        policy_id="subtitle-qc-test-v1",
        min_duration_frames=15,
        max_lines=2,
        max_chars_per_line=20,
        declared_style_refs=("style-default-ja",),
        default_style_ref="style-default-ja",
    )


def golden_fixture(fixture_id: str) -> dict[str, object]:
    document: object = json.loads(Path(GOLDEN_EXPECTED_PATH).read_bytes())
    assert isinstance(document, dict)
    fixtures = document["fixtures"]
    assert isinstance(fixtures, dict)
    entry = fixtures[fixture_id]
    assert isinstance(entry, dict)
    return entry


def compile_fixture(
    fixture_id: str,
    *,
    transcript: TranscriptCueSource | None = None,
    geometry: EditSourceGeometry | None = None,
    policy: SubtitleQcPolicy | None = None,
) -> CompileProductionResult:
    return compile_production(
        generated_for(fixture_id),
        geometry if geometry is not None else geometry_for(fixture_id),
        manifest_cue_source(fixture_id) if transcript is None else transcript,
        default_policy() if policy is None else policy,
        artifact_id=f"timeline-ir-{fixture_id}",
    )
