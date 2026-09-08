"""Consultation slice 2: adopted policies become planning inputs (store level).

Deterministic extraction (no LLM): the LATEST adopt|revise judgment with a
non-empty scope, joined with its proposal's details. A newer
reject/both_wrong withdraws (None — never reuse the older案). Outcomes are
an append-only journal; the rebuild derivation is pure state mapping.
"""

from __future__ import annotations

from pathlib import Path

from services.episode_cockpit.consultation_store import (
    AdoptedPolicyV1,
    ConsultationDecision,
    ConsultationJudgmentV1,
    ConsultationPolicyOutcomeV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_consultation,
    append_judgment,
    append_policy_outcome,
    append_proposal_set,
    derive_policy_rebuild,
    latest_adopted_policy,
    load_policy_outcomes,
    now_stamp,
    policy_summary,
    selection_rebuild_active,
)

_DETAILS = {
    "audience_message": "始めて見る人に分かる導入",
    "structure": "導入→本編→締め",
    "duration_estimate": "約4分",
    "candidate_scenes": ["opening", "demo"],
    "subtitle_policy": "短めの字幕",
    "audio_policy": "BGM小さめ",
    "tempo_policy": "前半テンポ重視",
    "reference_mapping": "参考1の構成を踏襲",
    "unused_reasons": "未使用素材はなし",
    "unconfirmed": ["尺の希望"],
}


def _setup(tmp_path: Path) -> str:
    record = ConsultationRecordV1(
        consultation_id="c1", created_at=now_stamp(), message="短くしたい"
    )
    append_consultation(tmp_path, record)
    append_proposal_set(
        tmp_path,
        ConsultationProposalSetV1(
            consultation_id="c1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(_DETAILS),
                ),
            ),
        ),
    )
    return "c1"


def _judge(  # noqa: PLR0913, PLR0917 (judgment record fields ARE the helper contract)
    tmp_path: Path,
    consultation_id: str,
    judgment_id: str,
    decision: ConsultationDecision,
    scope: dict[str, bool],
    proposal_id: str | None = "prop-1",
) -> None:
    append_judgment(
        tmp_path,
        ConsultationJudgmentV1(
            judgment_id=judgment_id,
            consultation_id=consultation_id,
            proposal_id=proposal_id,
            decision=decision,
            scope=ConsultationScope.model_validate(scope),
            note="メモ",
            created_at=now_stamp(),
        ),
    )


def test_no_judgments_yields_no_policy(tmp_path: Path) -> None:
    _setup(tmp_path)

    assert latest_adopted_policy(tmp_path) is None


def test_adopt_joins_proposal_details(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
    )

    policy = latest_adopted_policy(tmp_path)

    assert isinstance(policy, AdoptedPolicyV1)
    assert policy.consultation_id == consultation_id
    assert policy.judgment_id == "j1"
    assert policy.proposal_id == "prop-1"
    assert policy.decision == "adopt"
    assert policy.scope.composition is True
    assert policy.scope.appearance is False
    assert policy.structure == "導入→本編→締め"
    assert policy.subtitle_policy == "短めの字幕"
    assert list(policy.candidate_scenes) == ["opening", "demo"]
    assert list(policy.unconfirmed) == ["尺の希望"]
    assert policy.note == "メモ"


def test_newer_reject_withdraws_the_policy(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
    )
    assert latest_adopted_policy(tmp_path) is not None

    _judge(
        tmp_path, consultation_id, "j2", "reject",
        {"composition": False, "appearance": False, "audio": False},
        proposal_id=None,
    )

    assert latest_adopted_policy(tmp_path) is None


def test_latest_judgment_wins_over_older_adopt(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
    )
    _judge(
        tmp_path, consultation_id, "j2", "revise",
        {"composition": False, "appearance": False, "audio": True},
    )

    policy = latest_adopted_policy(tmp_path)

    assert policy is not None
    assert policy.judgment_id == "j2"
    assert policy.decision == "revise"
    assert policy.scope.audio is True
    assert policy.scope.composition is False


def test_empty_scope_yields_no_policy(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": False, "appearance": False, "audio": False},
    )

    assert latest_adopted_policy(tmp_path) is None


def test_whole_consultation_judgment_yields_no_policy(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
        proposal_id=None,
    )

    assert latest_adopted_policy(tmp_path) is None


def test_unknown_proposal_yields_no_policy(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
        proposal_id="prop-9",
    )

    assert latest_adopted_policy(tmp_path) is None


def test_policy_summary_is_a_compact_typed_projection(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "revise",
        {"composition": True, "appearance": True, "audio": False},
    )
    policy = latest_adopted_policy(tmp_path)
    assert policy is not None

    summary = policy_summary(policy)

    assert summary.decision == "revise"
    assert summary.scope.composition is True
    assert summary.scope.audio is False
    assert summary.structure == "導入→本編→締め"
    assert summary.tempo_policy == "前半テンポ重視"
    assert list(summary.unconfirmed) == ["尺の希望"]
    assert summary.note == "メモ"


def test_policy_outcome_round_trip(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    outcome = ConsultationPolicyOutcomeV1(
        outcome_id="pout-1",
        consultation_id=consultation_id,
        judgment_id="j1",
        proposal_id="prop-1",
        plan_version="v2",
        status="honored",
        reasons=("director re-run",),
        note="構造検証のみ",
        created_at=now_stamp(),
    )

    append_policy_outcome(tmp_path, outcome)

    saved = load_policy_outcomes(tmp_path)
    assert saved == [outcome]


def test_rebuild_derivation_without_policy_is_none(tmp_path: Path) -> None:
    view = derive_policy_rebuild(tmp_path, None)

    assert view["status"] == "none"
    assert view["target_version"] is None


def test_rebuild_derivation_without_reservation_is_none(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
    )
    policy = latest_adopted_policy(tmp_path)
    assert policy is not None

    view = derive_policy_rebuild(tmp_path, policy)

    assert view["status"] == "none"
    assert view["target_version"] is None


def test_rebuild_derivation_failed_outcome_without_reservation(tmp_path: Path) -> None:
    consultation_id = _setup(tmp_path)
    _judge(
        tmp_path, consultation_id, "j1", "adopt",
        {"composition": True, "appearance": False, "audio": False},
    )
    policy = latest_adopted_policy(tmp_path)
    assert policy is not None
    append_policy_outcome(
        tmp_path,
        ConsultationPolicyOutcomeV1(
            outcome_id="pout-1",
            consultation_id=consultation_id,
            judgment_id="j1",
            proposal_id="prop-1",
            plan_version=None,
            status="failed",
            reasons=("planner-infeasible: empty",),
            note="版を確定していません。",
            created_at=now_stamp(),
        ),
    )

    view = derive_policy_rebuild(tmp_path, policy)

    assert view["status"] == "failed"
    assert view["target_version"] is None
    assert "planner-infeasible" in str(view["detail"])


def test_no_selection_chain_is_not_active(tmp_path: Path) -> None:
    assert selection_rebuild_active(tmp_path) is False
