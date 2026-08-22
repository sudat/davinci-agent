"""Task 41: editorial QC candidate generation (PRD 14.2) — all ten checks.

Contract under test: candidates are REVIEW INPUT, never final truth; critical
candidates always carry ``needs_human_review`` (model-enforced); sensitive
content is keyword FLAG ONLY with mandatory human review; every detector is
deterministic over synthetic fixtures (positive → candidate with the right
check name, clean negative → no candidate).

Fixture joins mirror production identity seams: IR ``candidate_ref`` ->
``MomentSelectionProposalV2`` (handles / story blocks / rationales), T33
reconciliation records, T16 ``TriageEntry`` shot ids via candidate
``evidence_refs`` (derived as ``shot-<candidate_id>`` in these fixtures), T32
density guard re-run, preview trace subtitle presence.
"""

from __future__ import annotations

from typing import Final, get_args

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.audio_finishing import (
    OP_CAPABILITY,
    AudioFinishingPlanV1,
    AudioFinishingStageV1,
    AudioMetric,
    AudioProcessingOpName,
    AudioProcessingOpV1,
    AudioStageName,
    AudioUnit,
    TargetRangeV1,
)
from services.creative_plan.edit_models_v2 import (
    BRollOverlayOp,
    CreativeEditPlanProposalV2,
    EditOperationV2,
    ManualRequiredOp,
    PlacePrimaryClipOp,
    SelectionRefV2,
)
from services.creative_plan.ir_models_v2 import (
    AudioItemV2,
    AudioTrackV2,
    EffectIntentV2,
    PlacedClipV2,
    SemanticTransitionV2,
    SubtitleCueV2,
    TimelineIrV2,
    VideoTrackV2,
)
from services.creative_plan.presentation_intents import (
    ALL_KINDS,
    AudioPolicy,
    BrandAssets,
    ChannelPresentationProfile,
    ClosedRange,
    ColorPolicy,
    DensityLimits,
    IntroOutroRules,
    PresentationIntentV2,
    PunchInRange,
    SfxAccentParams,
    SfxPolicy,
    SubtitleStyle,
    TitleChoices,
    TransitionPreferences,
)
from services.creative_plan.subtitle_models import (
    PATH_ORDER,
    ReconciliationRecordV1,
    SubtitleCapabilityPathV1,
    SubtitlePlanCueV1,
    SubtitlePlanV1,
    SubtitleStyleProfileV1,
)
from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentHandles,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.editorial_v2.story_plan import (
    StoryBlock,
    StoryBriefRef,
    StoryPlanV1,
)
from services.episode_cockpit.review_chat import ReviewCommandKind
from services.media_intelligence.progressive import TriageEntry
from services.preview.models import (
    FfprobeSummary,
    PreviewFile,
    PreviewTraceManifest,
    RecordDecisionSpan,
    StrategyNotes,
    TimelineBinding,
    TraceDecision,
    TraceInput,
)
from services.qc import editorial_checks
from services.qc.editorial_checks import (
    ALL_EDITORIAL_CHECKS,
    DEFAULT_EDITORIAL_QC_THRESHOLDS,
    EditorialQcCandidateV1,
    EditorialQcCheck,
    EditorialQcInput,
    EditorialQcReportV1,
    EditorialQcThresholds,
    aggregate_candidates,
    run_editorial_qc,
    to_review_context,
)

RATE: Final = RationalFrameRate(num=30, den=1)
ZERO_SHA: Final = "0" * 64
DEFAULT_RATIONALE: Final = "東京タワーの撮影について話します"


# ------------------------------------------------------------------ fixtures


def _clip(
    item_id: str,
    cand: str,
    src: tuple[int, int],
    rec: tuple[int, int],
    *,
    source_id: str = "src-a",
) -> PlacedClipV2:
    return PlacedClipV2(
        item_id=item_id,
        source=SourceRef(
            source_id=source_id,
            span=SourceFrameSpan(start_frame=src[0], end_frame=src[1], rate=RATE),
        ),
        record_span=RecordFrameSpan(start_frame=rec[0], end_frame=rec[1]),
        candidate_ref=cand,
    )


def _cue(cue_id: str, text: str, start: int, end: int) -> SubtitleCueV2:
    return SubtitleCueV2(
        cue_id=cue_id,
        text=text,
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
        candidate_ref="cand-cue",
        transcript_ref="tr-cue",
    )


def _candidate(
    cid: str,
    span: tuple[int, int],
    *,
    block: str | None = None,
    handles: MomentHandles | None = None,
    rationale: str = DEFAULT_RATIONALE,
) -> MomentCandidateV2:
    return MomentCandidateV2(
        candidate_id=cid,
        candidate_type="speech",
        source_span=MomentSourceSpan(start_frame=span[0], end_frame=span[1]),
        story_block_ref=block,
        intent="keep",
        rationale=rationale,
        evidence_refs=(f"shot-{cid}",),
        confidence=0.9,
        handles=handles,
        provenance=MomentProvenance(producer="t41-fixture", version="1"),
    )


def _kept(*candidates: MomentCandidateV2) -> MomentSelectionProposalV2:
    return MomentSelectionProposalV2(
        proposal_id="sel-t41", episode_id="ep-t41", candidates=candidates
    )


def _creative(*ops: EditOperationV2) -> CreativeEditPlanProposalV2:
    return CreativeEditPlanProposalV2(
        schema_version="creative-edit-plan-v2",
        proposal_id="cep-t41",
        episode_id="ep-t41",
        selection_ref=SelectionRefV2(proposal_id="sel-t41"),
        operations=ops,
    )


def _audio_item(item_id: str, start: int, end: int) -> AudioItemV2:
    return AudioItemV2(
        item_id=item_id,
        source=SourceRef(
            source_id="src-a",
            span=SourceFrameSpan(start_frame=0, end_frame=end - start, rate=RATE),
        ),
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
    )


def _ir(
    primary: tuple[PlacedClipV2, ...],
    *,
    b_roll: tuple[PlacedClipV2, ...] = (),
    cues: tuple[SubtitleCueV2, ...] = (),
    audio: tuple[AudioTrackV2, ...] = (),
    effect_intents: tuple[EffectIntentV2, ...] = (),
) -> TimelineIrV2:
    tracks: list[VideoTrackV2] = [
        VideoTrackV2(role="primary", track_id="trk-primary", items=primary)
    ]
    if b_roll:
        tracks.append(VideoTrackV2(role="b_roll", track_id="trk-broll", items=b_roll))
    return TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-t41",
        rate=RATE,
        video_tracks=tuple(tracks),
        subtitle_cues=cues,
        audio_tracks=audio,
        effect_intents=effect_intents,
    )


def _trace(*, subtitle: bool, total: int = 170) -> PreviewTraceManifest:
    inputs: list[TraceInput] = [
        TraceInput(
            item_id="itm-1",
            kind="video",
            media_path="/var/media/pv-src.mp4",
            sha256=ZERO_SHA,
            source_span=SourceFrameSpan(start_frame=0, end_frame=total, rate=RATE),
            record_span=RecordFrameSpan(start_frame=0, end_frame=total),
        )
    ]
    if subtitle:
        inputs.append(
            TraceInput(
                item_id="cue-1",
                kind="subtitle",
                media_path="/var/media/pv-sub.mp4",
                sha256="1" * 64,
                source_span=SourceFrameSpan(start_frame=0, end_frame=total, rate=RATE),
                record_span=RecordFrameSpan(start_frame=0, end_frame=total),
            )
        )
    duration_ms = total * 1000 // 30
    return PreviewTraceManifest(
        schema_version="preview-trace-v1",
        preview=PreviewFile(
            path="/var/media/preview.mp4",
            sha256="2" * 64,
            size=1000,
            decoded_video_sha256="3" * 64,
        ),
        timeline_binding=TimelineBinding(
            plan_version="v1",
            ir_sha256="4" * 64,
            total_record_frames=total,
            timeline_rate=RATE,
        ),
        inputs=tuple(inputs),
        decisions=(
            TraceDecision(
                decision_id="d1",
                case_id="case-1",
                classification="clear",
                applied=True,
                plan_version_after="v2",
            ),
        ),
        record_to_decision=(
            RecordDecisionSpan(
                span=RecordFrameSpan(start_frame=0, end_frame=total), decision_id="d1"
            ),
        ),
        strategy_notes=StrategyNotes(
            subtitle_rung="native",
            overlay_strategy="none",
            audio_strategy="mirror",
            determinism_policy="semantic-equivalence-h264-videotoolbox",
        ),
        ffprobe_summary=FfprobeSummary(
            stream_count=2,
            video_codec="h264",
            width=320,
            height=180,
            r_frame_rate="30/1",
            avg_frame_rate="30/1",
            nb_read_frames=total,
            video_duration_ms=duration_ms,
            container_duration_ms=duration_ms,
            audio_codec="aac",
            audio_sample_rate=48000,
            audio_channels=2,
            subtitle_codec="mov_text" if subtitle else None,
        ),
    )


def _sub_plan(
    *,
    records: tuple[ReconciliationRecordV1, ...] = (),
    cues: tuple[SubtitlePlanCueV1, ...] = (),
) -> SubtitlePlanV1:
    return SubtitlePlanV1(
        schema_version="subtitle-plan-v1",
        episode_id="ep-t41",
        rate=RATE,
        style_profile=SubtitleStyleProfileV1(profile_id="sub-default"),
        filler_policy="retain",
        cues=cues,
        text_provenance=(),
        reconciliation=records,
        violations=(),
        capability_path=SubtitleCapabilityPathV1(
            ordered_paths=PATH_ORDER,
            selected="native_text_plus",
            matrix_capability="subtitle-capability",
            matrix_status="accepted",
            note="fixture",
        ),
    )


def _presentation_profile(*, cap: float = 10.0) -> ChannelPresentationProfile:
    return ChannelPresentationProfile(
        schema_version="channel-presentation-profile-v1",
        channel_id="ch-t41",
        allowed_recipe_families=("subtitle/default",),
        density=DensityLimits(per_kind=dict.fromkeys(ALL_KINDS, cap), global_per_minute=60.0),
        title_choices=TitleChoices(
            opening="title/opening",
            chapter="title/opening",
            lower_third="title/lower-third",
        ),
        subtitle_style=SubtitleStyle(
            recipe_id="subtitle/default", max_line_length_chars=13, font_size_px=42
        ),
        transition_preferences=TransitionPreferences(
            preferred_order=("dissolve", "hard_cut"),
            max_duration_frames=30,
        ),
        punch_in_range=PunchInRange(min=1.0, max=1.5),
        audio_policy=AudioPolicy(
            dialogue_lufs=ClosedRange(min=-18.0, max=-14.0),
            true_peak_db=ClosedRange(min=-2.0, max=0.0),
            bgm_duck_level_db=ClosedRange(min=-18.0, max=-12.0),
            sfx=SfxPolicy(allowed=True, max_per_minute=5.0),
        ),
        color_policy=ColorPolicy(
            technical_normalize_required=True,
            shot_match_required=False,
            channel_look="color/channel-look",
        ),
        intro_outro=IntroOutroRules(mode="optional", max_duration_frames=300),
        brand_assets=BrandAssets(fonts=("NotoSansJP",), safe_margin_pct=5.0),
    )


def _range(metric: AudioMetric, minimum: float, maximum: float, unit: AudioUnit) -> TargetRangeV1:
    return TargetRangeV1(metric=metric, minimum=minimum, maximum=maximum, unit=unit)


def _stage(
    stage: AudioStageName, ranges: tuple[TargetRangeV1, ...], *, enabled: bool
) -> AudioFinishingStageV1:
    return AudioFinishingStageV1(
        stage=stage,
        goal="fixture",
        target_ranges=ranges,
        enabled=enabled,
        justification=None if enabled else "not-requested: fixture",
    )


def _audio_plan(*, bgm_enabled: bool) -> AudioFinishingPlanV1:
    op_names: tuple[AudioProcessingOpName, ...] = (
        "eq",
        "compression",
        "voice_isolation",
    )
    ops = tuple(
        AudioProcessingOpV1(
            op=op,
            capability=OP_CAPABILITY[op],
            enabled=False,
            justification="not-requested: fixture",
        )
        for op in op_names
    )
    optional = AudioFinishingStageV1(
        stage="optional_eq_compression_voice_isolation",
        goal="fixture",
        target_ranges=(_range("dialogue_loudness", -18.0, -14.0, "lufs"),),
        enabled=False,
        justification="not-requested: fixture",
        ops=ops,
    )
    stages = (
        _stage("dialogue_cleanup", (_range("noise_reduction", -20.0, -6.0, "db"),), enabled=True),
        _stage(
            "dialogue_level_normalization",
            (_range("dialogue_loudness", -18.0, -14.0, "lufs"),),
            enabled=True,
        ),
        optional,
        _stage(
            "bgm_placement",
            (_range("bgm_level", -24.0, -18.0, "lufs"),),
            enabled=bgm_enabled,
        ),
        _stage("music_ducking", (_range("duck_depth", -18.0, -12.0, "db"),), enabled=False),
        _stage(
            "ambience_preservation",
            (_range("ambience_level", -30.0, -20.0, "db"),),
            enabled=False,
        ),
        _stage("optional_sfx", (_range("sfx_peak", -12.0, -6.0, "dbfs"),), enabled=False),
        AudioFinishingStageV1(
            stage="loudness_peak_qc",
            goal="fixture",
            target_ranges=(
                _range("integrated_loudness", -16.0, -14.0, "lufs"),
                _range("true_peak", -2.0, 0.0, "dbtp"),
            ),
            enabled=True,
        ),
    )
    return AudioFinishingPlanV1(
        schema_version="audio-finishing-plan-v1", episode_id="ep-t41", stages=stages
    )


def _clean_input() -> EditorialQcInput:
    c1 = _candidate("c1", (0, 90), block="blk-00")
    c2 = _candidate(
        "c2",
        (200, 260),
        block="blk-01",
        handles=MomentHandles(in_frame=10, out_frame=10),
    )
    primary = (
        _clip("itm-1", "c1", (0, 90), (0, 90)),
        _clip("itm-2", "c2", (190, 270), (90, 170)),
    )
    dialogue = AudioTrackV2(
        role="dialogue",
        track_id="trk-dlg",
        items=(_audio_item("aud-1", 0, 90), _audio_item("aud-2", 90, 170)),
    )
    cues = (
        _cue("cue-1", "今日は東京タワーの撮影です", 0, 90),
        _cue("cue-2", "撮影を終えて感想です", 90, 170),
    )
    story = StoryPlanV1(
        schema_version="story-plan-v1",
        episode_id="ep-t41",
        brief_ref=StoryBriefRef(episode_id="ep-t41"),
        blocks=(
            StoryBlock(block_id="blk-00", purpose="intro", block_kind="hook", order=0),
            StoryBlock(block_id="blk-01", purpose="wrap", block_kind="cta", order=1),
        ),
    )
    triage = (
        TriageEntry(
            shot_id="shot-c1",
            role="speech",
            story_relevance=0.9,
            select_potential=0.9,
            novelty=0.9,
            uncertainty=0.1,
            router_score=0.9,
        ),
        TriageEntry(
            shot_id="shot-c2",
            role="speech",
            story_relevance=0.9,
            select_potential=0.9,
            novelty=0.9,
            uncertainty=0.1,
            router_score=0.85,
        ),
    )
    return EditorialQcInput(
        ir_v2=_ir(primary, cues=cues, audio=(dialogue,)),
        preview_trace=_trace(subtitle=True),
        subtitle_plan=_sub_plan(),
        selection=_kept(c1, c2),
        story_plan=story,
        triage=triage,
    )


def _run_check(name: EditorialQcCheck, inputs: EditorialQcInput):
    return getattr(editorial_checks, f"check_{name}")(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)


# ------------------------------------------------------ per-check positives


def test_duplicated_explanation_detects_near_identical_cues() -> None:
    text = "今日は東京タワーの撮影に行きました"
    cues = (_cue("cue-a", text, 0, 60), _cue("cue-b", text, 200, 260))
    ir = _ir((_clip("itm-1", "c1", (0, 60), (0, 60)),), cues=cues)
    candidates = editorial_checks.check_duplicated_explanation(
        EditorialQcInput(ir_v2=ir), DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.check == "duplicated_explanation"
    assert candidate.severity == "warning"
    assert candidate.record_span == RecordFrameSpan(start_frame=200, end_frame=260)
    assert set(candidate.item_refs) == {"cue-a", "cue-b"}


def test_narrative_jump_flags_missing_referenced_block_as_critical() -> None:
    c2 = _candidate("c2", (200, 260), block="blk-ghost")
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "c1", (0, 90), (0, 90)),)),
        selection=_kept(_candidate("c1", (0, 90), block="blk-00"), c2),
        story_plan=StoryPlanV1(
            schema_version="story-plan-v1",
            episode_id="ep-t41",
            brief_ref=StoryBriefRef(episode_id="ep-t41"),
            blocks=(StoryBlock(block_id="blk-00", purpose="a", block_kind="hook", order=0),),
        ),
    )
    candidates = editorial_checks.check_narrative_jump(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["narrative_jump"]
    assert candidates[0].severity == "critical"
    assert candidates[0].needs_human_review is True


def test_narrative_jump_flags_skipped_intermediate_block() -> None:
    c1 = _candidate("c1", (0, 90), block="blk-00")
    c3 = _candidate("c3", (200, 290), block="blk-02")
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "c1", (0, 90), (0, 90)),)),
        selection=_kept(c1, c3),
        story_plan=StoryPlanV1(
            schema_version="story-plan-v1",
            episode_id="ep-t41",
            brief_ref=StoryBriefRef(episode_id="ep-t41"),
            blocks=(
                StoryBlock(block_id="blk-00", purpose="a", block_kind="hook", order=0),
                StoryBlock(block_id="blk-01", purpose="b", block_kind="body", order=1),
                StoryBlock(block_id="blk-02", purpose="c", block_kind="cta", order=2),
            ),
        ),
    )
    candidates = editorial_checks.check_narrative_jump(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["narrative_jump"]
    assert candidates[0].severity == "warning"
    assert "story:blk-01" in candidates[0].evidence_refs


def test_awkward_cut_flags_placement_cutting_into_content() -> None:
    c1 = _candidate("c1", (0, 90))
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "c1", (10, 90), (0, 80)),)), selection=_kept(c1)
    )
    candidates = editorial_checks.check_awkward_cut(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["awkward_cut"]
    assert candidates[0].severity == "warning"


def test_awkward_cut_flags_dropped_handles() -> None:
    c2 = _candidate("c2", (200, 260), handles=MomentHandles(in_frame=10, out_frame=10))
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-2", "c2", (200, 260), (0, 60)),)), selection=_kept(c2)
    )
    candidates = editorial_checks.check_awkward_cut(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["awkward_cut"]
    assert candidates[0].severity == "info"


def test_awkward_cut_accepts_handled_placement() -> None:
    c2 = _candidate("c2", (200, 260), handles=MomentHandles(in_frame=10, out_frame=10))
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-2", "c2", (190, 270), (0, 80)),)), selection=_kept(c2)
    )
    assert editorial_checks.check_awkward_cut(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS) == ()


def test_broll_irrelevance_flags_low_token_overlap() -> None:
    broll = _candidate("b1", (300, 360), rationale="駅前の混雑した様子を紹介する映像")
    anchor = _candidate("c1", (0, 90))
    inputs = EditorialQcInput(
        ir_v2=_ir(
            (_clip("itm-1", "c1", (0, 90), (0, 90)),),
            b_roll=(_clip("itm-b1", "b1", (300, 360), (10, 70)),),
        ),
        selection=_kept(anchor, broll),
        creative_plan=_creative(
            PlacePrimaryClipOp(kind="place_primary_clip", op_id="op-place-1", candidate_ref="c1"),
            BRollOverlayOp(
                kind="b_roll_overlay",
                op_id="op-broll-1",
                candidate_ref="b1",
                anchor_candidate_ref="c1",
            ),
        ),
    )
    candidates = editorial_checks.check_broll_irrelevance(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["broll_irrelevance"]
    assert candidates[0].severity == "warning"
    assert candidates[0].record_span == RecordFrameSpan(start_frame=10, end_frame=70)


def test_broll_irrelevance_accepts_related_broll() -> None:
    broll = _candidate("b1", (300, 360), rationale="東京タワーの外観の撮影映像")
    anchor = _candidate("c1", (0, 90))
    inputs = EditorialQcInput(
        ir_v2=_ir(
            (_clip("itm-1", "c1", (0, 90), (0, 90)),),
            b_roll=(_clip("itm-b1", "b1", (300, 360), (10, 70)),),
        ),
        selection=_kept(anchor, broll),
        creative_plan=_creative(
            BRollOverlayOp(
                kind="b_roll_overlay",
                op_id="op-broll-1",
                candidate_ref="b1",
                anchor_candidate_ref="c1",
            ),
        ),
    )
    assert editorial_checks.check_broll_irrelevance(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS) == ()


def test_subtitle_mismatch_flags_dropped_and_over_trimmed_records() -> None:
    records = (
        ReconciliationRecordV1(
            cue_id="cue-9",
            transcript_ref="tr-9",
            action="dropped",
            detail="cut by the edit",
            source_frames_lost=60,
        ),
        ReconciliationRecordV1(
            cue_id="cue-8",
            transcript_ref="tr-8",
            action="trimmed",
            detail="partial overlap",
            source_frames_lost=30,
        ),
    )
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "c1", (0, 60), (0, 60)),)),
        subtitle_plan=_sub_plan(records=records),
    )
    candidates = editorial_checks.check_subtitle_mismatch(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["subtitle_mismatch", "subtitle_mismatch"]
    by_ref = {c.evidence_refs[0]: c for c in candidates}
    assert by_ref["subtitle:cue-9"].severity == "info"
    assert by_ref["subtitle:cue-8"].severity == "warning"


def test_subtitle_mismatch_flags_preview_without_subtitle_track() -> None:
    plan_cues = (
        SubtitlePlanCueV1(
            cue_id="cue-p1",
            transcript_ref="tr-p1",
            source_id="src-a",
            lines=("今日は東京タワーです",),
            source_span=SourceFrameSpan(start_frame=0, end_frame=60, rate=RATE),
            record_span=RecordFrameSpan(start_frame=0, end_frame=60),
        ),
    )
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "c1", (0, 60), (0, 60)),)),
        preview_trace=_trace(subtitle=False, total=60),
        subtitle_plan=_sub_plan(cues=plan_cues),
    )
    candidates = editorial_checks.check_subtitle_mismatch(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
    assert [c.check for c in candidates] == ["subtitle_mismatch"]
    assert candidates[0].severity == "warning"
    assert candidates[0].evidence_refs == ("preview-trace:no-subtitle-input",)


def test_effect_density_excess_maps_t32_violations() -> None:
    intents = tuple(
        PresentationIntentV2(
            intent_id=f"pi-{index}",
            kind="sfx_accent",
            target_span=RecordFrameSpan(start_frame=start, end_frame=start + 12),
            params=SfxAccentParams(cue_hint="pop"),
            rationale="accent hit",
        )
        for index, start in enumerate((0, 30, 60))
    )
    density_ir = _ir((_clip("itm-d", "cd", (0, 180), (0, 180)),))
    inputs = EditorialQcInput(
        ir_v2=density_ir,
        presentation_intents=intents,
        presentation_profile=_presentation_profile(cap=10.0),
    )
    candidates = editorial_checks.check_effect_density_excess(
        inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert [c.check for c in candidates] == ["effect_density_excess"]
    assert candidates[0].severity == "warning"
    assert "sfx_accent" in candidates[0].detail
    under = EditorialQcInput(
        ir_v2=density_ir,
        presentation_intents=intents[:1],
        presentation_profile=_presentation_profile(cap=10.0),
    )
    assert (
        editorial_checks.check_effect_density_excess(under, DEFAULT_EDITORIAL_QC_THRESHOLDS) == ()
    )


def test_long_low_value_segment_flags_long_low_scored_placement() -> None:
    cl = _candidate("cl", (0, 600))
    inputs = EditorialQcInput(
        ir_v2=_ir((_clip("itm-1", "cl", (0, 600), (0, 600)),)),
        selection=_kept(cl),
        triage=(
            TriageEntry(
                shot_id="shot-cl",
                role="speech",
                story_relevance=0.2,
                select_potential=0.2,
                novelty=0.2,
                uncertainty=0.2,
                router_score=0.2,
            ),
        ),
    )
    candidates = editorial_checks.check_long_low_value_segment(
        inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert [c.check for c in candidates] == ["long_low_value_segment"]
    assert candidates[0].severity == "info"
    assert candidates[0].record_span == RecordFrameSpan(start_frame=0, end_frame=600)
    assert "triage:shot-cl" in candidates[0].evidence_refs


def test_long_low_value_segment_skips_high_score_and_short_spans() -> None:
    high = _candidate("c1", (0, 600))
    short = _candidate("c2", (700, 800))
    inputs = EditorialQcInput(
        ir_v2=_ir(
            (
                _clip("itm-1", "c1", (0, 600), (0, 600)),
                _clip("itm-2", "c2", (700, 800), (600, 700)),
            )
        ),
        selection=_kept(high, short),
        triage=(
            TriageEntry(
                shot_id="shot-c1",
                role="speech",
                story_relevance=0.9,
                select_potential=0.9,
                novelty=0.9,
                uncertainty=0.1,
                router_score=0.9,
            ),
            TriageEntry(
                shot_id="shot-c2",
                role="speech",
                story_relevance=0.2,
                select_potential=0.2,
                novelty=0.2,
                uncertainty=0.2,
                router_score=0.2,
            ),
        ),
    )
    assert (
        editorial_checks.check_long_low_value_segment(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS) == ()
    )


def test_audio_transition_problem_flags_uncovered_gap() -> None:
    dialogue = AudioTrackV2(
        role="dialogue",
        track_id="trk-dlg",
        items=(_audio_item("aud-1", 0, 100), _audio_item("aud-2", 150, 250)),
    )
    gap_ir = _ir((_clip("itm-1", "c1", (0, 250), (0, 250)),), audio=(dialogue,))
    candidates = editorial_checks.check_audio_transition_problem(
        EditorialQcInput(ir_v2=gap_ir), DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert [c.check for c in candidates] == ["audio_transition_problem"]
    assert candidates[0].record_span == RecordFrameSpan(start_frame=100, end_frame=150)


def test_audio_transition_problem_accepts_covered_and_contiguous() -> None:
    contiguous = AudioTrackV2(
        role="dialogue",
        track_id="trk-dlg",
        items=(_audio_item("aud-1", 0, 100), _audio_item("aud-2", 100, 200)),
    )
    assert (
        editorial_checks.check_audio_transition_problem(
            EditorialQcInput(
                ir_v2=_ir((_clip("itm-1", "c1", (0, 200), (0, 200)),), audio=(contiguous,))
            ),
            DEFAULT_EDITORIAL_QC_THRESHOLDS,
        )
        == ()
    )
    gapped = AudioTrackV2(
        role="dialogue",
        track_id="trk-dlg",
        items=(_audio_item("aud-1", 0, 100), _audio_item("aud-2", 150, 250)),
    )
    covered_ir = TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-t41",
        rate=RATE,
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="trk-primary",
                items=(
                    _clip("itm-1", "c1", (0, 100), (0, 100)),
                    _clip("itm-2", "c2", (100, 250), (100, 250)),
                ),
            ),
        ),
        audio_tracks=(gapped,),
        transitions=(
            SemanticTransitionV2(
                transition_id="tr-1",
                style="dissolve",
                at_record_frame=100,
                a_item_ref="itm-1",
                b_item_ref="itm-2",
                duration_frames=30,
            ),
        ),
    )
    assert (
        editorial_checks.check_audio_transition_problem(
            EditorialQcInput(ir_v2=covered_ir), DEFAULT_EDITORIAL_QC_THRESHOLDS
        )
        == ()
    )


def test_audio_transition_problem_flags_bgm_enabled_without_music_track() -> None:
    dialogue = AudioTrackV2(
        role="dialogue",
        track_id="trk-dlg",
        items=(_audio_item("aud-1", 0, 170),),
    )
    ir = _ir((_clip("itm-1", "c1", (0, 170), (0, 170)),), audio=(dialogue,))
    enabled = editorial_checks.check_audio_transition_problem(
        EditorialQcInput(ir_v2=ir, audio_plan=_audio_plan(bgm_enabled=True)),
        DEFAULT_EDITORIAL_QC_THRESHOLDS,
    )
    assert [c.check for c in enabled] == ["audio_transition_problem"]
    assert enabled[0].evidence_refs == ("audio-plan:bgm_placement",)
    disabled = editorial_checks.check_audio_transition_problem(
        EditorialQcInput(ir_v2=ir, audio_plan=_audio_plan(bgm_enabled=False)),
        DEFAULT_EDITORIAL_QC_THRESHOLDS,
    )
    assert disabled == ()


def test_sensitive_content_flags_keyword_hit_with_mandatory_review() -> None:
    cues = (_cue("cue-s", "私の電話番号は090-1234-5678です", 0, 60),)
    ir = _ir((_clip("itm-1", "c1", (0, 60), (0, 60)),), cues=cues)
    candidates = editorial_checks.check_sensitive_private_content(
        EditorialQcInput(ir_v2=ir), DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert [c.check for c in candidates] == ["sensitive_private_content"]
    assert candidates[0].severity == "warning"
    assert candidates[0].needs_human_review is True
    assert candidates[0].record_span == RecordFrameSpan(start_frame=0, end_frame=60)


def test_sensitive_content_honours_configured_patterns() -> None:
    cues = (_cue("cue-s", "ここに社内の合言葉を書きます", 0, 60),)
    ir = _ir((_clip("itm-1", "c1", (0, 60), (0, 60)),), cues=cues)
    inputs = EditorialQcInput(ir_v2=ir)
    thresholds = EditorialQcThresholds(sensitive_patterns=("合言葉",))
    candidates = editorial_checks.check_sensitive_private_content(inputs, thresholds)
    assert [c.check for c in candidates] == ["sensitive_private_content"]
    assert (
        editorial_checks.check_sensitive_private_content(inputs, DEFAULT_EDITORIAL_QC_THRESHOLDS)
        == ()
    )


def test_sensitive_content_rejects_invalid_regex_config() -> None:
    with pytest.raises(ValidationError, match="pattern_invalid"):
        EditorialQcThresholds(sensitive_patterns=("([unclosed",))


def test_manual_finalization_surfaces_ir_effects_and_plan_ops() -> None:
    ir = _ir(
        (_clip("itm-1", "c1", (0, 90), (0, 90)),),
        effect_intents=(
            EffectIntentV2(
                effect_id="fx-manual-1",
                kind="manual_required",
                target_item_id="itm-1",
                note="rotoscope cleanup",
            ),
        ),
    )
    creative = _creative(
        ManualRequiredOp(kind="manual_required", op_id="op-manual-1", effect_note="hand-drawn mask")
    )
    candidates = editorial_checks.check_unsupported_manual_finalization(
        EditorialQcInput(ir_v2=ir, creative_plan=creative), DEFAULT_EDITORIAL_QC_THRESHOLDS
    )
    assert [c.check for c in candidates] == ["unsupported_manual_finalization"] * 2
    assert all(c.severity == "info" for c in candidates)
    assert all(c.needs_human_review is True for c in candidates)
    assert candidates[0].record_span == RecordFrameSpan(start_frame=0, end_frame=90)


@pytest.mark.parametrize("check", ALL_EDITORIAL_CHECKS)
def test_clean_input_yields_no_candidate_for_each_check(check) -> None:
    inputs = _clean_input()
    assert _run_check(check, inputs) == ()


def test_run_editorial_qc_clean_yields_no_candidates() -> None:
    report = aggregate_candidates(run_editorial_qc(_clean_input()))
    assert report.candidates == ()
    assert report.counts_by_severity == {"info": 0, "warning": 0, "critical": 0}
    assert report.counts_by_check == {}


# ------------------------------------------------------ invariants + report


def test_critical_candidate_requires_human_review_at_the_model() -> None:
    with pytest.raises(ValidationError, match="critical_requires_review"):
        EditorialQcCandidateV1(
            check="narrative_jump",
            detail="must carry needs_human_review",
            severity="critical",
            needs_human_review=False,
            evidence_refs=("story:blk-ghost",),
        )


def test_aggregate_counts_and_critical_invariant() -> None:
    critical = EditorialQcCandidateV1(
        check="narrative_jump",
        detail="missing referenced block",
        severity="critical",
        needs_human_review=True,
        evidence_refs=("story:blk-ghost",),
    )
    info = EditorialQcCandidateV1(
        check="unsupported_manual_finalization",
        detail="manual op",
        severity="info",
        needs_human_review=True,
        evidence_refs=("plan:op-manual-1",),
    )
    warning = EditorialQcCandidateV1(
        check="duplicated_explanation",
        detail="near-identical cues",
        severity="warning",
        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
        evidence_refs=("ir:cue-a",),
    )
    report = aggregate_candidates((critical, info, warning, critical))
    assert report.schema_version == "editorial-qc-report-v1"
    assert report.counts_by_severity == {"info": 1, "warning": 1, "critical": 2}
    assert report.counts_by_check == {
        "duplicated_explanation": 1,
        "narrative_jump": 2,
        "unsupported_manual_finalization": 1,
    }


def test_report_rejects_tampered_counts() -> None:
    candidate = EditorialQcCandidateV1(
        check="duplicated_explanation",
        detail="near-identical cues",
        severity="warning",
        evidence_refs=("ir:cue-a",),
    )
    with pytest.raises(ValidationError, match="counts_mismatch"):
        EditorialQcReportV1(
            schema_version="editorial-qc-report-v1",
            candidates=(candidate,),
            counts_by_severity={"info": 0, "warning": 5, "critical": 0},
            counts_by_check={"duplicated_explanation": 1},
        )


# ------------------------------------------------- review-command seam


def test_to_review_context_payload_maps_natural_command_kinds() -> None:
    broll = EditorialQcCandidateV1(
        check="broll_irrelevance",
        detail="B-roll overlaps anchor only 0.02",
        severity="warning",
        record_span=RecordFrameSpan(start_frame=200, end_frame=260),
        evidence_refs=("plan:op-broll-1",),
    )
    narrative = EditorialQcCandidateV1(
        check="narrative_jump",
        detail="missing referenced block",
        severity="critical",
        needs_human_review=True,
        evidence_refs=("story:blk-ghost",),
    )
    context = to_review_context((broll, narrative), rate=RATE)
    assert context.schema_version == "editorial-qc-review-context-v1"
    assert len(context.items) == 2
    mapped = context.items[0]
    assert mapped.command_kind == "insert_broll"
    assert mapped.target_seconds == pytest.approx(200 / 30)
    assert mapped.severity == "warning"
    assert mapped.needs_human_review is False
    unmapped = context.items[1]
    assert unmapped.command_kind is None
    assert unmapped.target_seconds is None
    assert unmapped.needs_human_review is True


def test_to_review_context_command_table_only_uses_known_kinds() -> None:
    assert set(editorial_checks.CHECK_COMMAND_KIND) <= set(ALL_EDITORIAL_CHECKS)
    assert set(editorial_checks.CHECK_COMMAND_KIND.values()) <= set(
        get_args(ReviewCommandKind.__value__)
    )


# ------------------------------------------------------ round-trip + determinism


def test_flawed_episode_round_trips_through_json() -> None:
    inputs = _clean_input()
    flawed = inputs.model_copy(
        update={
            "ir_v2": _ir(
                (
                    _clip("itm-1", "c1", (0, 90), (0, 90)),
                    _clip("itm-2", "c2", (190, 270), (90, 170)),
                ),
                cues=(
                    _cue("cue-a", "同じ説明の文です", 0, 60),
                    _cue("cue-b", "同じ説明の文です", 90, 150),
                ),
            )
        }
    )
    report = aggregate_candidates(run_editorial_qc(flawed))
    parsed = EditorialQcReportV1.model_validate_json(report.model_dump_json())
    assert parsed == report
    assert report.counts_by_check.get("duplicated_explanation") == 1


def test_run_editorial_qc_is_deterministic() -> None:
    inputs = _clean_input()
    first = run_editorial_qc(inputs)
    second = run_editorial_qc(inputs)
    assert first == second
    assert aggregate_candidates(first) == aggregate_candidates(second)
