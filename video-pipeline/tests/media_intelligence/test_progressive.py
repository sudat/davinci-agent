# allow: SIZE_OK — task 16 pinned budget/progressive coverage to this module and
# the v44 T5 plan extends the SAME module (budget-kind accounting split + lead-map
# windows); repo test modules run 500-700 LOC (T3/T4 precedent).
"""Progressive analysis scheduler + AnalysisBudgetV1 (task 16 + v44 T5).

Covers: full three-stage routing, per-window trigger attribution, the 25%
deep-review coverage SLO with typed unjustified-expansion rejection,
determinism (identical canonical bytes on double run), the router-only
contract (no keep/remove intent fields), deterministic low-stratum
recall-audit sampling, the T5 budget-kind split (targeted deep review vs
full-source lead map), and deterministic lead-map window generation.
"""

from __future__ import annotations

import re
from itertools import pairwise

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate
from services.foundation_io import canonical_model_bytes
from services.media_intelligence.budget import (
    TRIGGER_REASONS,
    BudgetError,
    BudgetExpansionUnjustifiedError,
    BudgetKind,
    BudgetLeadMapCoverageError,
    BudgetRequest,
    DeepReviewWindow,
    UniversalPassRecord,
    build_analysis_budget,
)
from services.media_intelligence.lead_map import (
    LeadMapWindowError,
    LeadMapWindowPolicy,
    generate_lead_map_windows,
)
from services.media_intelligence.models import MediaIntelligenceArtifact
from services.media_intelligence.progressive import (
    HumanReviewWindow,
    ProgressivePlanError,
    ProgressivePolicy,
    plan_progressive_analysis,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _shot(  # noqa: PLR0913 — fixture DSL: one knob per trigger dimension
    shot_id: str,
    start: int,
    end: int,
    *,
    role: str = "coverage",
    select: str = "medium",
    conf: tuple[str, str] = ("medium", "medium"),
    transcript: bool = False,
    speaker: bool = False,
    words: bool = False,
    similarity: int = 0,
    visual_evidence: bool = False,
    face_cues: bool = False,
    subject_action: bool = False,
    quality_flags: tuple[str, ...] = (),
) -> dict:
    payload: dict = {
        "shot_id": shot_id,
        "source_span": {"start_frame": start, "end_frame": end},
        "description": "synthetic shot",
        "visual": {
            "shot_size": "medium",
            "camera_motion": "static",
            "quality_flags": list(quality_flags),
        },
        "editorial": {
            "role": role,
            "select_potential": select,
            "best_moment": {"frame": start + 1, "why": "moment"},
            "pacing": "moderate",
            "cuttability": {"in": "clean", "out": "clean"},
        },
        "transcript_refs": [],
        "evidence_refs": [],
        "confidence": {"editorial": conf[0], "visual": conf[1]},
    }
    if transcript:
        payload["transcript_segments"] = [
            {
                "segment_id": f"seg-{shot_id}",
                "text": "story content",
                "start_frame": start,
                "end_frame": end,
            }
        ]
    if speaker:
        payload["speaker_info"] = {"speaker_id": "spk-1"}
    if words:
        payload["word_timings"] = [
            {"word": "story", "start_frame": start, "end_frame": start + 1}
        ]
    if similarity:
        payload["similarity_refs"] = [
            {"ref_shot_id": f"shot-other-{index}", "score": 0.9}
            for index in range(similarity)
        ]
    if visual_evidence:
        payload["object_refs"] = [{"label": "product box"}]
    if face_cues:
        payload["face_reaction_cues"] = [{"cue": "laughter"}]
    if subject_action:
        payload["subject_action"] = {"primary_subject": "host", "action": "demonstrates"}
    return payload


def _artifact(shots: list[dict]) -> MediaIntelligenceArtifact:
    return MediaIntelligenceArtifact.model_validate(
        {
            "schema_version": "media-intelligence-v2",
            "episode_id": "ep-prog",
            "sources": [{"source_id": "src-001"}],
            "shots": shots,
        }
    )


def _multi_trigger_fixture() -> tuple[MediaIntelligenceArtifact, ProgressivePolicy]:
    """Six shots firing all eight trigger kinds, under the 25% SLO.

    Layout (30 fps): a 0-10 high_value · d 10-20 broll adjacent to a ·
    b 20-30 uncertain + qc flag · c 30-40 visually driven + reaction ·
    e 40-50 lowest score (recall sample) · f 50-450 plain filler.
    Union = 55 frames of 450 (12.2%) ≤ 25%.
    """
    shots = [
        _shot(
            "shot-a", 0, 10, select="high", conf=("high", "high"),
            transcript=True, speaker=True,
        ),
        _shot("shot-d", 10, 20, role="broll"),
        _shot("shot-b", 20, 30, conf=("low", "low"), quality_flags=("soft_focus",)),
        _shot("shot-c", 30, 40, role="reaction", face_cues=True),
        _shot("shot-e", 40, 50, select="low"),
        _shot("shot-f", 50, 450),
    ]
    policy = ProgressivePolicy(
        recall_audit_sample_count=1,
        human_review_windows=(HumanReviewWindow(start_frame=100, end_frame=105),),
    )
    return _artifact(shots), policy


def test_full_run_triages_all_shots_and_records_budget() -> None:
    artifact = _artifact(
        [_shot("shot-001", 0, 90), _shot("shot-002", 90, 180), _shot("shot-003", 180, 270)]
    )

    plan = plan_progressive_analysis(artifact, policy=ProgressivePolicy())

    assert [entry.shot_id for entry in plan.triage] == ["shot-001", "shot-002", "shot-003"]
    budget = plan.budget
    assert budget.schema_version == "analysis-budget-v1"
    assert budget.universal_pass.shots_count == 3
    assert budget.universal_pass.wall_clock_seconds == 0.0
    assert budget.source_duration_seconds == pytest.approx(9.0)
    assert budget.deep_review_windows == ()
    assert budget.reviewed_seconds == 0.0
    assert budget.frame_or_token_counters.frames == 0
    assert budget.cache_hits == 0
    assert budget.policy_limits.deep_review_coverage_max == 0.25
    assert budget.expansion_reasons == ()


def test_every_deep_window_carries_trigger_reason_and_source() -> None:
    artifact, policy = _multi_trigger_fixture()

    plan = plan_progressive_analysis(artifact, policy=policy)

    windows = {
        (w.start_frame, w.end_frame, w.trigger_reason, w.trigger_source)
        for w in plan.deep_review_windows
    }
    expected = {
        (0, 10, "high_value", "shot-a"),
        (10, 20, "broll_near_block", "shot-d"),
        (20, 30, "uncertain", "shot-b"),
        (20, 30, "editorial_qc_flag", "shot-b"),
        (30, 40, "visually_driven", "shot-c"),
        (30, 40, "reaction_action_timing", "shot-c"),
        (40, 50, "recall_audit_sample", "shot-e"),
        (100, 105, "human_request", "policy:human_request"),
    }
    assert windows == expected
    assert {w.trigger_reason for w in plan.deep_review_windows} == {
        "high_value",
        "uncertain",
        "visually_driven",
        "reaction_action_timing",
        "broll_near_block",
        "editorial_qc_flag",
        "human_request",
        "recall_audit_sample",
    }
    for window in plan.deep_review_windows:
        assert _IDENTIFIER.match(window.trigger_source) is not None


def test_over_coverage_without_expansion_reason_is_typed_error() -> None:
    artifact = _artifact(
        [
            _shot(
                "shot-hi", 0, 100, select="high", conf=("high", "high"),
                transcript=True, speaker=True,
            ),
            _shot("shot-lo", 100, 300),
        ]
    )
    policy = ProgressivePolicy()

    with pytest.raises(BudgetExpansionUnjustifiedError, match="expansion"):
        plan_progressive_analysis(artifact, policy=policy)


def test_over_coverage_with_expansion_reason_allowed_and_recorded() -> None:
    artifact = _artifact(
        [
            _shot(
                "shot-hi", 0, 100, select="high", conf=("high", "high"),
                transcript=True, speaker=True,
            ),
            _shot("shot-lo", 100, 300),
        ]
    )
    reason = "story-critical: opening demonstration needs full review"
    policy = ProgressivePolicy(expansion_reasons=(reason,))

    plan = plan_progressive_analysis(artifact, policy=policy)

    assert plan.budget.expansion_reasons == (reason,)
    assert plan.budget.reviewed_seconds == pytest.approx(100 / 30)
    assert len(plan.deep_review_windows) == 1


def test_determinism_double_run_identical_canonical_bytes() -> None:
    artifact, policy = _multi_trigger_fixture()

    first = plan_progressive_analysis(artifact, policy=policy)
    second = plan_progressive_analysis(artifact, policy=policy)

    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    assert canonical_model_bytes(first.budget) == canonical_model_bytes(second.budget)


def test_plan_has_no_keep_or_remove_intent_fields() -> None:
    artifact, policy = _multi_trigger_fixture()
    plan = plan_progressive_analysis(artifact, policy=policy)

    def _walk_keys(value: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                keys.add(str(key).lower())
                keys |= _walk_keys(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                keys |= _walk_keys(child)
        return keys

    keys = _walk_keys(plan.model_dump(mode="json"))
    assert not any("keep" in key for key in keys)
    assert not any("remove" in key for key in keys)
    assert not any("verdict" in key for key in keys)


def test_recall_audit_sample_draws_from_low_stratum_deterministically() -> None:
    artifact = _artifact(
        [
            _shot("shot-hi", 0, 60, select="high"),
            _shot("shot-mid-a", 60, 120, select="medium"),
            _shot("shot-mid-b", 120, 180, select="medium"),
            _shot("shot-low-a", 180, 240, select="low"),
            _shot("shot-low-b", 240, 300, select="low"),
            _shot("shot-fill", 300, 600),
        ]
    )
    policy = ProgressivePolicy(recall_audit_sample_count=2)

    first = plan_progressive_analysis(artifact, policy=policy)
    second = plan_progressive_analysis(artifact, policy=policy)

    sampled = {
        w.trigger_source
        for w in first.deep_review_windows
        if w.trigger_reason == "recall_audit_sample"
    }
    assert sampled == {"shot-low-a", "shot-low-b"}
    assert canonical_model_bytes(first) == canonical_model_bytes(second)


def test_builder_unions_overlapping_windows_counted_once() -> None:
    request = BudgetRequest(
        source_duration_frames=300,
        frame_rate=RationalFrameRate(num=30, den=1),
        universal_pass=UniversalPassRecord(wall_clock_seconds=0.0, shots_count=2),
        windows=(
            DeepReviewWindow(
                start_frame=0, end_frame=100,
                trigger_reason="high_value", trigger_source="shot-a",
            ),
            DeepReviewWindow(
                start_frame=50, end_frame=150,
                trigger_reason="uncertain", trigger_source="shot-a",
            ),
            DeepReviewWindow(
                start_frame=200, end_frame=220,
                trigger_reason="visually_driven", trigger_source="shot-c",
            ),
        ),
        expansion_reasons=("uncertainty: overlapping triggers justified",),
    )

    budget = build_analysis_budget(request)

    assert budget.reviewed_seconds == pytest.approx(170 / 30)
    assert budget.frame_or_token_counters.frames == 170
    assert budget.source_duration_seconds == pytest.approx(10.0)


def test_human_window_beyond_source_duration_is_typed_rejection() -> None:
    artifact = _artifact([_shot("shot-001", 0, 100)])
    policy = ProgressivePolicy(
        human_review_windows=(HumanReviewWindow(start_frame=90, end_frame=150),)
    )

    with pytest.raises(BudgetError, match="exceeds source duration"):
        plan_progressive_analysis(artifact, policy=policy)


def test_zero_duration_artifact_is_typed_rejection() -> None:
    artifact = _artifact([])

    with pytest.raises(ProgressivePlanError, match="zero-duration"):
        plan_progressive_analysis(artifact, policy=ProgressivePolicy())


def test_inverted_window_span_rejected_by_model() -> None:
    with pytest.raises(ValidationError):
        DeepReviewWindow(
            start_frame=100,
            end_frame=50,
            trigger_reason="high_value",
            trigger_source="shot-a",
        )


# ---------------------------------------------------------------------------
# v44 T5: lead-map vs targeted deep-review budget accounting + lead-map windows
# ---------------------------------------------------------------------------
# Frozen v44-real-01 source truth (private/reference-episodes/v44-real-01/runs/
# arm-B-r2/media-intelligence.json): sources[0].duration_frames == 8467.
# 25% of 8467 = 2116.75, so 2116 unique targeted frames pass and 2117 fail.

_FROZEN_TOTAL_FRAMES = 8467
_V44_REAL_01_RATE = RationalFrameRate(num=30000, den=1001)
_LEAD_MAP_SOURCE = "policy:lead_map"
_BASELINE_LEAD_MAP_BOUNDS = ((0, 3600), (3300, 6900), (6600, 8467))


def _lead_map_window(start: int, end: int) -> DeepReviewWindow:
    return DeepReviewWindow(
        start_frame=start,
        end_frame=end,
        trigger_reason="lead_map_window",
        trigger_source=_LEAD_MAP_SOURCE,
    )


def _baseline_lead_map_windows() -> tuple[DeepReviewWindow, ...]:
    return tuple(_lead_map_window(start, end) for start, end in _BASELINE_LEAD_MAP_BOUNDS)


def _budget_request(
    windows: tuple[DeepReviewWindow, ...],
    *,
    budget_kind: BudgetKind = "targeted_deep_review",
    expansion_reasons: tuple[str, ...] = (),
    total_frames: int = _FROZEN_TOTAL_FRAMES,
) -> BudgetRequest:
    return BudgetRequest(
        source_duration_frames=total_frames,
        frame_rate=_V44_REAL_01_RATE,
        universal_pass=UniversalPassRecord(wall_clock_seconds=0.0, shots_count=1),
        windows=windows,
        budget_kind=budget_kind,
        expansion_reasons=expansion_reasons,
    )


def test_budget_kind_defaults_to_targeted_and_old_payloads_stay_loadable() -> None:
    budget = build_analysis_budget(_budget_request(()))

    assert budget.budget_kind == "targeted_deep_review"
    legacy_payload = budget.model_dump(mode="json")
    del legacy_payload["budget_kind"]
    reparsed = type(budget).model_validate(legacy_payload)
    assert reparsed.budget_kind == "targeted_deep_review"


def test_lead_map_full_union_budget_covers_source_without_expansion() -> None:
    budget = build_analysis_budget(
        _budget_request(_baseline_lead_map_windows(), budget_kind="lead_map")
    )

    assert budget.budget_kind == "lead_map"
    assert budget.frame_or_token_counters.frames == _FROZEN_TOTAL_FRAMES
    assert budget.reviewed_seconds == pytest.approx(8467 * 1001 / 30000)
    assert budget.expansion_reasons == ()


def test_lead_map_missing_region_blocks_the_run() -> None:
    gapped = (
        _lead_map_window(0, 3600),
        _lead_map_window(3900, 6900),
        _lead_map_window(6600, 8467),
    )

    with pytest.raises(BudgetLeadMapCoverageError, match="cover the full source"):
        build_analysis_budget(_budget_request(gapped, budget_kind="lead_map"))


def test_lead_map_duplicates_and_overlap_cannot_hide_a_gap() -> None:
    padded_gap = (
        _lead_map_window(0, 3600),
        _lead_map_window(0, 3600),
        _lead_map_window(3300, 3600),
        _lead_map_window(6600, 8467),
    )

    with pytest.raises(BudgetLeadMapCoverageError, match="cover the full source"):
        build_analysis_budget(_budget_request(padded_gap, budget_kind="lead_map"))


def test_targeted_boundary_2116_passes_and_2117_fails_without_expansion() -> None:
    at_limit = build_analysis_budget(_budget_request((_lead_map_window(0, 2116),)))

    assert at_limit.frame_or_token_counters.frames == 2116
    assert at_limit.budget_kind == "targeted_deep_review"

    with pytest.raises(BudgetExpansionUnjustifiedError, match="expansion"):
        build_analysis_budget(_budget_request((_lead_map_window(0, 2117),)))
    expanded = build_analysis_budget(
        _budget_request(
            (_lead_map_window(0, 2117),),
            expansion_reasons=("targeted: specialist clip over default cap",),
        )
    )
    assert expanded.frame_or_token_counters.frames == 2117


def test_new_trigger_literals_are_registered_vocabulary() -> None:
    windows = (
        DeepReviewWindow(
            start_frame=0, end_frame=10,
            trigger_reason="lead_map_window", trigger_source=_LEAD_MAP_SOURCE,
        ),
        DeepReviewWindow(
            start_frame=0, end_frame=10,
            trigger_reason="specialist_target", trigger_source="gemini:reduce",
        ),
    )

    assert all(window.trigger_reason in TRIGGER_REASONS for window in windows)


def test_lead_map_baseline_windows_match_frozen_8467_frame_evidence() -> None:
    windows = generate_lead_map_windows(_FROZEN_TOTAL_FRAMES)

    assert [(w.start_frame, w.end_frame) for w in windows] == [
        (0, 3600),
        (3300, 6900),
        (6600, 8467),
    ]
    assert all(w.trigger_reason == "lead_map_window" for w in windows)
    assert all(w.trigger_source == _LEAD_MAP_SOURCE for w in windows)
    assert generate_lead_map_windows(_FROZEN_TOTAL_FRAMES) == windows
    budget = build_analysis_budget(_budget_request(windows, budget_kind="lead_map"))
    assert budget.frame_or_token_counters.frames == _FROZEN_TOTAL_FRAMES


def test_lead_map_speech_boundary_within_150_snaps_internal_cut() -> None:
    windows = generate_lead_map_windows(_FROZEN_TOTAL_FRAMES, speech_boundaries=(3350,))

    assert [(w.start_frame, w.end_frame) for w in windows] == [
        (0, 3600),
        (3350, 6950),
        (6650, 8467),
    ]
    budget = build_analysis_budget(_budget_request(windows, budget_kind="lead_map"))
    assert budget.frame_or_token_counters.frames == _FROZEN_TOTAL_FRAMES


def test_lead_map_boundary_beyond_150_keeps_fixed_cut() -> None:
    windows = generate_lead_map_windows(
        _FROZEN_TOTAL_FRAMES, speech_boundaries=(0, 3500, 8467)
    )

    assert [(w.start_frame, w.end_frame) for w in windows] == [
        (0, 3600),
        (3300, 6900),
        (6600, 8467),
    ]


def test_lead_map_snap_tie_resolves_to_earlier_boundary() -> None:
    windows = generate_lead_map_windows(_FROZEN_TOTAL_FRAMES, speech_boundaries=(3200, 3400))

    assert [(w.start_frame, w.end_frame) for w in windows] == [
        (0, 3600),
        (3200, 6800),
        (6500, 8467),
    ]


def test_lead_map_final_window_never_exceeds_max_window_frames() -> None:
    total = 10200

    plain = generate_lead_map_windows(total)
    assert [(w.start_frame, w.end_frame) for w in plain] == [
        (0, 3600),
        (3300, 6900),
        (6600, 10200),
    ]

    backward_snap = generate_lead_map_windows(total, speech_boundaries=(6451,))
    assert [(w.start_frame, w.end_frame) for w in backward_snap] == [
        (0, 3600),
        (3300, 6900),
        (6451, 10051),
        (9751, 10200),
    ]
    for windows in (plain, backward_snap):
        assert all(w.end_frame - w.start_frame <= 3600 for w in windows)


def test_lead_map_zero_total_frames_is_typed_rejection() -> None:
    with pytest.raises(LeadMapWindowError, match="> 0"):
        generate_lead_map_windows(0)


def test_lead_map_policy_invariants_are_model_rejections() -> None:
    with pytest.raises(ValidationError):
        LeadMapWindowPolicy(max_window_frames=300, overlap_frames=300)
    with pytest.raises(ValidationError):
        LeadMapWindowPolicy(snap_radius_frames=301)


def test_lead_map_policy_rejects_non_progressing_snap_combinations() -> None:
    # Smallest possible snapped next start = start + (max - overlap - snap);
    # it must be > start or a nearby boundary can pin/rewind the start and
    # loop forever. 10-6-6 = -2 (the verified defect), 10-7-3 = 0 (repeat),
    # 10-9-8 = -7 (backward).
    with pytest.raises(ValidationError):
        LeadMapWindowPolicy(max_window_frames=10, overlap_frames=6, snap_radius_frames=6)
    with pytest.raises(ValidationError):
        LeadMapWindowPolicy(max_window_frames=10, overlap_frames=7, snap_radius_frames=3)
    with pytest.raises(ValidationError):
        LeadMapWindowPolicy(max_window_frames=10, overlap_frames=9, snap_radius_frames=8)


def test_lead_map_sound_small_policy_always_makes_strict_forward_progress() -> None:
    policy = LeadMapWindowPolicy(max_window_frames=10, overlap_frames=5, snap_radius_frames=4)

    windows = generate_lead_map_windows(40, speech_boundaries=(6,), policy=policy)
    starts = [w.start_frame for w in windows]

    assert all(later > earlier for earlier, later in pairwise(starts))
    assert windows[-1].end_frame == 40
    budget = build_analysis_budget(
        _budget_request(windows, budget_kind="lead_map", total_frames=40)
    )
    assert budget.frame_or_token_counters.frames == 40
