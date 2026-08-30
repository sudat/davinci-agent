"""AudioFinishingPlanV1 tests — task 34, PRD §10.3 / impl-plan §8.4.

The audio finishing ladder is a fixed SEMANTIC order of stages; each stage
carries a semantic goal plus target ranges only (no Fairlight/MCP execution
detail — that is compiled in task 38/39). Locked behaviors:

(a) ladder ORDER invariant — the stage sequence is fixed regardless of which
    stages are enabled, and a reordered/duplicated/missing stage list is a
    typed validation error at the model itself;
(b) fixture 1 — clean dialogue only gets a MINIMAL plan: cleanup and
    normalization are disabled with an "already-good" auto-justification
    (improving already-good audio is forbidden without a semantic reason);
(c) fixture 2 — noisy dialogue enables cleanup;
(d) fixture 3 — dialogue + BGM enables placement and ducking with target
    ranges;
(e) fixture 4 — ambience-led sequence enables ambience preservation while
    dialogue stages stay minimal;
(f) a disabled stage or op without justification is a typed error;
(g) an enabled op bound to a non-accepted mcp-fit capability is rejected
    unless it carries an explicit acceptance justification;
(h) plans round-trip through canonical JSON;
(i) the loudness/peak QC stage carries both target ranges.

Adversarial probes: malformed inputs — missing justification, reordered
ladder, metric/unit mismatch, inverted range; stale state — JSON round-trip;
capability gate against synthetic mcp-fit statuses.

The capability gate is DATA-DRIVEN: tests inject synthetic statuses so the
"failed voice-isolation row" behavior is hermetic regardless of the live
mcp-fit.json state.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from services.creative_plan.audio_finishing import (
    AUDIO_LADDER,
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    AudioFinishingError,
    AudioFinishingPlanV1,
    AudioFinishingStageV1,
    AudioOpRequestV1,
    TargetRangeV1,
    UnacceptedAudioCapabilityError,
    UnnecessaryAudioProcessingError,
    build_audio_plan,
    load_capability_statuses,
    validate_audio_finishing,
)

# Synthetic capability statuses mirroring a failed voice-isolation row.
_FAILED_STATUSES = {"voice-isolation": "failed", "audio-property-operation": "failed"}
_ACCEPTED_STATUSES = {"voice-isolation": "accepted", "audio-property-operation": "accepted"}


def _facts(**overrides: bool | str) -> AudioFactsV1:
    values: dict[str, bool | str] = {
        "episode_id": "ep-audio",
        "dialogue_clean": True,
        "has_bgm": False,
        "has_ambience": False,
        "measured_loudness_ok": True,
    }
    values.update(overrides)
    return AudioFactsV1(**values)  # type: ignore[arg-type]


def _build(
    facts: AudioFactsV1, op_requests: tuple[AudioOpRequestV1, ...] = ()
) -> AudioFinishingPlanV1:
    return build_audio_plan(
        facts,
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=op_requests,
        capability_statuses=_FAILED_STATUSES,
    )


def _clean_fixture() -> AudioFactsV1:
    return _facts()  # fixture 1: clean dialogue only


def _noisy_fixture() -> AudioFactsV1:
    return _facts(dialogue_clean=False, measured_loudness_ok=False)  # fixture 2


def _bgm_fixture() -> AudioFactsV1:
    return _facts(has_bgm=True)  # fixture 3: dialogue + BGM ducking


def _ambience_fixture() -> AudioFactsV1:
    return _facts(has_ambience=True)  # fixture 4: ambience-led visual sequence


ALL_FIXTURES = [_clean_fixture(), _noisy_fixture(), _bgm_fixture(), _ambience_fixture()]


def _stage(plan: AudioFinishingPlanV1, name: str) -> AudioFinishingStageV1:
    return next(s for s in plan.stages if s.stage == name)


def _payload(plan: AudioFinishingPlanV1) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def _stage_of(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in payload["stages"] if s["stage"] == name)


# ---- (a) ladder order invariant


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_ladder_order_is_fixed_regardless_of_enables(fixture: AudioFactsV1) -> None:
    plan = _build(fixture)
    assert [s.stage for s in plan.stages] == list(AUDIO_LADDER)


def test_enable_pattern_differs_between_fixtures_but_order_does_not() -> None:
    clean = _build(_clean_fixture())
    noisy = _build(_noisy_fixture())
    assert [s.stage for s in clean.stages] == [s.stage for s in noisy.stages]
    clean_enables = {s.stage: s.enabled for s in clean.stages}
    noisy_enables = {s.stage: s.enabled for s in noisy.stages}
    assert clean_enables != noisy_enables


def test_reordered_ladder_is_rejected() -> None:
    plan = _build(_noisy_fixture())
    payload = plan.model_dump(mode="json")
    stages = payload["stages"]
    payload["stages"] = [stages[1], stages[0], *stages[2:]]
    with pytest.raises(ValidationError, match="ladder_order"):
        AudioFinishingPlanV1.model_validate(payload)


def test_missing_stage_is_rejected() -> None:
    plan = _build(_noisy_fixture())
    payload = plan.model_dump(mode="json")
    payload["stages"] = payload["stages"][1:]
    with pytest.raises(ValidationError, match="ladder_order"):
        AudioFinishingPlanV1.model_validate(payload)


# ---- (b) fixture 1: clean dialogue only -> minimal plan


def test_clean_dialogue_gets_minimal_plan() -> None:
    plan = _build(_clean_fixture())
    cleanup = _stage(plan, "dialogue_cleanup")
    normalization = _stage(plan, "dialogue_level_normalization")
    assert cleanup.enabled is False
    assert cleanup.justification is not None
    assert cleanup.justification.startswith("already-good:")
    assert normalization.enabled is False
    assert normalization.justification is not None
    assert normalization.justification.startswith("already-good:")
    optional = _stage(plan, "optional_eq_compression_voice_isolation")
    assert optional.enabled is False
    assert all(op.enabled is False for op in optional.ops)


# ---- (c) fixture 2: noisy dialogue -> cleanup enabled


def test_noisy_dialogue_enables_cleanup_and_normalization() -> None:
    plan = _build(_noisy_fixture())
    cleanup = _stage(plan, "dialogue_cleanup")
    normalization = _stage(plan, "dialogue_level_normalization")
    assert cleanup.enabled is True
    assert normalization.enabled is True


# ---- (c2) dialogue-only measurement-domain coherence (Task 5 repair 2):
# with no BGM and no ambience the program audio IS the dialogue, so both
# LUFS gates measure the same full-render integrated loudness — the plan
# must not demand one quantity inside two disjoint ranges.


def test_dialogue_only_normalization_carries_the_delivery_target() -> None:
    """A dialogue-only plan's normalization gate must equal the delivery
    target: dialogue loudness and whole-program loudness are the same
    measured quantity, so [-18,-16] against the QC's [-14.5,-13.5] would
    be a physically unsatisfiable contract."""
    plan = _build(_noisy_fixture())
    normalization = _stage(plan, "dialogue_level_normalization")
    qc = _stage(plan, "loudness_peak_qc")
    delivery = DEFAULT_AUDIO_POLICY.integrated_loudness_lufs
    assert normalization.enabled is True
    assert qc.enabled is True
    target = normalization.target_ranges[0]
    assert (target.metric, target.unit) == ("dialogue_loudness", "lufs")
    assert (target.minimum, target.maximum) == (delivery.minimum, delivery.maximum)
    qc_loudness = next(t for t in qc.target_ranges if t.metric == "integrated_loudness")
    assert (target.minimum, target.maximum) == (
        qc_loudness.minimum,
        qc_loudness.maximum,
    )


def test_dialogue_only_contradictory_dual_range_plan_is_rejected() -> None:
    """A hand-built dialogue-only plan whose normalization range is disjoint
    from the QC delivery range is an impossible dual-range contract over one
    measured quantity — the facts-aware guard must refuse it typed."""
    payload = _payload(_build(_noisy_fixture()))
    normalization = _stage_of(payload, "dialogue_level_normalization")
    normalization["target_ranges"][0]["minimum"] = -18.0
    normalization["target_ranges"][0]["maximum"] = -16.0
    plan = AudioFinishingPlanV1.model_validate(payload)
    with pytest.raises(AudioFinishingError, match="dialogue-only-loudness-conflict"):
        validate_audio_finishing(
            plan,
            audio_facts=_noisy_fixture(),
            capability_statuses=_FAILED_STATUSES,
        )


def test_bgm_episode_keeps_the_dialogue_domain_target() -> None:
    """With BGM present the dialogue target is a different measurement
    domain from the whole-program delivery target — the default policy's
    dialogue range must be preserved unchanged."""
    plan = _build(_bgm_fixture())
    normalization = _stage(plan, "dialogue_level_normalization")
    target = normalization.target_ranges[0]
    assert (target.metric, target.unit) == ("dialogue_loudness", "lufs")
    assert (target.minimum, target.maximum) == (-18.0, -16.0)


# ---- (d) fixture 3: dialogue + BGM -> placement and ducking with ranges


def test_bgm_fixture_enables_placement_and_ducking_with_ranges() -> None:
    plan = _build(_bgm_fixture())
    placement = _stage(plan, "bgm_placement")
    ducking = _stage(plan, "music_ducking")
    assert placement.enabled is True
    assert placement.target_ranges[0].metric == "bgm_level"
    assert placement.target_ranges[0].unit == "lufs"
    assert ducking.enabled is True
    assert ducking.target_ranges[0].metric == "duck_depth"
    assert ducking.target_ranges[0].unit == "db"
    assert ducking.target_ranges[0].minimum < ducking.target_ranges[0].maximum


# ---- (e) fixture 4: ambience-led -> preservation enabled, dialogue minimal


def test_ambience_fixture_preserves_ambience_with_minimal_dialogue() -> None:
    plan = _build(_ambience_fixture())
    ambience = _stage(plan, "ambience_preservation")
    cleanup = _stage(plan, "dialogue_cleanup")
    normalization = _stage(plan, "dialogue_level_normalization")
    assert ambience.enabled is True
    assert ambience.target_ranges[0].metric == "ambience_level"
    assert cleanup.enabled is False
    assert normalization.enabled is False


# ---- (f) no-op justification guard


def test_disabled_stage_without_justification_is_rejected() -> None:
    payload = _payload(_build(_noisy_fixture()))
    qc = _stage_of(payload, "loudness_peak_qc")
    qc["enabled"] = False
    qc["justification"] = None
    with pytest.raises(ValidationError, match="noop_justification_required"):
        AudioFinishingPlanV1.model_validate(payload)


def test_disabled_op_without_justification_is_rejected() -> None:
    payload = _payload(_build(_noisy_fixture()))
    optional = _stage_of(payload, "optional_eq_compression_voice_isolation")
    optional["ops"][0]["justification"] = None
    with pytest.raises(ValidationError, match="noop_justification_required"):
        AudioFinishingPlanV1.model_validate(payload)


# ---- (g) capability gate on accepted-only ops


def test_voice_isolation_without_acceptance_justification_is_rejected() -> None:
    with pytest.raises(UnacceptedAudioCapabilityError) as error:
        _build(_noisy_fixture(), op_requests=(AudioOpRequestV1(op="voice_isolation"),))
    assert error.value.code == "unaccepted-capability"
    assert "voice-isolation" in error.value.detail


def test_voice_isolation_with_justification_passes_capability_gate() -> None:
    plan = _build(
        _noisy_fixture(),
        op_requests=(
            AudioOpRequestV1(op="voice_isolation", justification="HVAC bleed under the voice"),
        ),
    )
    optional = _stage(plan, "optional_eq_compression_voice_isolation")
    voice = next(op for op in optional.ops if op.op == "voice_isolation")
    assert voice.enabled is True
    assert optional.enabled is True


def test_accepted_status_lets_op_through_without_justification() -> None:
    plan = build_audio_plan(
        _noisy_fixture(),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=(AudioOpRequestV1(op="voice_isolation"),),
        capability_statuses=_ACCEPTED_STATUSES,
    )
    assert _stage(plan, "optional_eq_compression_voice_isolation").enabled is True


def test_op_on_clean_audio_without_reason_is_unnecessary_processing() -> None:
    with pytest.raises(UnnecessaryAudioProcessingError):
        build_audio_plan(
            _clean_fixture(),
            policy=DEFAULT_AUDIO_POLICY,
            op_requests=(AudioOpRequestV1(op="voice_isolation"),),
            capability_statuses=_ACCEPTED_STATUSES,
        )


def test_unknown_capability_status_is_typed_error() -> None:
    plan = _build(
        _noisy_fixture(),
        op_requests=(AudioOpRequestV1(op="voice_isolation", justification="reason"),),
    )
    with pytest.raises(ValueError, match="unknown-capability"):
        validate_audio_finishing(
            plan,
            audio_facts=_noisy_fixture(),
            capability_statuses={"audio-property-operation": "failed"},
        )


# ---- no-unnecessary-processing guard on hand-built plans


def test_enabled_cleanup_on_clean_audio_requires_justification() -> None:
    payload = _payload(_build(_clean_fixture()))
    cleanup = _stage_of(payload, "dialogue_cleanup")
    cleanup["enabled"] = True
    cleanup["justification"] = None
    hand_built = AudioFinishingPlanV1.model_validate(payload)
    with pytest.raises(UnnecessaryAudioProcessingError):
        validate_audio_finishing(
            hand_built,
            audio_facts=_clean_fixture(),
            capability_statuses=_FAILED_STATUSES,
        )


def test_enabled_cleanup_on_clean_audio_with_reason_is_allowed() -> None:
    payload = _payload(_build(_clean_fixture()))
    cleanup = _stage_of(payload, "dialogue_cleanup")
    cleanup["enabled"] = True
    cleanup["justification"] = "shot 12 re-recorded on a different mic; match the rest"
    hand_built = AudioFinishingPlanV1.model_validate(payload)
    validated = validate_audio_finishing(
        hand_built,
        audio_facts=_clean_fixture(),
        capability_statuses=_FAILED_STATUSES,
    )
    assert validated == hand_built


# ---- (h) round-trip


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_plans_round_trip_through_json(fixture: AudioFactsV1) -> None:
    plan = _build(fixture)
    restored = AudioFinishingPlanV1.model_validate(plan.model_dump(mode="json"))
    assert restored == plan


# ---- (i) loudness QC target ranges


def test_loudness_qc_carries_both_target_ranges() -> None:
    plan = _build(_noisy_fixture())
    qc = _stage(plan, "loudness_peak_qc")
    assert qc.enabled is True
    metrics = {r.metric: r for r in qc.target_ranges}
    assert set(metrics) == {"integrated_loudness", "true_peak"}
    assert metrics["integrated_loudness"].unit == "lufs"
    assert metrics["true_peak"].unit == "dbtp"


def test_qc_stage_without_both_ranges_is_rejected() -> None:
    payload = _payload(_build(_noisy_fixture()))
    qc = _stage_of(payload, "loudness_peak_qc")
    qc["target_ranges"] = qc["target_ranges"][:1]
    with pytest.raises(ValidationError, match="qc_ranges_incomplete"):
        AudioFinishingPlanV1.model_validate(payload)


# ---- structural guards


def test_ops_outside_optional_stage_are_rejected() -> None:
    payload = _payload(_build(_noisy_fixture()))
    optional = _stage_of(payload, "optional_eq_compression_voice_isolation")
    cleanup = _stage_of(payload, "dialogue_cleanup")
    cleanup["ops"] = optional["ops"]
    with pytest.raises(ValidationError, match="ops_outside_optional_stage"):
        AudioFinishingPlanV1.model_validate(payload)


def test_metric_unit_mismatch_is_rejected() -> None:
    with pytest.raises(ValidationError, match="metric_unit_mismatch"):
        TargetRangeV1(metric="true_peak", minimum=-2.0, maximum=-1.0, unit="db")


def test_inverted_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="range_inverted"):
        TargetRangeV1(metric="duck_depth", minimum=12.0, maximum=6.0, unit="db")


def test_duplicate_op_requests_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate-op-request"):
        _build(
            _noisy_fixture(),
            op_requests=(
                AudioOpRequestV1(op="eq", justification="reason"),
                AudioOpRequestV1(op="eq", justification="other"),
            ),
        )


def test_episode_linkage_is_enforced() -> None:
    plan = _build(_noisy_fixture())
    with pytest.raises(ValueError, match="episode-mismatch"):
        validate_audio_finishing(
            plan,
            audio_facts=_facts(episode_id="ep-other"),
            capability_statuses=_FAILED_STATUSES,
        )


# ---- default policy and real mcp-fit integration seam


def test_builder_defaults_load_real_mcp_fit_statuses() -> None:
    plan = build_audio_plan(_noisy_fixture(), policy=DEFAULT_AUDIO_POLICY)
    cleanup = _stage(plan, "dialogue_cleanup")
    assert cleanup.enabled is True
    statuses = load_capability_statuses()
    assert "voice-isolation" in statuses
    assert "audio-property-operation" in statuses
    assert set(statuses.values()) <= {"accepted", "failed", "partial", "not_available"}
