"""ColorFinishingPlanV1 — justified no-op color plan tests (task 35).

PRD 10.4 / implementation plan 8.5: technical correction, camera/shot
matching, channel look, reference sanity checks, and visual QC live in
SEPARATE typed sections; every ``needed: false`` section must carry an
explicit justification (the justified no-op contract — an unjustified
empty plan is unrepresentable). A per-shot generative grade is NOT part
of the contract.

Locked behaviors:

(a) two-camera fixture -> matching enabled with a cross-camera group;
(b) exposure issues -> technical correction enabled with evidence;
(c) clean single-camera + configured look -> look section only, every
    other section a justified no-op;
(d) unjustified no-op -> typed rejection (noop_justification_required);
(e) separation — the three sections serialize as distinct keys;
(f) preferred_path is a pure function of the mcp-fit color rows'
    statuses (parametrized);
(g) round-trip through JSON + double-build determinism.

Adversarial probes: malformed_input = unjustified no-op / contradictory
sections (groups on a no-op, look_ref on a no-op, evidence-less needed,
resolve keys); stale_state = round-trip + byte-identical rebuild.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.creative_plan.color_finishing import (
    CameraShotMatchingV1,
    CameraSourceV1,
    ChannelEpisodeLookV1,
    ColorFactsV1,
    ColorFinishingPlanV1,
    ColorIssueV1,
    ColorPlanPolicy,
    CorrectionItemV1,
    MatchGroupV1,
    McpCapabilityStatus,
    ReferenceSanityCheckV1,
    TechnicalCorrectionV1,
    VisualQcCheckV1,
    build_color_plan,
    preferred_path_for,
)

EPISODE = "ep-color-01"


def _policy(
    color_grade_status: McpCapabilityStatus = "accepted",
    advanced_qc_status: McpCapabilityStatus = "accepted",
) -> ColorPlanPolicy:
    return ColorPlanPolicy(
        color_grade_status=color_grade_status,
        advanced_qc_status=advanced_qc_status,
    )


def _facts(**overrides: object) -> ColorFactsV1:
    payload: dict[str, object] = {"episode_id": EPISODE}
    payload.update(overrides)
    return ColorFactsV1.model_validate(payload)


# ---------------------------------------------------------------- (a) match


def test_matching_enabled_when_two_cameras() -> None:
    facts = _facts(
        cameras=(
            CameraSourceV1(camera_id="cam-a", source_ids=("src-1", "src-2")),
            CameraSourceV1(camera_id="cam-b", source_ids=("src-3",)),
        )
    )
    plan = build_color_plan(facts, policy=_policy())

    assert plan.camera_shot_matching.needed is True
    assert len(plan.camera_shot_matching.groups) == 1
    group = plan.camera_shot_matching.groups[0]
    assert set(group.camera_ids) == {"cam-a", "cam-b"}
    assert plan.camera_shot_matching.evidence != ()


# ---------------------------------------------------- (b) technical correction


def test_exposure_issues_enable_technical_correction_with_evidence() -> None:
    facts = _facts(
        exposure_issues=(ColorIssueV1(source_id="src-1", detail="underexposed by 1.2 EV"),),
        cameras=(CameraSourceV1(camera_id="cam-a", source_ids=("src-1",)),),
    )
    plan = build_color_plan(facts, policy=_policy())

    assert plan.technical_correction.exposure.needed is True
    assert any("src-1" in item for item in plan.technical_correction.exposure.evidence)
    assert plan.technical_correction.white_balance.needed is False
    assert plan.technical_correction.white_balance.justification


# ----------------------------------------------------------- (c) look only


def test_look_only_plan_with_justified_noops() -> None:
    facts = _facts(
        cameras=(CameraSourceV1(camera_id="cam-a", source_ids=("src-1",)),),
        look_configured=True,
        look_ref="look-channel-warm",
    )
    plan = build_color_plan(facts, policy=_policy())

    assert plan.channel_episode_look.needed is True
    assert plan.channel_episode_look.look_ref == "look-channel-warm"
    assert plan.technical_correction.exposure.needed is False
    assert plan.technical_correction.exposure.justification
    assert plan.technical_correction.white_balance.needed is False
    assert plan.technical_correction.white_balance.justification
    assert plan.camera_shot_matching.needed is False
    assert plan.camera_shot_matching.justification
    assert plan.camera_shot_matching.groups == ()


# ------------------------------------------- (d) justified no-op contract


def test_unjustified_noop_item_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CorrectionItemV1(needed=False)
    assert excinfo.value.errors()[0]["type"] == "noop_justification_required"


def test_unjustified_noop_matching_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CameraShotMatchingV1(needed=False)
    assert excinfo.value.errors()[0]["type"] == "noop_justification_required"


def test_unjustified_noop_look_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ChannelEpisodeLookV1(needed=False)
    assert excinfo.value.errors()[0]["type"] == "noop_justification_required"


def test_empty_plan_without_justifications_rejected() -> None:
    # The all-noop "empty plan" — no section justified — is unrepresentable.
    with pytest.raises(ValidationError) as excinfo:
        ColorFinishingPlanV1(
            schema_version="color-finishing-plan-v1",
            episode_id=EPISODE,
            technical_correction=TechnicalCorrectionV1(
                exposure=CorrectionItemV1(needed=False),
                white_balance=CorrectionItemV1(needed=False),
            ),
            camera_shot_matching=CameraShotMatchingV1(needed=False),
            channel_episode_look=ChannelEpisodeLookV1(needed=False),
            preferred_path="external",
        )
    assert excinfo.value.errors()[0]["type"] == "noop_justification_required"


# ------------------------------------------------- section invariants (typed)


def test_needed_section_requires_evidence() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CorrectionItemV1(needed=True, evidence=(), justification=None)
    assert excinfo.value.errors()[0]["type"] == "needed_requires_evidence"


def test_justification_forbidden_when_needed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CorrectionItemV1(needed=True, evidence=("src-1: dark",), justification="note")
    assert excinfo.value.errors()[0]["type"] == "justification_only_for_noop"


def test_noop_matching_with_groups_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CameraShotMatchingV1(
            needed=False,
            justification="single camera",
            groups=(MatchGroupV1(group_id="match-1", camera_ids=("cam-a",)),),
        )
    assert excinfo.value.errors()[0]["type"] == "matching_noop_has_groups"


def test_matching_needed_without_groups_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CameraShotMatchingV1(needed=True, evidence=("camera:cam-a",), groups=())
    assert excinfo.value.errors()[0]["type"] == "matching_requires_groups"


def test_noop_look_with_ref_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ChannelEpisodeLookV1(needed=False, justification="no look", look_ref="look-x")
    assert excinfo.value.errors()[0]["type"] == "noop_has_look_ref"


def test_whitespace_justification_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CorrectionItemV1(needed=False, justification="   ")
    assert excinfo.value.errors()[0]["type"] == "noop_justification_required"


def test_resolve_keys_rejected() -> None:
    # NLE-independent artifact: resolve-specific keys are forbidden (task-31 seam).
    with pytest.raises(ValidationError) as excinfo:
        ColorFactsV1.model_validate({"episode_id": EPISODE, "resolve_grade_node": "vendor-blue"})
    assert excinfo.value.errors()[0]["type"] == "resolve_field_forbidden"


# ------------------------------------------------------- (e) separation


def test_sections_separated_in_serialization() -> None:
    plan = build_color_plan(
        _facts(
            cameras=(
                CameraSourceV1(camera_id="cam-a", source_ids=("src-1",)),
                CameraSourceV1(camera_id="cam-b", source_ids=("src-2",)),
            ),
            look_configured=True,
        ),
        policy=_policy(),
    )
    dumped = plan.model_dump()

    assert "technical_correction" in dumped
    assert "camera_shot_matching" in dumped
    assert "channel_episode_look" in dumped
    assert set(dumped["technical_correction"]) == {"exposure", "white_balance"}


# ------------------------------------------- (f) preferred path from matrix


@pytest.mark.parametrize(
    ("color_status", "qc_status", "expected"),
    [
        ("accepted", "accepted", "mcp_live_grading"),
        ("accepted", "failed", "mcp_live_grading"),
        ("failed", "accepted", "advanced_drx_qc"),
        ("partial", "accepted", "advanced_drx_qc"),
        ("not_available", "accepted", "advanced_drx_qc"),
        ("failed", "failed", "external"),
        ("not_available", "partial", "external"),
    ],
)
def test_preferred_path_follows_matrix_status(
    color_status: McpCapabilityStatus,
    qc_status: McpCapabilityStatus,
    expected: str,
) -> None:
    assert preferred_path_for(color_status, qc_status) == expected

    plan = build_color_plan(
        _facts(look_configured=True),
        policy=_policy(color_grade_status=color_status, advanced_qc_status=qc_status),
    )
    assert plan.preferred_path == expected


def test_real_matrix_rows_prefer_mcp_live_grading() -> None:
    # capabilities/v4.4/mcp-fit.json: color-grade-preset-drx accepted,
    # advanced-delivery-qc accepted -> the preferred path is live grading.
    plan = build_color_plan(
        _facts(look_configured=True),
        policy=_policy(color_grade_status="accepted", advanced_qc_status="accepted"),
    )
    assert plan.preferred_path == "mcp_live_grading"


# ------------------------------------------- sanity checks + visual QC


def test_reference_sanity_checks_derived_from_relevance() -> None:
    plan = build_color_plan(
        _facts(skin_tone_relevant=True, product_relevant=True),
        policy=_policy(),
    )
    kinds = {check.kind for check in plan.reference_sanity_checks}
    assert kinds == {"skin_tone", "product"}


def test_reference_sanity_checks_empty_when_not_relevant() -> None:
    plan = build_color_plan(_facts(), policy=_policy())
    assert plan.reference_sanity_checks == ()


def test_visual_qc_checks_carried_from_policy() -> None:
    policy = ColorPlanPolicy(
        color_grade_status="accepted",
        advanced_qc_status="accepted",
        visual_qc_checks=(
            VisualQcCheckV1(check_id="qc-exposure", delegated_to="advanced-delivery-qc"),
        ),
    )
    plan = build_color_plan(_facts(), policy=policy)
    assert plan.visual_qc == policy.visual_qc_checks


def test_duplicate_check_ids_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ColorFinishingPlanV1(
            schema_version="color-finishing-plan-v1",
            episode_id=EPISODE,
            technical_correction=TechnicalCorrectionV1(
                exposure=CorrectionItemV1(needed=False, justification="clean"),
                white_balance=CorrectionItemV1(needed=False, justification="clean"),
            ),
            camera_shot_matching=CameraShotMatchingV1(needed=False, justification="one cam"),
            channel_episode_look=ChannelEpisodeLookV1(needed=False, justification="no look"),
            reference_sanity_checks=(
                ReferenceSanityCheckV1(check_id="chk-skin", kind="skin_tone"),
                ReferenceSanityCheckV1(check_id="chk-skin", kind="product"),
            ),
            preferred_path="external",
        )
    assert excinfo.value.errors()[0]["type"] == "duplicate_check_id"


# ------------------------------------------------------- (g) round-trip


def test_round_trip_and_double_build_determinism() -> None:
    facts = _facts(
        exposure_issues=(ColorIssueV1(source_id="src-1", detail="blown highlights"),),
        cameras=(
            CameraSourceV1(camera_id="cam-a", source_ids=("src-1",)),
            CameraSourceV1(camera_id="cam-b", source_ids=("src-2",)),
        ),
        look_configured=True,
        look_ref="look-channel-warm",
        skin_tone_relevant=True,
    )
    policy = ColorPlanPolicy(
        color_grade_status="accepted",
        advanced_qc_status="accepted",
        visual_qc_checks=(
            VisualQcCheckV1(check_id="qc-exposure", delegated_to="advanced-delivery-qc"),
        ),
    )
    plan = build_color_plan(facts, policy=policy)

    clone = ColorFinishingPlanV1.model_validate_json(plan.model_dump_json())
    assert clone == plan

    rebuilt = build_color_plan(facts, policy=policy)
    assert rebuilt.model_dump_json() == plan.model_dump_json()
